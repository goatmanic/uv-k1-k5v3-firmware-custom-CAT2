# K5 Qt Viewer

Qt-based live screen viewer and byte logger for F4HWN screenshot stream.

## Features

- Displays live 128x64 radio screen from UART/USB screenshot stream
- Uses keepalive frame `55 AA 00 00` to request streaming
- Two scrolling log windows:
  - RX bytes (radio -> PC)
  - TX bytes (PC -> radio)
- Hex dump per packet with timestamp
- Controls for color theme, pixel scaling, and log clearing

## Install

```bash
python3 -m pip install pyserial PySide6
```

## Run

```bash
python3 tools/qtviewer/k5qtviewer.py --list-ports
python3 tools/qtviewer/k5qtviewer.py --port /dev/ttyUSB0
```

Windows example:

```bash
python tools/qtviewer/k5qtviewer.py --port COM3
```
