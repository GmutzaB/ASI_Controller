#!/bin/bash
CUSBA="/home/arduino/ArduinoApps/asi_controller_v5/python/cusba64"
FILE=$"/home/arduino/ArduinoApps/asi_controller_v5/python/heater_cmd.txt"

chmod 666 "$FILE" 2>/dev/null
CMD=$(cat "$FILE" 2>/dev/null)
chmod 666 "$FILE" 2>/dev/null

if [ "$CMD" = "ON" ]; then
	$CUSBA /S:ttyUSB0 1:3
elif [ "$CMD" = "OFF" ]; then
	$CUSBA /S:ttyUSB0 0:3
fi
