#!/usr/bin/env python3
"""
Mirror heater command from AppLab-visible path to host path.

Source (container/AppLab side): /app/python/heater_cmd.txt
Destination (host/systemd side): /home/arduino/heater_cmd.txt
"""

import os
import time
from datetime import datetime

SRC_PATH = "/app/python/heater_cmd.txt"
DST_PATH = "/home/arduino/heater_cmd.txt"
STATE_PATH = "/tmp/heater_cmd_sync_last.txt"
LOG_PATH = "/tmp/heater_cmd_sync.log"
POLL_SEC = 1.0


def log(msg):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts}Z {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def read_cmd(path):
    try:
        with open(path, "r") as f:
            value = f.read().strip().upper()
            if value in ("ON", "OFF"):
                return value
    except Exception:
        return None
    return None


def write_cmd(path, value):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as f:
        f.write(value)


def read_last_state():
    return read_cmd(STATE_PATH)


def write_last_state(value):
    try:
        with open(STATE_PATH, "w") as f:
            f.write(value)
    except Exception as e:
        log(f"WARNING: could not write state file: {e}")


def main():
    log("heater_cmd_sync starting")
    last = read_last_state()

    while True:
        cmd = read_cmd(SRC_PATH)
        if cmd and cmd != last:
            try:
                write_cmd(DST_PATH, cmd)
                write_last_state(cmd)
                last = cmd
                log(f"Synced {cmd} from {SRC_PATH} -> {DST_PATH}")
            except Exception as e:
                log(f"ERROR: failed syncing command {cmd}: {e}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
