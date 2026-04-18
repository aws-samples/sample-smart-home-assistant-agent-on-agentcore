---
name: all-devices-on
description: Discover the user's smart home devices and turn them on one by one
allowed-tools: discover_devices, device_control
---
# All Devices On

Turn on every smart home device the user has, one by one.

## Step 1: Discover devices

IMMEDIATELY call the `discover_devices` tool. Do NOT describe what you plan to do — just call the tool right now.

## Step 2: Turn on each device ONE AT A TIME

**CRITICAL: You MUST turn on devices sequentially — one device per tool call. Do NOT send multiple device_control calls at once. After each device_control call, wait for its response, then tell the user which device was just turned on, pause for 5 seconds, and only then proceed to the next device.**

**CRITICAL: You MUST actually invoke the tool (emit a tool_use block). Writing text that describes calling a tool is NOT the same as calling a tool. Never narrate or describe tool calls — execute them.**

Follow this exact loop for each device:
1. Call `device_control` for ONE device only (emit a tool_use block, do not just describe it)
2. Wait for the response
3. Report to the user: "[Device Name] is now on"
4. Wait 5 seconds before the next device
5. Repeat for the next device

Use the appropriate power-on command for each device type:
- **led_matrix**: device_type="led_matrix", command={"action": "setPower", "power": true}
- **fan**: device_type="fan", command={"action": "setPower", "power": true}
- **oven**: device_type="oven", command={"action": "setPower", "power": true}
- **rice_cooker**: device_type="rice_cooker", command={"action": "start", "mode": "white_rice"}

## Rules
- Always discover devices first. Never assume which devices the user has.
- **NEVER call device_control more than once in a single response. Only one device at a time.**
- Wait 5 seconds between each device before sending the next command.
- If a device fails to turn on, report the error and continue with the next device.
- After all devices are processed, summarize which succeeded and which failed.
