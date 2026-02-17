#!/usr/bin/env python3
"""Qt-based UART/USB screenshot viewer for UV-K5 F4HWN firmware.

Features:
- Safe parser for screenshot diff frames
- Keepalive sender (0x55 0xAA 0x00 0x00)
- Live byte-level TX/RX logging windows
- LCD-like 128x64 screen renderer
- Remote keypad (button inject over UART command protocol)
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
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

# UART command protocol
CMD_HEADER = b"\xAB\xCD"
CMD_FOOTER = b"\xDC\xBA"
OBFUS_TBL = b"\x16\x6c\x14\xe6\x2e\x91\x0d\x40\x21\x35\xd5\x40\x13\x03\xe9\x80"

MSG_SESSION_INIT = 0x0514
MSG_SESSION_INFO = 0x0515
MSG_BUTTON_EVENT = 0x0610
MSG_BUTTON_ACK = 0x0611

ACTION_PRESS = 0
ACTION_RELEASE = 1

ACK_STATUS = {
    0: "accepted",
    1: "busy",
    2: "invalid",
    3: "stale",
}

SESSION_TIMEOUT_MS = 500
SESSION_RETRY_INTERVAL_MS = 300
BUTTON_ACK_TIMEOUT_MS = 700
BUTTON_RETRY_LIMIT = 4

KEY_CODES = {
    "0": 0,
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
    "8": 8,
    "9": 9,
    "MENU": 10,
    "UP": 11,
    "DOWN": 12,
    "EXIT": 13,
    "STAR": 14,
    "F": 15,
    "SIDE2": 17,
    "SIDE1": 18,
}


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
    cmd_diag = QtCore.Signal(str)

    def __init__(self, port: str, baud: int = 38400, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._serial = serial.Serial(port, baud, timeout=0)
        self._buffer = bytearray()
        self._cmd_buffer = bytearray()
        self._frame = bytearray(FRAME_SIZE)

        self._session_ts: int | None = None
        self._session_pending = False
        self._session_deadline_ms = 0
        self._last_session_attempt_ms = 0

        self._button_queue: list[tuple[int, int, str, int]] = []
        self._next_seq = 1
        self._inflight_seq: int | None = None
        self._inflight_event: tuple[int, int, str, int] | None = None
        self._inflight_deadline_ms = 0

        self._keepalive_timer = QtCore.QTimer(self)
        self._keepalive_timer.timeout.connect(self.send_keepalive)
        self._keepalive_timer.start(120)

        self._poll_timer = QtCore.QTimer(self)
        self._poll_timer.timeout.connect(self.poll)
        self._poll_timer.start(10)

        self._button_timer = QtCore.QTimer(self)
        self._button_timer.timeout.connect(self._service_button_tx)
        self._button_timer.start(25)

    def close(self) -> None:
        self._keepalive_timer.stop()
        self._poll_timer.stop()
        self._button_timer.stop()
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

    def queue_button_tap(self, key_name: str) -> None:
        key_name = key_name.upper()
        key_code = KEY_CODES.get(key_name)
        if key_code is None:
            self.status.emit(f"Unknown key: {key_name}")
            return

        self._button_queue.append((key_code, ACTION_PRESS, key_name, 0))
        self._button_queue.append((key_code, ACTION_RELEASE, key_name, 0))
        self.status.emit(f"Queued tap: {key_name}")

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
                    self._cmd_buffer.extend(data)
                    self._consume_screen_buffer()
                    self._consume_cmd_buffer()
        except serial.SerialException as exc:
            self.status.emit(f"RX error: {exc}")

    def _consume_screen_buffer(self) -> None:
        while True:
            if len(self._buffer) < 5:
                return

            hdr = self._buffer.find(HEADER)
            if hdr < 0:
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
            else:
                self.status.emit(f"Ignored frame type=0x{msg_type:02X} size={size}")

    def _consume_cmd_buffer(self) -> None:
        while True:
            msg = self._fetch_cmd_packet()
            if msg is None:
                return

            msg_type, payload = msg
            if msg_type == MSG_SESSION_INFO:
                session_ts = self._word_from_payload(payload, 0)
                self.cmd_diag.emit(f"RX 0x0515 session_info ts=0x{session_ts:08X}")
                self._session_pending = False
                self.status.emit("Remote keypad session established")
            elif msg_type == MSG_BUTTON_ACK and len(payload) >= 4:
                seq = payload[0] | (payload[1] << 8)
                status = payload[2]
                qdepth = payload[3]
                label = ACK_STATUS.get(status, f"unknown({status})")
                self.cmd_diag.emit(f"RX 0x0611 button_ack seq={seq} status={label} qdepth={qdepth}")

                if self._inflight_seq is None:
                    continue

                if seq != self._inflight_seq:
                    continue

                event = self._inflight_event
                self._inflight_seq = None
                self._inflight_event = None

                if status == 0:
                    self.status.emit(f"Button ACK: {label}, queue_depth={qdepth}")
                elif status in (1, 3):
                    # busy/stale -> retry event (front of queue)
                    if event is not None:
                        self._requeue_event(event)
                    if status == 3:
                        self._session_ts = None
                        self._session_pending = False
                    self.status.emit(f"Button ACK: {label}, retrying")
                else:
                    self.status.emit(f"Button ACK: {label}, dropped")

    def _fetch_cmd_packet(self) -> tuple[int, bytes] | None:
        buf = self._cmd_buffer

        if len(buf) < 8:
            return None

        begin = buf.find(CMD_HEADER)
        if begin < 0:
            if buf.endswith(b"\xAB"):
                del buf[:-1]
            else:
                del buf[:]
            return None

        if begin > 0:
            del buf[:begin]

        if len(buf) < 8:
            return None

        msg_len = buf[2] | (buf[3] << 8)
        packet_end = 6 + msg_len
        total = packet_end + 2

        if len(buf) < total:
            return None

        if buf[packet_end] != CMD_FOOTER[0] or buf[packet_end + 1] != CMD_FOOTER[1]:
            del buf[:2]
            return None

        body = bytearray(buf[4 : 4 + msg_len + 2])
        self._obfus(body)

        msg = body[:-2]
        if len(msg) < 2:
            del buf[:total]
            return None

        msg_type = msg[0] | (msg[1] << 8)
        payload = bytes(msg[4:]) if len(msg) >= 4 else b""

        del buf[:total]
        return msg_type, payload

    @staticmethod
    def _word_from_payload(payload: bytes, off: int = 0) -> int:
        if len(payload) < off + 4:
            return 0
        return payload[off] | (payload[off + 1] << 8) | (payload[off + 2] << 16) | (payload[off + 3] << 24)

    def _service_button_tx(self) -> None:
        now_ms = int(time.time() * 1000)

        if self._session_pending and now_ms >= self._session_deadline_ms:
            self._session_pending = False
            self._session_ts = None
            self.status.emit("Remote keypad session timeout")

        if self._inflight_seq is not None and now_ms >= self._inflight_deadline_ms:
            if self._inflight_event is not None:
                self._requeue_event(self._inflight_event)
            self._inflight_seq = None
            self._inflight_event = None
            self.status.emit("Button ACK timeout, retrying")

        if self._inflight_seq is not None or not self._button_queue:
            return

        if self._session_pending:
            return

        if not self._is_session_fresh(now_ms):
            if (now_ms - self._last_session_attempt_ms) < SESSION_RETRY_INTERVAL_MS:
                return
            self._start_session(now_ms)
            return

        key_code, action, key_name, retries = self._button_queue.pop(0)
        seq = self._next_seq & 0xFFFF
        self._next_seq = (self._next_seq + 1) & 0xFFFF

        payload = bytearray(10)
        payload[0:4] = self._word_le(self._session_ts or 0)
        payload[4:6] = self._hw_le(seq)
        payload[6] = key_code
        payload[7] = action
        payload[8:10] = self._hw_le(0)

        act = "press" if action == ACTION_PRESS else "release"
        self.cmd_diag.emit(
            f"TX 0x0610 button_event key={key_name} action={act} seq={seq} ts=0x{(self._session_ts or 0):08X}"
        )
        self._send_cmd(MSG_BUTTON_EVENT, payload)
        self._inflight_seq = seq
        self._inflight_event = (key_code, action, key_name, retries)
        self._inflight_deadline_ms = now_ms + BUTTON_ACK_TIMEOUT_MS
        self.status.emit(f"Sent {key_name} {act}")

    def _start_session(self, now_ms: int) -> None:
        self._session_pending = True
        self._last_session_attempt_ms = now_ms
        self._session_deadline_ms = now_ms + SESSION_TIMEOUT_MS
        self._session_ts = now_ms & 0xFFFFFFFF
        payload = bytearray(4)
        payload[0:4] = self._word_le(self._session_ts)
        self.cmd_diag.emit(f"TX 0x0514 session_init ts=0x{self._session_ts:08X}")
        self._send_cmd(MSG_SESSION_INIT, payload)

    def _requeue_event(self, event: tuple[int, int, str, int]) -> None:
        key_code, action, key_name, retries = event
        retries += 1
        if retries > BUTTON_RETRY_LIMIT:
            act = "press" if action == ACTION_PRESS else "release"
            self.status.emit(f"Dropped {key_name} {act}: retry limit exceeded")
            return
        self._button_queue.insert(0, (key_code, action, key_name, retries))

    def _is_session_fresh(self, now_ms: int) -> bool:
        if self._session_ts is None:
            return False
        return (now_ms - self._session_ts) < 5000

    def _send_cmd(self, msg_type: int, payload: bytes) -> None:
        msg = bytearray(4 + len(payload))
        msg[0:2] = self._hw_le(msg_type)
        msg[2:4] = self._hw_le(len(payload))
        msg[4 : 4 + len(payload)] = payload

        msg_len = len(msg)
        if msg_len % 2:
            msg += b"\x00"
            msg_len += 1

        packet = bytearray(8 + msg_len)
        packet[0:2] = b"\xAB\xCD"
        packet[2:4] = self._hw_le(msg_len)
        packet[4 : 4 + msg_len] = msg

        crc = self._calc_crc(packet, 4, msg_len)
        packet[4 + msg_len : 6 + msg_len] = self._hw_le(crc)
        packet[6 + msg_len : 8 + msg_len] = b"\xDC\xBA"

        body = bytearray(packet[4 : 6 + msg_len])
        self._obfus(body)
        packet[4 : 6 + msg_len] = body

        self._serial.write(packet)
        self.tx_log.emit(bytes(packet))

    @staticmethod
    def _obfus(buf: bytearray) -> None:
        n = len(OBFUS_TBL)
        for i in range(len(buf)):
            buf[i] ^= OBFUS_TBL[i % n]

    @staticmethod
    def _hw_le(n: int) -> bytes:
        return bytes((n & 0xFF, (n >> 8) & 0xFF))

    @staticmethod
    def _word_le(n: int) -> bytes:
        return bytes((n & 0xFF, (n >> 8) & 0xFF, (n >> 16) & 0xFF, (n >> 24) & 0xFF))

    @staticmethod
    def _calc_crc(buf: bytearray, off: int, size: int) -> int:
        crc = 0
        for i in range(size):
            b = buf[off + i] & 0xFF
            crc ^= b << 8
            for _ in range(8):
                if (crc >> 15) & 1:
                    crc = ((crc << 1) ^ 0x1021) & 0xFFFF
                else:
                    crc = (crc << 1) & 0xFFFF
        return crc

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
        self.setWindowTitle("K5 Qt Viewer + Remote Keypad + Byte Logger")
        self.resize(1360, 840)

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

        self.remote_box = QtWidgets.QGroupBox("Remote Keypad")
        self.remote_grid = QtWidgets.QGridLayout(self.remote_box)
        right.addWidget(self.remote_box)

        self.rx_box = QtWidgets.QPlainTextEdit()
        self.tx_box = QtWidgets.QPlainTextEdit()
        self.diag_box = QtWidgets.QPlainTextEdit()
        for box in (self.rx_box, self.tx_box, self.diag_box):
            box.setReadOnly(True)
            box.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
            font = QtGui.QFont("Courier New")
            font.setStyleHint(QtGui.QFont.Monospace)
            box.setFont(font)

        right.addWidget(QtWidgets.QLabel("RX Bytes (radio -> PC)"))
        right.addWidget(self.rx_box)
        right.addWidget(QtWidgets.QLabel("TX Bytes (PC -> radio)"))
        right.addWidget(self.tx_box)
        right.addWidget(QtWidgets.QLabel("Protocol Diagnostics (0x0514/0x0515/0x0610/0x0611)"))
        right.addWidget(self.diag_box)

        self.setCentralWidget(central)

        self.receiver = K5Receiver(port=port, baud=baud, parent=self)
        self.receiver.frame_ready.connect(self.screen.set_frame)
        self.receiver.status.connect(self.status_lbl.setText)
        self.receiver.rx_log.connect(lambda b: self._append_bytes(self.rx_box, b, "RX"))
        self.receiver.tx_log.connect(lambda b: self._append_bytes(self.tx_box, b, "TX"))
        self.receiver.cmd_diag.connect(self._append_diag)
        self.clear_logs_btn.clicked.connect(self._clear_logs)

        self._build_remote_keypad()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        self.receiver.close()
        super().closeEvent(event)

    def _build_remote_keypad(self) -> None:
        # Matches radio keypad layout.
        layout = [
            ["MENU", "UP", "DOWN", "EXIT"],
            ["1", "2", "3", "STAR"],
            ["4", "5", "6", "0"],
            ["7", "8", "9", "F"],
            ["SIDE1", "SIDE2", "", ""],
        ]

        for r, row in enumerate(layout):
            for c, key_name in enumerate(row):
                if not key_name:
                    continue
                btn = QtWidgets.QPushButton(key_name)
                btn.setMinimumHeight(34)
                btn.clicked.connect(lambda _checked=False, name=key_name: self.receiver.queue_button_tap(name))
                self.remote_grid.addWidget(btn, r, c)

        hint = QtWidgets.QLabel("Click = key tap (press+release).")
        hint.setStyleSheet("color: #777;")
        self.remote_grid.addWidget(hint, len(layout), 0, 1, 4)

    def _clear_logs(self) -> None:
        self.rx_box.clear()
        self.tx_box.clear()
        self.diag_box.clear()

    def _append_bytes(self, box: QtWidgets.QPlainTextEdit, data: bytes, tag: str) -> None:
        ts = dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        hexline = " ".join(f"{x:02X}" for x in data)
        box.appendPlainText(f"[{ts}] {tag} {len(data):3d}B | {hexline}")
        box.verticalScrollBar().setValue(box.verticalScrollBar().maximum())

    def _append_diag(self, text: str) -> None:
        ts = dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.diag_box.appendPlainText(f"[{ts}] {text}")
        self.diag_box.verticalScrollBar().setValue(self.diag_box.verticalScrollBar().maximum())

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
