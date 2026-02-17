# Copyright (c) 2026

from __future__ import annotations

import time

import msg as mm

MSG_SESSION_INIT = 0x0514
MSG_SESSION_INFO = 0x0515
MSG_BUTTON_EVENT = 0x0610
MSG_BUTTON_ACK = 0x0611

ACTION_PRESS = 0
ACTION_RELEASE = 1

KEY_MAP = {
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


def now_ts() -> int:
    return int(time.time() * 1000) & 0xFFFFFFFF


class MsgReceiver:
    def __init__(self, ser):
        self.ser = ser
        self.buf = bytearray()

    def recv(self) -> mm.Msg | None:
        self._rx()
        return mm.fetch(self.buf)

    def _rx(self):
        chunk = self.ser.read(256)
        if chunk:
            self.buf.extend(chunk)


def send_msg(ser, msg: mm.Msg):
    pack = mm.make_packet(msg.buf)
    ser.write(pack)
    ser.flush()


def make_session_init(timestamp: int) -> mm.Msg:
    msg = mm.Msg.make(MSG_SESSION_INIT, 4)
    msg.set_word_LE(4, timestamp)
    return msg


def make_button_msg(timestamp: int, seq: int, key_code: int, action: int, hold_ms: int = 0) -> mm.Msg:
    msg = mm.Msg.make(MSG_BUTTON_EVENT, 10)
    msg.set_word_LE(4, timestamp)
    msg.set_hw_LE(8, seq)
    msg.buf[10] = key_code & 0xFF
    msg.buf[11] = action & 0xFF
    msg.set_hw_LE(12, hold_ms & 0xFFFF)
    return msg


def wait_for_msg(receiver: MsgReceiver, msg_type: int, timeout_s: float) -> mm.Msg | None:
    end = time.time() + timeout_s
    while time.time() < end:
        msg = receiver.recv()
        if msg and msg.get_msg_type() == msg_type:
            return msg
        time.sleep(0.002)
    return None


def send_button(ser, key_name: str, action_name: str, seq: int, timeout_s: float = 0.4) -> tuple[bool, str]:
    key_name = key_name.upper()
    if key_name not in KEY_MAP:
        return False, f"invalid key '{key_name}'"

    action_name = action_name.lower()
    if action_name == "press":
        action = ACTION_PRESS
    elif action_name == "release":
        action = ACTION_RELEASE
    else:
        return False, f"invalid action '{action_name}'"

    receiver = MsgReceiver(ser)
    ts = now_ts()

    send_msg(ser, make_session_init(ts))
    session_reply = wait_for_msg(receiver, MSG_SESSION_INFO, timeout_s)
    if not session_reply:
        return False, "no session reply (0x0515)"

    send_msg(ser, make_button_msg(ts, seq, KEY_MAP[key_name], action))
    ack = wait_for_msg(receiver, MSG_BUTTON_ACK, timeout_s)
    if not ack:
        return False, "no button ack (0x0611)"

    ack_seq = ack.get_hw_LE(4)
    status = ack.buf[6]
    qdepth = ack.buf[7]

    if ack_seq != (seq & 0xFFFF):
        return False, f"ack sequence mismatch: expected={seq & 0xFFFF} got={ack_seq}"

    if status == 0:
        return True, f"accepted (queue_depth={qdepth})"

    status_map = {
        1: "busy",
        2: "invalid",
        3: "stale",
    }
    return False, f"rejected: {status_map.get(status, f'unknown({status})')} (queue_depth={qdepth})"
