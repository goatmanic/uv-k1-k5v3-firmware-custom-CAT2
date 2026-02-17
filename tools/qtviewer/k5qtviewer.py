#!/usr/bin/env python3
"""Qt-based UART/USB screenshot viewer for UV-K5 F4HWN firmware.

Features:
- Safe parser for screenshot diff frames
- Keepalive sender (0x55 0xAA 0x00 0x00)
- Live byte-level TX/RX logging windows
- LCD-like 128x64 screen renderer
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from dataclasses import dataclass

from PySide6 import QtCore, QtGui, QtWidgets
import serial
from serial.tools import list_ports

WIDTH = 128
HEIGHT = 64
FRAME_SIZE = 1024
KEEPALIVE = b"\x55\xAA\x00\x00"
HEADER = b"\xAA\x55"
TYPE_SCREENSHOT = 0x01
TYPE_DIFF = 0x02


@dataclass
class Colors:
    fg: QtGui.QColor
    bg: QtGui.QColor


class ScreenWidget(QtWidgets.QWidget):
    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._frame = bytearray(FRAME_SIZE)
        self._scale = 4
        self._colors = Colors(fg=QtGui.QColor(0, 0, 0), bg=QtGui.QColor(202, 202, 202))
        self.setMinimumSize(WIDTH * self._scale, HEIGHT * self._scale)

    def set_frame(self, frame: bytearray) -> None:
        if len(frame) == FRAME_SIZE:
            self._frame[:] = frame
            self.update()

    def set_scale(self, scale: int) -> None:
        self._scale = max(2, min(12, scale))
        self.setMinimumSize(WIDTH * self._scale, HEIGHT * self._scale)
        self.updateGeometry()
        self.update()

    def set_colors(self, colors: Colors) -> None:
        self._colors = colors
        self.update()

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(WIDTH * self._scale, HEIGHT * self._scale)

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802
        del event
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), self._colors.bg)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(self._colors.fg)

        bit_index = 0
        pixel_w = max(1, self._scale - 1)
        pixel_h = self._scale
        for y in range(HEIGHT):
            py = y * self._scale
            for x in range(WIDTH):
                byte_idx = bit_index // 8
                bit_pos = bit_index % 8
                if (self._frame[byte_idx] >> bit_pos) & 1:
                    p.drawRect(x * (self._scale - 1), py, pixel_w, pixel_h)
                bit_index += 1


class K5Receiver(QtCore.QObject):
    frame_ready = QtCore.Signal(bytearray)
    status = QtCore.Signal(str)
    rx_log = QtCore.Signal(bytes)
    tx_log = QtCore.Signal(bytes)

    def __init__(self, port: str, baud: int = 38400, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._serial = serial.Serial(port, baud, timeout=0)
        self._buffer = bytearray()
        self._frame = bytearray(FRAME_SIZE)
        self._keepalive_timer = QtCore.QTimer(self)
        self._keepalive_timer.timeout.connect(self.send_keepalive)
        self._keepalive_timer.start(120)
        self._poll_timer = QtCore.QTimer(self)
        self._poll_timer.timeout.connect(self.poll)
        self._poll_timer.start(10)

    def close(self) -> None:
        self._keepalive_timer.stop()
        self._poll_timer.stop()
        if self._serial.is_open:
            self._serial.close()

    def send_keepalive(self) -> None:
        if not self._serial.is_open:
            return
        try:
            self._serial.write(KEEPALIVE)
            self.tx_log.emit(KEEPALIVE)
        except serial.SerialException as exc:
            self.status.emit(f"TX error: {exc}")

    def poll(self) -> None:
        if not self._serial.is_open:
            return
        try:
            waiting = self._serial.in_waiting
            if waiting:
                data = self._serial.read(waiting)
                if data:
                    self.rx_log.emit(data)
                    self._buffer.extend(data)
                    self._consume_buffer()
        except serial.SerialException as exc:
            self.status.emit(f"RX error: {exc}")

    def _consume_buffer(self) -> None:
        while True:
            if len(self._buffer) < 5:
                return

            hdr = self._buffer.find(HEADER)
            if hdr < 0:
                # keep tail for split headers
                if len(self._buffer) > 1:
                    self._buffer[:] = self._buffer[-1:]
                return
            if hdr > 0:
                del self._buffer[:hdr]
                if len(self._buffer) < 5:
                    return

            msg_type = self._buffer[2]
            size = (self._buffer[3] << 8) | self._buffer[4]
            total = 5 + size
            if len(self._buffer) < total:
                return

            payload = bytes(self._buffer[5:total])
            del self._buffer[:total]

            if msg_type == TYPE_SCREENSHOT and size == FRAME_SIZE:
                self._frame[:] = payload
                self.frame_ready.emit(bytearray(self._frame))
                self.status.emit("Full frame received")
            elif msg_type == TYPE_DIFF and size % 9 == 0:
                self._apply_diff(payload)
                self.frame_ready.emit(bytearray(self._frame))
                self.status.emit(f"Diff frame received ({size // 9} chunks)")
            else:
                self.status.emit(f"Ignored frame type=0x{msg_type:02X} size={size}")

    def _apply_diff(self, payload: bytes) -> None:
        i = 0
        while i + 9 <= len(payload):
            block = payload[i]
            i += 1
            if block >= 128:
                break
            self._frame[block * 8:block * 8 + 8] = payload[i:i + 8]
            i += 8


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, port: str, baud: int) -> None:
        super().__init__()
        self.setWindowTitle("K5 Qt Viewer + Byte Logger")
        self.resize(1300, 800)

        central = QtWidgets.QWidget()
        root = QtWidgets.QHBoxLayout(central)

        left = QtWidgets.QVBoxLayout()
        right = QtWidgets.QVBoxLayout()
        root.addLayout(left, 3)
        root.addLayout(right, 2)

        self.screen = ScreenWidget()
        left.addWidget(self.screen)

        controls = QtWidgets.QHBoxLayout()
        left.addLayout(controls)

        self.scale = QtWidgets.QSpinBox()
        self.scale.setRange(2, 12)
        self.scale.setValue(4)
        self.scale.valueChanged.connect(self.screen.set_scale)
        controls.addWidget(QtWidgets.QLabel("Scale"))
        controls.addWidget(self.scale)

        self.theme = QtWidgets.QComboBox()
        self.theme.addItems(["Grey", "Orange", "Blue", "White", "Invert"])
        self.theme.currentTextChanged.connect(self._on_theme_changed)
        controls.addWidget(QtWidgets.QLabel("Theme"))
        controls.addWidget(self.theme)

        self.clear_logs_btn = QtWidgets.QPushButton("Clear Logs")
        controls.addWidget(self.clear_logs_btn)

        self.status_lbl = QtWidgets.QLabel("Starting...")
        left.addWidget(self.status_lbl)

        self.rx_box = QtWidgets.QPlainTextEdit()
        self.tx_box = QtWidgets.QPlainTextEdit()
        for box in (self.rx_box, self.tx_box):
            box.setReadOnly(True)
            box.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
            font = QtGui.QFont("Courier New")
            font.setStyleHint(QtGui.QFont.Monospace)
            box.setFont(font)

        right.addWidget(QtWidgets.QLabel("RX Bytes (radio -> PC)"))
        right.addWidget(self.rx_box)
        right.addWidget(QtWidgets.QLabel("TX Bytes (PC -> radio)"))
        right.addWidget(self.tx_box)

        self.setCentralWidget(central)

        self.receiver = K5Receiver(port=port, baud=baud, parent=self)
        self.receiver.frame_ready.connect(self.screen.set_frame)
        self.receiver.status.connect(self.status_lbl.setText)
        self.receiver.rx_log.connect(lambda b: self._append_bytes(self.rx_box, b, "RX"))
        self.receiver.tx_log.connect(lambda b: self._append_bytes(self.tx_box, b, "TX"))
        self.clear_logs_btn.clicked.connect(self._clear_logs)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        self.receiver.close()
        super().closeEvent(event)

    def _clear_logs(self) -> None:
        self.rx_box.clear()
        self.tx_box.clear()

    def _append_bytes(self, box: QtWidgets.QPlainTextEdit, data: bytes, tag: str) -> None:
        ts = dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        hexline = " ".join(f"{x:02X}" for x in data)
        box.appendPlainText(f"[{ts}] {tag} {len(data):3d}B | {hexline}")
        box.verticalScrollBar().setValue(box.verticalScrollBar().maximum())

    def _on_theme_changed(self, theme: str) -> None:
        if theme == "Grey":
            colors = Colors(QtGui.QColor(0, 0, 0), QtGui.QColor(202, 202, 202))
        elif theme == "Orange":
            colors = Colors(QtGui.QColor(0, 0, 0), QtGui.QColor(255, 193, 37))
        elif theme == "Blue":
            colors = Colors(QtGui.QColor(0, 0, 0), QtGui.QColor(28, 134, 228))
        elif theme == "White":
            colors = Colors(QtGui.QColor(0, 0, 0), QtGui.QColor(255, 255, 255))
        else:
            colors = Colors(QtGui.QColor(202, 202, 202), QtGui.QColor(0, 0, 0))
        self.screen.set_colors(colors)


def cmd_list_ports() -> int:
    print("Available serial ports:")
    for p in list_ports.comports():
        desc = " - ".join(filter(None, (p.product, p.manufacturer)))
        if desc:
            print(f"  {p.device}: {desc}")
        else:
            print(f"  {p.device}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Qt screen viewer and UART logger for UV-K5")
    parser.add_argument("--port", help="Serial port (ex: /dev/ttyUSB0, COM3)")
    parser.add_argument("--baud", type=int, default=38400, help="Baudrate (default 38400)")
    parser.add_argument("--list-ports", action="store_true", help="List serial ports and exit")
    args = parser.parse_args()

    if args.list_ports:
        return cmd_list_ports()
    if not args.port:
        parser.error("--port is required unless --list-ports is used")

    app = QtWidgets.QApplication(sys.argv)
    try:
        win = MainWindow(port=args.port, baud=args.baud)
    except serial.SerialException as exc:
        QtWidgets.QMessageBox.critical(None, "Serial error", str(exc))
        return 1
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
