#imports
import json
import time
import os
import cv2
import base64
import serial
import struct
import subprocess
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

#"/home/arduino/ArduinoApps/asi_controller_v5/python/cusba64"

print("=== SAVING FILES TO:", CAPTURE_DIR, "===")

os.makedirs(CAPTURE_DIR, exist_ok = True)
os.makedirs(PENDING_DIR, exist_ok = True)

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

    data = raw[600000:6 + length]
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
    raw = Bridge.call("get_environment", "")
    return json.loads(raw)
    

def valid_env(env):
        if not env.get("ok", False):
                return False, "ENVIRONMENT_PACKET_DAMAGED"

        if env["temp_c"] is None:
                return False, "TEMP_MISSING"
        
        if env["humidity"] is None:
            return False, "HUMIDITY_MISSING"
        
        if env["lux"] is None:
            return False, "LUX_MISSING"
            
        if env["temp_c"] < TEMP_MIN_C or env["temp_c"] > TEMP_MAX_C:
                return False, "TEMP_OUT_OF_ACCEPTABLE_RANGE"

        if env["humidity"] < HUMIDITY_MIN or env["humidity"] > HUMIDITY_MAX:
                return False, "HUMIDITY_OUT_OF_ACCEPTABLE_RANGE"

        if env["lux"] > LUX_MAX:
                return False, "TOO_BRIGHT"

        return True, "VALUES_OK"

# Heater Functions

def heater_on():
    import os
    print("debug CWD:", os.getcwd())
    print("Debug writing to /app/python/heater_cmd.txt")
    with open("/app/python/heater_cmd.txt", "w") as f:
        f.write("ON")
    print("Heater ON command sent")

def heater_off():
    with open("/app/python/heater_cmd.txt", "w") as f:
        f.write("OFF")
    print("Heater OFF command sent")

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
    
    if not cap.isOpened():
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
def transmit_data(image_path, env):
    global LAST_SYNC_FAILED
    try:
        # Time sync status
        env_to_send = dict(env)
        env_to_send['time_sync_ok'] = not LAST_SYNC_FAILED
        
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
        print("Transmit error:", e)

# Main Cycle

def run_cycle():
    # Check Environment
    env = read_environment()
    print("ENV =", env)

    # Heater control
    temp = env.get("temp_c")
    humidity = env.get("humidity")

    try:
        if temp < HEATER_TEMP_THRESHOLD_C or humidty > HEATER_HUMIDITY_THRESHOLD:
            heater_on()
        else:
            heater_off()
    except subprocess.TimeoutExpired:
        print("WARNING: Heater comman timeout")
    except Exception as e:
        print("WARNING: Heater error:", e)
        

    
    ok_to_capture, decision = valid_env(env)
    
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    image_name = f"image_{timestamp}"
    
    if ok_to_capture:
        write_pending_metadata(image_name, env, "IMAGE_REQUESTED")
        success = capture_image(image_name)
        if success:
            write_pending_metadata(image_name, env, "IMAGE_CAPTURED")
            image_path = CAPTURE_DIR + image_name + ".jpg"
            print("transmitting to TS-7250-V3")
            transmit_data(image_path, env)
        else:
            write_skip_log(env, "CAMERA_FAILED")
            print("transmitting to TS-7250-V3")
            transmit_data(None, env)
    else:
        write_skip_log(env, decision)
        print("transmitting to TS-7250-V3")
        transmit_data(None, env)

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
        print("Python error:", e)
        
    print(f"Entering low power wait for {MIN_CAPTURE_INTERVAL_SEC} seconds...")
    time.sleep(MIN_CAPTURE_INTERVAL_SEC)


App.run(user_loop=loop)