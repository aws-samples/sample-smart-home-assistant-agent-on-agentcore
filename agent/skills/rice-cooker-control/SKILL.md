---
name: rice-cooker-control
description: Control the smart rice cooker - start cooking with different modes (white_rice, brown_rice, porridge, steam), stop cooking, and toggle keep warm
allowed-tools: device_control
---
# Rice Cooker Control

You control a smart rice cooker. Available commands:

## Start Cooking
- Start with mode: device_type="rice_cooker", command={"action": "start", "mode": "<mode>"}
- Available modes: white_rice, brown_rice, porridge, steam

## Stop Cooking
- Stop: device_type="rice_cooker", command={"action": "stop"}

## Keep Warm
- Enable keep warm: device_type="rice_cooker", command={"action": "keepWarm", "enabled": true}
- Disable keep warm: device_type="rice_cooker", command={"action": "keepWarm", "enabled": false}

Always confirm the action taken to the user. When starting a cooking mode, let the user know which mode was selected.
