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

## Patching in Aurora
Patch the logo as 9 RGB fixtures with 6 channels each: R, G, B, (unused), dimmer, strobe. Fixtures 1–8 are the
letters and dots (B, dot1, O1, dot2, O2, dot3, M, dot4), fixture 9 is the whole tree (crown, trunk and roots). They
start at `DMX_START_ADDRESS` (default 1), so fixture n is on channels 6n−5…6n. Optionally patch the logo control
fixture right after them (channels 55–58 with start address 1: strobe variant, beat, texture, hit) and set
`CONTROL_FIXTURE=true`.

The script sends a frame only when it changes, at most ~40 per second, and resends the last one every second. When
Aurora disconnects it stops sending, and the logo falls back to its scene after 5 seconds.
