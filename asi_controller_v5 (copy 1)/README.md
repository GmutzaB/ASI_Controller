# ASI Controller v6

Integrated all-sky imaging system for low-light and ultra low power polar operation.

## System Components

- `python/main.py` - App runtime loop for environment read, doomsday logic, image capture, and metadata transmit.
- `sketch/sketch.ino` - Arduino firmware for SHT85 + TSL2591 sensor reads and Bridge API responses.
- `python/heater_agent.py` - Host-side relay controller using `cusba64`.
- `python/heater_cmd_sync.py` - Sync bridge from AppLab-visible heater command state to host command state.
- `python/lux_algorithm.py` - Standalone daylight/filter/lux modeling utility.

## Heater Control Flow

1. App runtime decides heater state (`ON`/`OFF`) and writes command intent.
2. `heater_cmd_sync.py` mirrors command state to `/home/arduino/heater_cmd.txt` when needed.
3. `heater_agent.py` watches host command file and applies relay commands with `cusba64`.

## Services (host)

- `python/heater-agent.service`
- `python/heater-cmd-sync.service`

Install service units in `/etc/systemd/system/`, then use:

```bash
sudo systemctl daemon-reload
sudo systemctl enable heater-agent.service heater-cmd-sync.service
sudo systemctl restart heater-agent.service heater-cmd-sync.service
```

## Quick Troubleshooting

- Confirm command files update as expected (`/app/python/heater_cmd.txt` and `/home/arduino/heater_cmd.txt`).
- Confirm both services are active (`systemctl status ...`).
- Confirm manual relay command works as `arduino` user.
