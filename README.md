# Aurora Lights Proxy (B.O.O.M. logo)

This script forwards DMX packets from the [Aurora core](https://github.com/gewis/narrowcasting-core) to the B.O.O.M.
LED logo over MQTT. It connects to the SocketIO server and listens for DMX packets, turns the logo's channels into
live frames and publishes them on `boom-logo/live`. It also publishes its own status on `boom-logo/bridge/status`, so
the logo's website can show whether Aurora is connected.

The frame format and the rules for sending them are in the "Live source (Aurora)" section of `protocol/PROTOCOL.md`
in the logo repository.

## Prerequisites
- Python 3.9 or newer.
- The logo's MQTT broker, and the password of its `bridge` user.

## Installation
- Create a virtual environment `python -m venv venv`.
- Activate the virtual environment `./venv/Scripts/activate.bat` or `./venv/Scripts/activate`.
- Install requirements `pip install -r requirements.txt`.
- Copy `.env.example` to `.env` and set the Aurora URL and API key, and the MQTT broker and password.
- Start the script `python main.py`.

## Docker
Next to the logo's `deploy/` stack on the same host, the proxy joins its network (`boom-logo_default`) and reaches
the broker as `mosquitto`. Copy `.env.example` to `.env`, set `MQTT_HOST=mosquitto` and the bridge password, then
`docker compose up -d --build`.

## Patching in Aurora
Patch the logo as 9 RGB fixtures with 6 channels each: R, G, B, (unused), dimmer, strobe, laid out as a 9×2 grid:
the letters and dots on row y0 (x0–x8, x4 empty) and the whole tree (crown, trunk and roots) on row y1 at x4.
Fixture n is at x = n−1: 1 B, 2 dot1, 3 O1, 4 dot2, 5 tree, 6 O2, 7 dot3, 8 M, 9 dot4. They start at
`DMX_START_ADDRESS` (default 1), so fixture n is on channels 6n−5…6n.

The script sends a frame only when it changes, at most ~40 per second, and resends the last one every second. When
Aurora disconnects, or sends no DMX packets for 15 minutes, it stops sending, and the logo falls back to its scene
after 5 seconds. Frames start again with the next DMX packet.
