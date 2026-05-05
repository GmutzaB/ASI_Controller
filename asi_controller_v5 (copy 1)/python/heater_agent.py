#!/usr/bin/env python3
"""
Low-power heater relay agent.

Runs outside AppLab, watches heater_cmd.txt, and applies ON/OFF changes
to the USB relay using cusba64.
"""

import os
import shutil
import subprocess
import time
from datetime import datetime

CMD_PATHS = [
    "/home/arduino/ArduinoApps/asi_controller_v5/python/heater_cmd.txt",
    "/app/python/heater_cmd.txt",
]

LOG_PATHS = [
    "/home/arduino/ArduinoApps/asi_controller_v5/python/heater_agent.log",
    "/tmp/heater_agent.log",
]

CUSBA_CANDIDATES = [
    "/home/arduino/ArduinoApps/asi_controller_v5/python/cusba64",
    "/app/python/cusba64",
    "/home/arduino/cusba64",
    "cusba64",
]

USB_CANDIDATES = ["ttyUSB0", "ttyUSB1", "ttyUSB2"]
POLL_SEC = 2.0
STATE_FILE = "/tmp/heater_agent_last_command.txt"


def pick_existing_path(paths):
    for path in paths:
        parent = os.path.dirname(path)
        if os.path.isdir(parent):
            return path
    return paths[0]


CMD_FILE = pick_existing_path(CMD_PATHS)
LOG_FILE = pick_existing_path(LOG_PATHS)


def log(msg):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts}Z {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def resolve_cusba():
    for candidate in CUSBA_CANDIDATES:
        if "/" in candidate:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return None


def read_cmd():
    try:
        with open(CMD_FILE, "r") as f:
            return f.read().strip().upper()
    except Exception:
        return None


def read_last_command():
    try:
        with open(STATE_FILE, "r") as f:
            value = f.read().strip().upper()
            if value in ("ON", "OFF"):
                return value
    except Exception:
        pass
    return None


def write_last_command(value):
    try:
        with open(STATE_FILE, "w") as f:
            f.write(value)
    except Exception as e:
        log(f"WARNING: failed to write state file: {e}")


def apply_command(cmd, cusba):
    relay_cmd = "1:3" if cmd == "ON" else "0:3"
    for usb in USB_CANDIDATES:
        try:
            result = subprocess.run(
                [cusba, f"/S:{usb}", relay_cmd],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                log(f"Applied {cmd} via {cusba} /S:{usb} {relay_cmd}")
                return True
            stderr = result.stderr.strip() if result.stderr else "(no stderr)"
            log(f"Relay apply failed on {usb}: {stderr}")
        except Exception as e:
            log(f"Relay apply exception on {usb}: {e}")
    return False


def ensure_cmd_file_exists():
    parent = os.path.dirname(CMD_FILE)
    os.makedirs(parent, exist_ok=True)
    if not os.path.exists(CMD_FILE):
        with open(CMD_FILE, "w") as f:
            f.write("OFF")
        log(f"Created {CMD_FILE} with OFF")


def main():
    log("heater_agent starting")
    ensure_cmd_file_exists()

    cusba = resolve_cusba()
    if not cusba:
        log("ERROR: cusba64 not found or not executable")
        return 1

    log(f"Using command file: {CMD_FILE}")
    log(f"Using cusba: {cusba}")

    last_seen = read_last_command()
    last_mtime = 0.0

    while True:
        try:
            mtime = os.path.getmtime(CMD_FILE)
            if mtime != last_mtime:
                last_mtime = mtime
                cmd = read_cmd()
                if cmd not in ("ON", "OFF"):
                    log(f"Ignoring invalid command: {cmd}")
                    time.sleep(POLL_SEC)
                    continue
                if cmd == last_seen:
                    time.sleep(POLL_SEC)
                    continue

                if apply_command(cmd, cusba):
                    last_seen = cmd
                    write_last_command(cmd)
                else:
                    log(f"WARNING: failed to apply command {cmd}")
        except Exception as e:
            log(f"Loop error: {e}")

        time.sleep(POLL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
