"""
Micro-Epsilon thermoMETER CT3 (CT 3M) pyrometer driver + quick logger.

Protocol (binary, same as Optris CT family):
  send 1 command byte, device answers with 2 bytes (big-endian)
    0x01  target (object) temperature   T = (raw - 1000) / 10   [degC]
    0x02  sensing-head temperature      T = (raw - 1000) / 10
    0x03  electronics-box temperature   T = (raw - 1000) / 10
    0x04  emissivity                    e = raw / 1000
    0x05  transmission                  t = raw / 1000
    0x84  SET emissivity  -> send 0x84, hi, lo (raw = e*1000); device echoes value

Default serial: 115200 baud, 8N1 (confirmed on our CT3) (check the box menu / CompactConnect if no reply;
use --scan to try common baud rates).

Usage (from the repo root):
    python -m instruments.ct3.ct3 --list                     # show COM ports
    python -m instruments.ct3.ct3 --port COM5 --scan         # find the baud rate
    python -m instruments.ct3.ct3 --port COM5                # print live temps
    python -m instruments.ct3.ct3 --port COM5 --csv data/run1.csv
"""

import argparse
import csv
import struct
import sys
import time

import serial
import serial.tools.list_ports

from instruments.common.base import Instrument


class CT3(Instrument):
    name = "ct3"

    CMD_T_TARGET = 0x01
    CMD_T_HEAD = 0x02
    CMD_T_BOX = 0x03
    CMD_EMISSIVITY = 0x04
    CMD_TRANSMISSION = 0x05
    CMD_SET_EMISSIVITY = 0x84

    def __init__(self, port, baudrate=115200, timeout=0.2, retries=2):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.retries = retries
        self.ser = None

    # ---- connection -------------------------------------------------------
    def connect(self):
        self.ser = serial.Serial(
            self.port, self.baudrate,
            bytesize=8, parity="N", stopbits=1,
            timeout=self.timeout, write_timeout=self.timeout,
        )
        time.sleep(0.05)
        self.ser.reset_input_buffer()

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()

    # ---- low level --------------------------------------------------------
    def _query(self, payload: bytes, nbytes=2) -> int:
        last_err = None
        for _ in range(self.retries + 1):
            self.ser.reset_input_buffer()
            self.ser.write(payload)
            resp = self.ser.read(nbytes)
            if len(resp) == nbytes:
                return struct.unpack(">H", resp)[0]
            last_err = f"expected {nbytes} bytes, got {resp!r}"
        raise TimeoutError(f"CT3 no/short reply to {payload.hex()}: {last_err}")

    @staticmethod
    def _to_degc(raw: int) -> float:
        return (raw - 1000) / 10.0

    # ---- public API -------------------------------------------------------
    def target_temp(self) -> float:
        return self._to_degc(self._query(bytes([self.CMD_T_TARGET])))

    def head_temp(self) -> float:
        return self._to_degc(self._query(bytes([self.CMD_T_HEAD])))

    def box_temp(self) -> float:
        return self._to_degc(self._query(bytes([self.CMD_T_BOX])))

    def emissivity(self) -> float:
        return self._query(bytes([self.CMD_EMISSIVITY])) / 1000.0

    def transmission(self) -> float:
        return self._query(bytes([self.CMD_TRANSMISSION])) / 1000.0

    def set_emissivity(self, e: float) -> float:
        if not 0.1 <= e <= 1.1:
            raise ValueError("emissivity must be 0.100–1.100")
        raw = round(e * 1000)
        echoed = self._query(bytes([self.CMD_SET_EMISSIVITY]) + struct.pack(">H", raw))
        return echoed / 1000.0

    def read(self) -> dict:
        return {"T_ct3_C": self.target_temp()}

    def probe(self) -> bool:
        try:
            t = self.head_temp()
            return -40 < t < 200  # head temp should be near ambient
        except (TimeoutError, serial.SerialException):
            return False


# ---- helpers ---------------------------------------------------------------
def list_ports():
    for p in serial.tools.list_ports.comports():
        print(f"{p.device:10s} {p.description}  [{p.hwid}]")


def scan_baud(port):
    for baud in (115200, 9600, 57600, 38400, 19200, 4800, 2400):
        with CT3(port, baud) as dev:
            if dev.probe():
                print(f"CT3 found at {baud} baud (head {dev.head_temp():.1f} °C)")
                return baud
        print(f"  {baud}: no valid reply")
    print("No reply at any baud rate. Check wiring, port, and that the box's "
          "digital output is enabled (not analog-only).")
    return None


def run(port, baud, rate_hz, csv_path, emissivity):
    period = 1.0 / rate_hz if rate_hz > 0 else 0.0
    n, last_print = 0, -1.0
    with CT3(port, baud) as dev:
        if emissivity is not None:
            print(f"Emissivity set to {dev.set_emissivity(emissivity):.3f}")
        print(f"Connected: {port} @ {baud}  e={dev.emissivity():.3f}  "
              f"head={dev.head_temp():.1f} °C  box={dev.box_temp():.1f} °C")
        print("Ctrl+C to stop.\n")

        f = writer = None
        if csv_path:
            f = open(csv_path, "w", newline="")
            writer = csv.writer(f)
            writer.writerow(["t_s", "T_ct3_C"])

        t0 = time.perf_counter()
        next_t = t0
        try:
            while True:
                try:
                    T = dev.target_temp()
                except TimeoutError as e:
                    print(f"\n{e}")
                    T = float("nan")
                t = time.perf_counter() - t0
                n += 1
                if t - last_print >= 0.1:  # screen at 10 Hz; CSV gets every sample
                    last_print = t
                    if f:
                        f.flush()  # live CSV for other programs, ~10x/s
                    print(f"\r t = {t:8.3f} s   T = {T:7.1f} °C   {n / t if t else 0:5.0f} Hz   ",
                          end="", flush=True)
                if writer:
                    writer.writerow([f"{t:.4f}", f"{T:.1f}"])
                if period:
                    next_t += period
                    time.sleep(max(0.0, next_t - time.perf_counter()))
        except KeyboardInterrupt:
            t = time.perf_counter() - t0
            print(f"\nStopped. {n} samples in {t:.2f} s = {n / t:.0f} Hz average")
        finally:
            if f:
                f.close()
                print(f"Saved {csv_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Micro-Epsilon CT3 pyrometer reader")
    ap.add_argument("--list", action="store_true", help="list serial ports")
    ap.add_argument("--port", help="e.g. COM5 or /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--scan", action="store_true", help="auto-detect baud rate")
    ap.add_argument("--rate", type=float, default=0,
                    help="samples per second (default 0 = as fast as possible)")
    ap.add_argument("--csv", help="log to this CSV (elapsed seconds, °C)")
    ap.add_argument("--emissivity", type=float, help="set emissivity before logging")
    a = ap.parse_args()

    if a.list or not a.port:
        list_ports()
        sys.exit(0)
    if a.scan:
        found = scan_baud(a.port)
        if not found:
            sys.exit(1)
        a.baud = found
    run(a.port, a.baud, a.rate, a.csv, a.emissivity)
