#imports
import json
import time
import os
import cv2
import base64
import logging
import serial
import struct
import subprocess
import shutil
from arduino.app_utils import App, Bridge
from datetime import datetime
from PIL import Image


#NOTE:
#Current settings are set to /dev/ttys1, if transfer doesn't work may need to be changed to 
#/dev/ttyHS1
#/dev/ttyS0
#/dev/ttyS2
#/dev/ttyS3

# Directory Paths

CAPTURE_DIR = "/app/python/"
PENDING_DIR = "/app/python/"

# Heater Directory path 

CUSBA = "/app/python/cusba64"
HEATER_CMD_PATHS = [
    "/app/python/heater_cmd.txt",
    "/home/arduino/heater_cmd.txt",
    "/home/arduino/ArduinoApps/asi_contoll_cer_v5/python/heater_cmd.txt",
    "/home/arduino/ArduinoApps/asi_controll_Cer_v5/python/heater_cmd.txt",
    "/home/arduino/ArduinoApps/asi_controller_v5/python/heater_cmd.txt",
]
HEATER_CONTROL_SCRIPT_PATHS = [
    "/app/python/heater_control.sh",
    "/home/arduino/ArduinoApps/asi_contoll_cer_v5/heater_control.sh",
    "/home/arduino/ArduinoApps/asi_controll_Cer_v5/heater_control.sh",
    "/home/arduino/ArduinoApps/asi_controller_v5/heater_control.sh",
    "/home/arduino/heater_control.sh",
]
HEATER_APPLY_COOLDOWN_SEC = 5
LAST_HEATER_APPLY_TIME = 0.0
LAST_HEATER_COMMAND = "OFF"
# AppLab should only write command intent; external heater_agent.py applies to USB relay.
HEATER_DIRECT_APPLY = False

#"/home/arduino/ArduinoApps/asi_controll_Cer_v5/python/cusba64"

print("=== SAVING FILES TO:", CAPTURE_DIR, "===")

os.makedirs(CAPTURE_DIR, exist_ok = True)
os.makedirs(PENDING_DIR, exist_ok = True)

LOG_FILE = os.path.join(CAPTURE_DIR, "asi_controller.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("asi_controller")


def heater_script_candidates():
    main_dir = os.path.dirname(os.path.abspath(__file__))
    return [
        os.path.join(main_dir, "heater_control.sh"),
        os.path.join(os.path.dirname(main_dir), "heater_control.sh"),
        *HEATER_CONTROL_SCRIPT_PATHS,
    ]


def resolve_cusba():
    candidates = [
        CUSBA,
        "/home/arduino/ArduinoApps/asi_contoll_cer_v5/python/cusba64",
        "/home/arduino/ArduinoApps/asi_controll_Cer_v5/python/cusba64",
        "/home/arduino/ArduinoApps/asi_controller_v5/python/cusba64",
        "/home/arduino/cusba64",
        "cusba64",
    ]
    for candidate in candidates:
        if "/" in candidate:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return None

# Presets

TEMP_MIN_C = -40.0
TEMP_MAX_C = 60.0

HUMIDITY_MIN = 0.0
HUMIDITY_MAX = 90.0

LUX_MAX = 60.0

HEATER_TEMP_THRESHOLD_C = 80 # Turn on if below this temp
HEATER_HUMIDITY_THRESHOLD = 80 # Turn on if above this %

MIN_CAPTURE_INTERVAL_SEC = 15 # 5 mins

# Time sync with TS-7250-V3
ctr = 0
TIME_SYNC_INTERVAL = 15
LAST_SYNC_FAILED = False
LAST_TIME_REQUEST_TIMEOUT = 5

# Packet IDs for Serial Transfer
PACKET_ENV = 0x01
PACKET_IMAGE = 0x02
PACKET_DONE = 0x03
PACKET_TIME_REQUEST = 0x04
PACKET_TIME_RESPONSE = 0x05

# Clean up function
CLEANUP_INTERVAL = 10
MAX_FILE_AGE_SEC = 4200
PROTECTED_FILES = {'requirements.txt'}

# Packet functions for transmission

def checksum(data: bytes) -> int:
    result = 0
    for b in data:
        result ^= b
    return result

# Must build the packet and encode the base64 string for the bridge

def make_packet(packet_id: int, data: bytes) -> str:
    length = len(data)
    chk = checksum(data)
    header = struct.pack('>BBI', 0xAA, packet_id, length)
    footer = struct.pack('>BB', chk, 0xBB)
    raw = header + data + footer
    return base64.b64encode(raw).decode('ascii')

def send_packet(packet_id: int, data: bytes):
    encode = make_packet(packet_id, data)
    result = Bridge.call("write_serial", encode + "\n")
    try:
        r = json.loads(result)
        if not r.get("ok"):
            print(f"Write failed: {r.get('error')}")
    except Exception:
            pass

def read_incoming_packet():
    raw_64 = Bridge.call("read_serial", "").strip()
    if not raw_64:
        return None, None
    try:
        raw = base64.b64decode(raw_64)
    except Exception as e:
        print("Base64 decode error:", e)
        return None, None

    if len(raw) < 7:
        return None, None

    if raw[0] != 0xAA:
        return None, None
    
    packet_id = raw[1]
    length = struct.unpack('>I', raw[2:6])[0]
    
    if len(raw) < 6 + length + 2:
        return None, None

    # Data starts immediately after 6-byte header.
    data = raw[6:6 + length]
    received_chk = raw[6 + length]
    end_byte = raw[6 + length + 1]
    
    if end_byte != 0xBB:
        print("WARNING: BAD END BYTE")
        return None, None

    if received_chk != checksum(data):
        print("WARNING: CHECKSUM MISMATCH, PACKET DROPPED")
        return None, None

    return packet_id, data
        

#FUNCTIONS

# Time Sync Functions
def set_system_time(time_str):
    global LAST_SYNC_FAILED
    try:
        result = subprocess.run(
            ['sudo', 'date', '-u', '-s', time_str],
            capture_output = True,
            text = True
        )
        if result.returncode == 0:
            print(f"System time set to UTC: {time_str}")
            LAST_SYNC_FAILED = False
        else:
            print(f"Failed to set time: {result.stderr}")
            LAST_SYNC_FAILED = True
    except Exception as e:
        print(f"set_system_time error:", e)
        LAST_SYNC_FAILED = True

def request_time_sync():
    global LAST_SYNC_FAILED
    try:
        send_packet(PACKET_TIME_REQUEST, b'TIME?')
        print("Time request sent to the TS-7250-V3, waiting for response...")

        deadline = time.time() + LAST_TIME_REQUEST_TIMEOUT
        while time.time() < deadline:
            packet_id, data = read_incoming_packet()
            if packet_id == PACKET_TIME_RESPONSE and data:
                time_str = data.decode('utf-8').strip()
                set_system_time(time_str)
                return
            time.sleep(0.05)
            
        print("WARNING: TIME SYNC TIMED OUT, NO RESPONSE FROM TS-7250-V3")
        LAST_SYNC_FAILED = True
        
    except Exception as e:
        print("Time sync error:", e)
        LAST_SYNC_FAILED = True

# Environmental Functions

def read_environment():
    try:
        raw = Bridge.call("get_environment", "")
        env = json.loads(raw)
        if not isinstance(env, dict):
            raise ValueError("Environment payload is not a JSON object")
        return env
    except Exception as e:
        logger.exception("read_environment failed: %s", e)
        # Fail-safe payload: force doomsday but keep capture/transmit alive.
        return {
            "ok": False,
            "temp_c": None,
            "temp_f": None,
            "humidity": None,
            "lux": None,
            "visible": None,
            "ir": None,
            "full": None,
            "sensor_status": {
                "sht85_ok": False,
                "tsl2591_ok": False,
            },
            "failed_sensors": "ENVIRONMENT_READ_ERROR",
            "doomsday_protocol": True,
        }
    

def normalize_temperature_fields(env):
    temp_c = env.get("temp_c")
    if temp_c is None:
        return env
    expected_temp_f = (temp_c * 9.0 / 5.0) + 32.0
    current_temp_f = env.get("temp_f")
    if current_temp_f is None or abs(current_temp_f - expected_temp_f) > 0.25:
        env["temp_f"] = round(expected_temp_f, 2)
        logger.warning(
            "Adjusted temp_f from %s to %.2f based on temp_c %.2f",
            current_temp_f,
            env["temp_f"],
            temp_c,
        )
    return env
    

def valid_env(env):
        if not env.get("ok", False):
                return False, "ENVIRONMENT_PACKET_DAMAGED"

        if env.get("temp_c") is None:
                return False, "TEMP_MISSING"
        
        if env.get("humidity") is None:
            return False, "HUMIDITY_MISSING"
        
        if env.get("lux") is None:
            return False, "LUX_MISSING"
            
        if env.get("temp_c") < TEMP_MIN_C or env.get("temp_c") > TEMP_MAX_C:
                return False, "TEMP_OUT_OF_ACCEPTABLE_RANGE"

        if env.get("humidity") < HUMIDITY_MIN or env.get("humidity") > HUMIDITY_MAX:
                return False, "HUMIDITY_OUT_OF_ACCEPTABLE_RANGE"

        if env.get("lux") > LUX_MAX:
                return False, "TOO_BRIGHT"

        return True, "VALUES_OK"


def get_failed_sensors(env):
    failed_sensors = []
    sensor_status = env.get("sensor_status") or {}

    # Prefer firmware-level status when available.
    if sensor_status.get("sht85_ok") is False:
        failed_sensors.append("SHT85")
    if sensor_status.get("tsl2591_ok") is False:
        failed_sensors.append("TSL2591")

    firmware_failed = env.get("failed_sensors")
    if isinstance(firmware_failed, str) and firmware_failed.strip():
        for name in firmware_failed.split(","):
            clean_name = name.strip()
            if clean_name and clean_name not in failed_sensors:
                failed_sensors.append(clean_name)
    elif isinstance(firmware_failed, list):
        for name in firmware_failed:
            if isinstance(name, str):
                clean_name = name.strip()
                if clean_name and clean_name not in failed_sensors:
                    failed_sensors.append(clean_name)

    if not env.get("ok", False):
        failed_sensors.append("ENVIRONMENT_PACKET")
    if env.get("temp_c") is None:
        failed_sensors.append("SHT85_TEMP")
    if env.get("humidity") is None:
        failed_sensors.append("SHT85_HUMIDITY")
    if env.get("lux") is None:
        failed_sensors.append("TSL2591_LUX")
    if env.get("visible") is None:
        failed_sensors.append("TSL2591_VISIBLE")
    if env.get("ir") is None:
        failed_sensors.append("TSL2591_IR")
    if env.get("full") is None:
        failed_sensors.append("TSL2591_FULL")
    # De-duplicate while preserving order.
    return list(dict.fromkeys(failed_sensors))

# Heater Functions

def heater_on():
    set_heater("ON")

def heater_off():
    set_heater("OFF")


def set_heater(state: str):
    state = state.strip().upper()

    # Keep command-file write for debug visibility.
    wrote_command = False
    for path in HEATER_CMD_PATHS:
        parent_dir = os.path.dirname(path)
        if parent_dir and not os.path.isdir(parent_dir):
            continue
        try:
            with open(path, "w") as f:
                f.write(state)
            print(f"Heater command {state} written to {path}")
            wrote_command = True
            break
        except Exception as e:
            print(f"WARNING: Could not write {path}: {e}")

    if not wrote_command:
        logger.error("Failed to write heater command file for state %s", state)
        return False

    if not HEATER_DIRECT_APPLY:
        # External heater agent reads heater_cmd.txt and applies CUSBA command.
        return True

    cmd = "1:3" if state == "ON" else "0:3"

    global LAST_HEATER_APPLY_TIME
    now = time.time()
    if (now - LAST_HEATER_APPLY_TIME) < HEATER_APPLY_COOLDOWN_SEC:
        return False

    cusba_candidates = [
        resolve_cusba(),
        "/home/arduino/ArduinoApps/asi_contoll_cer_v5/python/cusba64",
        "/home/arduino/ArduinoApps/asi_controll_Cer_v5/python/cusba64",
        "/home/arduino/ArduinoApps/asi_controller_v5/python/cusba64",
        "/home/arduino/cusba64",
    ]
    usb_candidates = ["ttyUSB0", "ttyUSB1", "ttyUSB2"]

    for cusba in cusba_candidates:
        if not cusba:
            continue
        if not (os.path.isfile(cusba) and os.access(cusba, os.X_OK)):
            continue
        for usb in usb_candidates:
            try:
                result = subprocess.run(
                    [cusba, f"/S:{usb}", cmd],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0:
                    LAST_HEATER_APPLY_TIME = now
                    print(f"Heater {state} OK via {cusba} /S:{usb} {cmd}")
                    if result.stdout.strip():
                        print("CUSBA stdout:", result.stdout.strip())
                    return True
                print(f"Heater {state} failed via {cusba} {usb}: {result.stderr.strip()}")
            except Exception as e:
                print(f"Heater {state} exception via {cusba} {usb}: {e}")

    print(f"WARNING: Heater {state} command failed on all cusba/tty candidates")
    return False

# Camera Function

def capture_image(image_name):
    output_path = "/app/python/" + image_name + ".jpg"
    cap = None
    for port in range(4):
        test_cap = cv2.VideoCapture(port)
        if test_cap.isOpened():
            cap = test_cap
            break
        test_cap.release()
    
    if cap is None or not cap.isOpened():
        print("Camera not accessible")
        return False

    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'GREY'))
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 2592)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1944)
    time.sleep(4) #give camera a buffer to warm up
    
    for _ in range(30):
        ret, frame = cap.read()
        time.sleep(0.05)
    ret, frame = cap.read()
    cap.release()
    
    if ret and frame is not None:
        print("Mean:", frame.mean())
        if len(frame.shape) == 2:
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        
        written = cv2.imwrite(output_path, frame)
        print("Written Sucess:", written)
        print("File exists after write:", os.path.exists(output_path))
        print("Frame shape:", frame.shape)
        return True
        
    else:
        print("Failed to grab frame")
        return False

# Logging Meta Data Functions

def write_pending_metadata(image_name, env, decision):
    txt_path = PENDING_DIR + image_name + ".txt"
    now = datetime.now().strftime("%Y-%m-%d_%H:%M:%S")
        
    with open(txt_path, "w") as f:
        f.write(f"Timestamp: {now}\n")
        f.write(f"Image File: {image_name}\n")
        f.write(f"Decision: {decision}\n\n")
        f.write(f"Environment Values\n")
        f.write(f"OK: {env.get('ok')}\n")                
        f.write(f"Temperature C: {env.get('temp_c')}\n")
        f.write(f"Temperature F: {env.get('temp_f')}\n")
        f.write(f"Humidity: {env.get('humidity')}\n")
        f.write(f"Lux: {env.get('lux')}\n")
        f.write(f"Visible: {env.get('visible')}\n")
        f.write(f"IR: {env.get('ir')}\n")
        f.write(f"Full: {env.get('full')}\n")
    print("Pending metadata written:", txt_path)


def write_doomsday_log(env, failed_sensors):
    now = datetime.now().strftime("%Y-%m-%d_%H:%M:%S")
    txt_path = PENDING_DIR + now + "_doomsday.txt"

    with open(txt_path, "w") as f:
        f.write(f"Timestamp: {now}\n")
        f.write("DOOMSDAY_PROTOCOL: ACTIVE\n")
        f.write(f"Failed Sensors: {', '.join(failed_sensors)}\n\n")
        f.write("Environment Values\n")
        f.write(f"OK: {env.get('ok')}\n")
        f.write(f"Temperature C: {env.get('temp_c')}\n")
        f.write(f"Temperature F: {env.get('temp_f')}\n")
        f.write(f"Humidity: {env.get('humidity')}\n")
        f.write(f"Lux: {env.get('lux')}\n")
        f.write(f"Visible: {env.get('visible')}\n")
        f.write(f"IR: {env.get('ir')}\n")
        f.write(f"Full: {env.get('full')}\n")

    logger.error("DOOMSDAY_PROTOCOL activated. Failed sensors: %s", ", ".join(failed_sensors))
        
def write_skip_log(env, decision):
    now = datetime.now().strftime("%Y-%m-%d_%H:%M:%S")
    txt_path = PENDING_DIR + now + "skip.txt"
    
    with open(txt_path, "w") as f:
        f.write(f"Timestamp: {now}\n")
        f.write(f"Decision: {decision}\n\n")
        f.write(f"Environment Values\n")
        f.write(f"OK: {env.get('ok')}\n")                
        f.write(f"Temperature C: {env.get('temp_c')}\n")
        f.write(f"Temperature F: {env.get('temp_f')}\n")
        f.write(f"Humidity: {env.get('humidity')}\n")
        f.write(f"Lux: {env.get('lux')}\n")
        f.write(f"Visible: {env.get('visible')}\n")
        f.write(f"IR: {env.get('ir')}\n")
        f.write(f"Full: {env.get('full')}\n")
        
    print("Captrue skipped:", decision)

# Clean up function
def cleanup_old_files():
    print("- - Running cleanup - -")
    now = time.time()
    for filename in os.listdir(CAPTURE_DIR):
        if filename in PROTECTED_FILES:
            continue
        if filename.endswith('.jpg') or filename.endswith('.txt'):
            filepath = os.path.join(CAPTURE_DIR, filename)
            if now - os.path.getmtime(filepath) > MAX_FILE_AGE_SEC:
                try:
                    os.remove(filepath)
                    print(f"Deleted: {filename}")
                except Exception as e:
                    print(f"Cleanup error on {filename}:", e)

# Transmit Function
def transmit_data(image_path, env, doomsday_active=False, failed_sensors=None):
    global LAST_SYNC_FAILED
    if failed_sensors is None:
        failed_sensors = []
    try:
        # Time sync status
        env_to_send = dict(env)
        env_to_send['time_sync_ok'] = not LAST_SYNC_FAILED
        if doomsday_active:
            env_to_send['doomsday_protocol_active'] = True
            env_to_send['failed_sensors'] = failed_sensors
            env_to_send['alert_team'] = True
        
        # Send env packet
        env_bytes = json.dumps(env_to_send).encode('utf-8')
        send_packet(PACKET_ENV, env_bytes)
        print("Environment packet sent")
        time.sleep(0.1)
        
        # Packet 0x02 Image file size then its chunks
        if image_path and os.path.exists(image_path):
            with open(image_path, 'rb') as f:
                image_data = f.read()

            file_size = len(image_data)
            print(f"Image size packet sent: {file_size} bytes")

            # Send file-size header as its own 'IMAGE' packet so the reciever can pre-allocate a buffer 
            # Utalizing 4 byte big-endian unsigned int.
            size_payload = struct.pack('>I', file_size)
            send_packet(PACKET_IMAGE, size_payload)
            print(f"Image size packet sent: {file_size} bytes")
            time.sleep(0.1)
            
            # Send in 200 byte chunks
            chunk_size = 200
            offset = 0
            chunk_num = 0
            while offset < file_size:
                chunk = image_data[offset:offset + chunk_size]
                send_packet(PACKET_IMAGE, chunk)
                offset += len(chunk)
                chunk_num += 1
                print(f"Chunk {chunk_num} sent, {offset}/{file_size} bytes")
                time.sleep(0.02)
        else:
            print("No image to transmit, sending ENV only")
    
        # Packet 0x03 Send Done signal
        send_packet(PACKET_DONE, json.dumps({"done": True}).encode('utf-8'))
        print("Transfer complete")

    except Exception as e:
        logger.exception("Transmit error: %s", e)

# Main Cycle

def run_cycle():
    # Check Environment
    env = read_environment()
    env = normalize_temperature_fields(env)
    print("ENV =", env)

    # Heater control
    temp = env.get("temp_c")
    humidity = env.get("humidity")

    heater_state = "LOW_POWER_MODE"
    try:
        if temp is not None and humidity is not None and (
            temp < HEATER_TEMP_THRESHOLD_C or humidity > HEATER_HUMIDITY_THRESHOLD
        ):
            heater_on()
            heater_state = "HEATER_ON"
        elif temp is not None and humidity is not None:
            heater_off()
            heater_state = "HEATER_OFF"
        else:
            logger.warning("Skipping heater command because sensor data is missing")
            heater_state = "LOW_POWER_MODE"
    except subprocess.TimeoutExpired:
        logger.warning("Heater command timeout")
        heater_state = "HEATER_COMMAND_TIMEOUT"
    except Exception as e:
        logger.exception("Heater error: %s", e)
        heater_state = "HEATER_ERROR"
        

    failed_sensors = get_failed_sensors(env)
    doomsday_active = len(failed_sensors) > 0

    try:
        ok_to_capture, decision = valid_env(env)
    except Exception as e:
        logger.exception("valid_env failed: %s", e)
        ok_to_capture, decision = False, "VALID_ENV_EXCEPTION"
    if doomsday_active:
        write_doomsday_log(env, failed_sensors)
        ok_to_capture = True
        decision = f"DOOMSDAY_PROTOCOL_ACTIVE:{','.join(failed_sensors)}"
    
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    image_name = f"image_{timestamp}"

    if doomsday_active:
        cycle_mode = "DOOMSDAY_CAPTURE"
    elif ok_to_capture:
        cycle_mode = "GOOD_CAPTURE"
    else:
        cycle_mode = "BAD_CONDITIONS_SKIP"

    env["heater_state"] = heater_state
    env["cycle_mode"] = cycle_mode
    
    if ok_to_capture:
        if not doomsday_active:
            write_pending_metadata(image_name, env, "IMAGE_REQUESTED")
        success = capture_image(image_name)
        if success:
            if not doomsday_active:
                write_pending_metadata(image_name, env, "IMAGE_CAPTURED")
            image_path = CAPTURE_DIR + image_name + ".jpg"
            print("transmitting to TS-7250-V3")
            transmit_data(image_path, env, doomsday_active, failed_sensors)
        else:
            write_skip_log(env, "CAMERA_FAILED")
            print("transmitting to TS-7250-V3")
            transmit_data(None, env, doomsday_active, failed_sensors)
    else:
        env["bad_conditions_reason"] = decision
        write_skip_log(env, f"BAD_CONDITIONS_SKIP:{decision}")
        print("transmitting to TS-7250-V3")
        transmit_data(None, env, doomsday_active, failed_sensors)

def loop():
    global ctr
    
    try:
        ctr += 1
        print(f"Cycle {ctr}/{TIME_SYNC_INTERVAL}")

        if ctr >= TIME_SYNC_INTERVAL:
            print("--Requesting time sync fom TS-7250-V3--")
            request_time_sync()
            ctr = 0
        
        if ctr % CLEANUP_INTERVAL == 0:
            cleanup_old_files()
        
        run_cycle()
        
    except Exception as e:
        logger.exception("Python error: %s", e)
        
    print(f"Entering low power wait for {MIN_CAPTURE_INTERVAL_SEC} seconds...")
    time.sleep(MIN_CAPTURE_INTERVAL_SEC)


App.run(user_loop=loop)
