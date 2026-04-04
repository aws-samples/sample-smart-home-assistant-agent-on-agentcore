---
name: oven-control
description: Control the smart oven - set power on/off, cooking mode (bake, broil, convection), and temperature (200-500 F)
allowed-tools: device_control
---
# Oven Control

You control a smart oven. Available commands:

## Power Control
- Turn on: device_type="oven", command={"action": "setPower", "power": true}
- Turn off: device_type="oven", command={"action": "setPower", "power": false}

## Mode Selection
- Set mode: device_type="oven", command={"action": "setMode", "mode": "<mode>"}
- Available modes: bake, broil, convection

## Temperature Control
- Set temperature: device_type="oven", command={"action": "setTemperature", "temperature": <200-500>}
- Temperature range: 200-500 degrees Fahrenheit

Always confirm the action taken to the user. When setting temperature, include the value in degrees Fahrenheit. Warn the user if they request a temperature outside the 200-500 F range.
