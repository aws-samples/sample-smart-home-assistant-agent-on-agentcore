---
name: fan-control
description: Control the smart fan - set power on/off, speed (0-3), and oscillation on/off
allowed-tools: device_control
---
# Fan Control

You control a smart fan. Available commands:

## Power Control
- Turn on: device_type="fan", command={"action": "setPower", "power": true}
- Turn off: device_type="fan", command={"action": "setPower", "power": false}

## Speed Control
- Set speed: device_type="fan", command={"action": "setSpeed", "speed": <0-3>}
- Speed 0: Off
- Speed 1: Low
- Speed 2: Medium
- Speed 3: High

## Oscillation
- Enable oscillation: device_type="fan", command={"action": "setOscillation", "oscillation": true}
- Disable oscillation: device_type="fan", command={"action": "setOscillation", "oscillation": false}

Always confirm the action taken to the user. When setting speed, mention the level name (low, medium, high).
