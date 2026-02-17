# UART Button Receiver Design (PC -> Radio)

This document describes a robust approach for injecting button events over UART/USB into firmware without colliding with existing serial traffic.

## Why this can fit current firmware

The firmware already has a packetized command protocol with:

- Framing (`0xABCD ... 0xBADC`)
- Obfuscation using a 16-byte table
- CRC validation
- Per-session timestamp gating for stateful commands

The parser (`UART_IsCommandAvailable`) already validates framing, lengths, footer, and CRC before handing messages to `UART_HandleCommand`. This makes it a good place to add a new command family for button input.

## Proposed command set

Use a new command ID range that does not conflict with existing commands, for example:

- `0x0610` = `BUTTON_EVENT`
- `0x0611` = `BUTTON_EVENT_ACK`

### `BUTTON_EVENT` payload

```c
struct __attribute__((packed)) {
    Header_t Header;       // ID=0x0610
    uint32_t Timestamp;    // must match active serial session
    uint16_t Seq;          // host sequence number, monotonic modulo 65536
    uint8_t  KeyCode;      // KEY_Code_t value
    uint8_t  Action;       // 0=press, 1=release, 2=longpress(optional)
    uint16_t HoldMs;       // optional hold duration hint
};
```

### `BUTTON_EVENT_ACK` payload

```c
struct __attribute__((packed)) {
    Header_t Header;       // ID=0x0611
    uint16_t Seq;          // echoed sequence
    uint8_t  Status;       // 0=accepted, 1=busy, 2=invalid, 3=stale
    uint8_t  QueueDepth;   // queue fill level
};
```

## Collision and safety strategy

### 1) Single-writer discipline on host

Only one host process should open the serial port. If scripting and a GUI are both needed, route everything through one broker process that owns UART.

### 2) Session-bound acceptance

Reject button messages unless the sender first establishes session via existing init command (`0x0514`/`0x052F`) and includes matching timestamp. This prevents stale senders from injecting events.

### 3) Bounded queue in firmware

Do not process button messages directly in the UART ISR / packet parser path. Instead:

- Parse packet
- Validate `KeyCode/Action`
- Push to a fixed ring queue (small, e.g. 8-16 entries)
- Return ACK immediately
- Consume queue from main loop/task context

If queue is full, drop newest (or oldest, but be consistent) and return `Status=busy`.

### 4) No synthetic key storms

Rate-limit accepted events per second and/or deduplicate repeated `press` for same key while still logically pressed.

### 5) Explicit press/release state machine

Maintain `remote_keys_down[key]` bitmap. Enforce legal transitions:

- `press` when already down -> ignore + ACK invalid (or accepted-noop)
- `release` when not down -> ignore + ACK invalid

This avoids stuck-key crashes and weird UI state.


## Coexistence with the screen viewer traffic (important)

The current screen viewer path is **not** using the same framed command protocol as `0x05xx` UART packets:

- Viewer sends keepalive bytes: `55 AA 00 00`
- Firmware screenshot stream sends frames with `AA 55` header and type/length payload

So if button injection and screen streaming share one serial link, you must avoid half-duplex collisions and parser confusion by design.

### Recommended transport policy

1. **Single host owner for the serial port** (viewer + button sender behind one process), never two independent apps opening the same port.
2. **Timeslice TX from host**: send keepalive first, then (if needed) one button packet, then return to RX-heavy screenshot mode.
3. **Bounded button send rate** while viewer is active (e.g. <= 20 events/s) to protect screenshot latency.
4. **ACK timeout tuned for viewer mode** (slightly longer than normal, because stream traffic can delay reads).

### Firmware-side safeguards for mixed traffic

- Keep screenshot path and command path logically separate in parser/dispatch.
- If a button command arrives while screenshot lock/critical section is active, return `ACK=busy` instead of blocking.
- Never call UI/key handlers directly from UART parser; always enqueue and consume in main loop.

### Optional (best) architecture

Add an explicit mode flag for tools:

- `MODE_SCREEN` (viewer active, button rate-limited)
- `MODE_CONFIG` (normal config/programming commands)

Or introduce a tiny host-side **broker** that multiplexes:

- screenshot keepalive/receive loop
- queued button send + ACK/retry

This is the safest way to support "watch screen + press remote buttons" at the same time without race conditions.

## Firmware integration points

- Add command structs and handlers in `App/app/uart.c`
- Add a tiny remote key FIFO module (new file pair, e.g. `App/app/remote_key.c/.h`)
- Hook consumption in main input path (same place physical key results are dispatched)
- Reuse existing `KEY_Code_t` values from `App/driver/keyboard.h`

Keep all memory static (no malloc), and keep parser-time work O(1).

## Host script changes (tools/serialtool)

Add a subcommand, e.g.:

```bash
python -m tools.serialtool.cli button --port /dev/ttyUSB0 --key MENU --action press
python -m tools.serialtool.cli button --port /dev/ttyUSB0 --key MENU --action release
```

Implementation approach:

1. Reuse existing packet builder (`tools/serialtool/msg.py::make_packet`)
2. Add helper to send `0x0610` with sequence counter
3. Read until matching `0x0611` ACK/timeout
4. Retry a small number of times on timeout
5. Surface `busy/invalid/stale` statuses to caller

## Recommended rollout

1. Implement firmware command + ACK only (no injection), verify parser stability.
2. Add queue and consume into existing key handling.
3. Add host CLI sender + ACK handling.
4. Stress test with 10k synthetic events and random delays.
5. Add watchdog-safe behavior: if malformed traffic floods UART, parser still recovers.

## Basic test matrix

- Valid press/release for every `KEY_Code_t`
- Duplicate press and duplicate release handling
- Queue full behavior and ACK correctness
- Session mismatch rejection
- High-rate burst while scanning/transmitting
- Coexistence with normal EEPROM read/write commands
- Coexistence with active screen viewer keepalive + screenshot streaming (`55AA0000`, `AA55...`)

## Notes

- Keep this feature behind a compile-time flag initially (e.g. `ENABLE_UART_BUTTON_RX`).
- Prefer USB CDC (`UART_PORT_VCP`) for development, then validate hardware UART.
