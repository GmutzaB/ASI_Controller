# ASI Controller Python Scripts

This folder contains the Python-side logic for ASI controller operations.

## Key Files

- `main.py` - Main AppLab runtime loop (sensor read, doomsday handling, capture/transmit flow).
- `heater_agent.py` - Host-side relay controller that reads heater command state and applies `cusba64` commands.
- `heater_cmd_sync.py` - Bridge process that mirrors AppLab command state to host-visible heater command file.
- `lux_algorithm.py` - Standalone daylight/filter/lux modeling and TSL2591 gain recommendation tool.
  - This calculation program is meant only for early approximations/estimations.
  - It is designed to help with sensor/filter calibration planning and validation.

## Heater Control Architecture

- App runtime writes heater intent (`ON`/`OFF`) to command file(s).
- `heater_cmd_sync.py` can mirror command state from AppLab-visible path to host path.
- `heater_agent.py` reads host command state and issues USB relay commands via `cusba64`.

## Services

- `heater-agent.service` - Runs `heater_agent.py` under systemd.
- `heater-cmd-sync.service` - Runs `heater_cmd_sync.py` under systemd.

## Notes

- Keep host paths and AppLab-visible paths aligned with your deployment.
- AppLab/container paths (for example `/app/python/...`) may not be visible from host `systemd` services.
- Use `heater_cmd_sync.py` to bridge command state into `/home/arduino/heater_cmd.txt` for host-side relay control.
- When troubleshooting heater behavior, verify:
  - command file value
  - sync service status/logs
  - heater agent status/logs
  - manual `cusba64` command works as `arduino` user
