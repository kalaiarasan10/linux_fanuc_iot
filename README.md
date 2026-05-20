# FANUC IoT Edge — Linux Edition

Runs the FANUC CNC data collector on Linux (Raspberry Pi / Ubuntu).
Uses `libfwlib32.so` via ctypes — no chattertools needed.

## Supported Platforms
- Raspberry Pi OS (armv7) → `lib/libfwlib32.so`
- Ubuntu x64 → replace with `libfwlib32-linux-x64.so`

## Quick Install (Raspberry Pi / Ubuntu)
```bash
sudo bash install.sh
```

## Manual Run (WSL / Dev)
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python src/manager.py
```

## GUI
Open browser: `http://<device-ip>:8765`

## Logs
```bash
journalctl -u fanuciotedge -f
```

## .so File Location
`lib/libfwlib32.so` — FANUC Focas2 Linux shared library
