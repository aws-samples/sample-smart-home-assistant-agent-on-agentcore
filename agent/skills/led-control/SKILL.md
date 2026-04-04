---
name: led-control
description: Control the Govee LED Matrix - set modes (rainbow, breathing, chase, sparkle, fire, ocean, aurora), brightness (0-100), power on/off, and static colors
allowed-tools: device_control
---
# LED Matrix Control

You control a Govee RGBIC LED Matrix panel. Available commands:

## Power Control
- Turn on: device_type="led_matrix", command={"action": "setPower", "power": true}
- Turn off: device_type="led_matrix", command={"action": "setPower", "power": false}

## Mode Selection
- Set mode: device_type="led_matrix", command={"action": "setMode", "mode": "<mode>"}
- Available modes: rainbow, breathing, chase, sparkle, fire, ocean, aurora

## Brightness
- Set brightness: device_type="led_matrix", command={"action": "setBrightness", "brightness": <0-100>}

## Static Color
- Set color: device_type="led_matrix", command={"action": "setColor", "color": "#RRGGBB"}

Always confirm the action taken to the user.
