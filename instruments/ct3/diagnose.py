"""CT3 communication diagnostic.

    python -m instruments.ct3.diagnose --port COM3

Tries every common baud rate, with the DTR/RTS lines on and off, and also
listens without sending anything (in case the CT3 is streaming in burst mode).
Prints every byte that comes back so we can see what the sensor is doing.
"""

import argparse
import time

import serial

BAUDS = (9600, 115200, 57600, 38400, 19200, 4800, 2400, 1200)
QUERIES = {"target temp 0x01": b"\x01", "head temp 0x02": b"\x02", "emissivity 0x04": b"\x04"}


def decode(cmd, resp):
    if len(resp) != 2:
        return ""
    raw = resp[0] * 256 + resp[1]
    if cmd in (b"\x04",):
        return f"-> {raw / 1000:.3f}"
    return f"-> {(raw - 1000) / 10:.1f} °C"


def try_port(port, baud, dtr, rts):
    hits = []
    try:
        ser = serial.Serial(port, baud, timeout=0.3)
    except serial.SerialException as e:
        print(f"  cannot open {port}: {e}")
        return None
    with ser:
        ser.dtr, ser.rts = dtr, rts
        time.sleep(0.2)
        # 1. passive listen (burst mode)
        ser.reset_input_buffer()
        time.sleep(0.5)
        stream = ser.read(ser.in_waiting or 0)
        if stream:
            hits.append(f"streaming without a query: {stream[:16].hex(' ')}")
        # 2. queries
        for label, cmd in QUERIES.items():
            ser.reset_input_buffer()
            ser.write(cmd)
            resp = ser.read(2)
            time.sleep(0.05)
            resp += ser.read(ser.in_waiting or 0)
            if resp:
                hits.append(f"{label}: {resp.hex(' ')} {decode(cmd, resp[:2])}")
    return hits


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    a = ap.parse_args()

    any_reply = False
    for baud in BAUDS:
        for dtr, rts in ((True, True), (False, False), (True, False), (False, True)):
            hits = try_port(a.port, baud, dtr, rts)
            if hits is None:
                raise SystemExit(1)
            tag = f"{baud:>6} baud  DTR={int(dtr)} RTS={int(rts)}"
            if hits:
                any_reply = True
                print(f"REPLY  {tag}")
                for h in hits:
                    print(f"       {h}")
            else:
                print(f"  --   {tag}")
    if not any_reply:
        print("\nNo bytes at any setting. Likely causes:\n"
              " - another program (CompactConnect, Fluke DataTemp, an old script) has the port open\n"
              " - the CT3's digital interface is set to a different mode/address in the box menu\n"
              " - the USB adapter isn't the one wired to the CT3's digital (not analog) output")
