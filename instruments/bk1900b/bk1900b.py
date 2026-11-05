"""
BK Precision 1900B DC supply (1-16.2 V, 0-63 A) driver + quick logger.

Protocol (ASCII, from the BK 1900B-series programming manual):
  USB virtual COM, 9600 baud, 8N1, no flow control.
  Every command ends with CR (\\r, no line feed). Every reply ends with "OK\\r";
  queries return one data line first, then "OK\\r".

    GETD          -> VVVVIIIIS   live display: V (2 dp), I (2 dp), S = 0 CV / 1 CC
    GETS          -> VVVIII      setpoints:    V (1 dp), I (1 dp)
    GMAX          -> VVVIII      max ratings,  e.g. 162630 = 16.2 V, 63.0 A (our unit)
    GOVP / GOCP   -> VVV / III   upper voltage / current limits (1 dp)
    VOLT<vvv>     set voltage    VOLT125 = 12.5 V   (min 1.0 V: VOLT005 gets no reply)
    CURR<iii>     set current    CURR305 = 30.5 A
    SOVP<vvv> / SOCP<iii>        set upper voltage / current limit
    SOUT0 / SOUT1 output ON / OFF   (note: 0 = ON, 1 = OFF)

  The manual's examples disagree on the decimal places in GETD current
  (says 1 dp for the 1900B, example shows 2 dp). Run --check once on the
  real supply: it prints the raw GETD reply next to the front panel so the
  scaling can be confirmed; change DISPLAY_DP below if it's off.

Usage (from the repo root):
    python -m instruments.bk1900b.bk1900b --list                  # show COM ports
    python -m instruments.bk1900b.bk1900b --port COM4 --check     # identify + raw replies
    python -m instruments.bk1900b.bk1900b --port COM4             # print live V / I / P
    python -m instruments.bk1900b.bk1900b --port COM4 --csv data/bk_run1.csv
    python -m instruments.bk1900b.bk1900b --port COM4 --set-I 20 --set-V 5 --on
"""

import argparse
import csv
import re
import sys
import threading
import time

import serial
import serial.tools.list_ports

from instruments.common.base import Instrument


class BK1900B(Instrument):
    name = "bk1900b"

    # Decimal places in each reply/command field (see note above)
    V_MIN = 1.0            # 1900B won't accept a voltage setpoint below 1.0 V (no OK reply)

    DISPLAY_DP = (2, 2)    # GETD: voltage, current
    SET_DP = (1, 1)        # VOLT/CURR/GETS/GMAX/OVP/OCP: voltage, current

    def __init__(self, port, baudrate=9600, timeout=0.5, retries=2):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.retries = retries
        self.ser = None
        self.V_max = self.I_max = None
        self._lock = threading.Lock()  # GUI + logger + controller may share it

    # ---- connection -------------------------------------------------------
    def connect(self):
        self.ser = serial.Serial(
            self.port, self.baudrate,
            bytesize=8, parity="N", stopbits=1,
            timeout=self.timeout, write_timeout=self.timeout,
        )
        time.sleep(0.05)
        self.ser.reset_input_buffer()
        try:
            self.V_max, self.I_max = self.max_ratings()
        except (TimeoutError, ValueError):
            pass  # not fatal here; probe()/--check will report it

    def close(self):
        """Closes the port only. The output is left as it is (call output_off() first if wanted)."""
        if self.ser and self.ser.is_open:
            self.ser.close()

    # ---- low level --------------------------------------------------------
    def _command(self, cmd: str) -> list[str]:
        """Send cmd + CR, read until 'OK'. Returns the data lines before OK."""
        last_err = None
        with self._lock:
            for _ in range(self.retries + 1):
                self.ser.reset_input_buffer()
                self.ser.write((cmd + "\r").encode("ascii"))
                lines = []
                deadline = time.perf_counter() + self.timeout
                while time.perf_counter() < deadline:
                    raw = self.ser.read_until(b"\r")
                    if not raw:
                        break
                    line = raw.decode("ascii", "replace").strip()
                    if not line:
                        continue
                    if line == "OK":
                        return lines
                    lines.append(line)
                last_err = f"got {lines!r} without OK"
        raise TimeoutError(f"BK1900B no/short reply to {cmd!r}: {last_err}")

    def _query(self, cmd: str, n_digits: int) -> str:
        lines = self._command(cmd)
        for line in lines:
            digits = re.sub(r"\D", "", line)
            if len(digits) == n_digits:
                return digits
        raise ValueError(f"BK1900B {cmd}: expected {n_digits} digits, got {lines!r}")

    @staticmethod
    def _num(digits: str, dp: int) -> float:
        return int(digits) / 10 ** dp

    @staticmethod
    def _field(value: float, dp: int, width: int = 3) -> str:
        raw = round(value * 10 ** dp)
        if not 0 <= raw < 10 ** width:
            raise ValueError(f"{value} doesn't fit a {width}-digit field")
        return f"{raw:0{width}d}"

    # ---- readback ---------------------------------------------------------
    def display(self) -> tuple[float, float, int]:
        """Live output (V, I, mode). mode: 0 = CV, 1 = CC."""
        d = self._query("GETD", 9)
        vdp, idp = self.DISPLAY_DP
        return self._num(d[0:4], vdp), self._num(d[4:8], idp), int(d[8])

    def setpoints(self) -> tuple[float, float]:
        d = self._query("GETS", 6)
        return self._num(d[:3], self.SET_DP[0]), self._num(d[3:], self.SET_DP[1])

    def max_ratings(self) -> tuple[float, float]:
        d = self._query("GMAX", 6)
        return self._num(d[:3], self.SET_DP[0]), self._num(d[3:], self.SET_DP[1])

    def ovp(self) -> float:
        return self._num(self._query("GOVP", 3), self.SET_DP[0])

    def ocp(self) -> float:
        return self._num(self._query("GOCP", 3), self.SET_DP[1])

    # ---- control ----------------------------------------------------------
    def _check_limit(self, value, limit, what, low=0.0):
        if value < low or (limit is not None and value > limit):
            raise ValueError(f"{what} {value} outside {low}..{limit}")

    def set_voltage(self, V: float):
        self._check_limit(V, self.V_max, "voltage", low=self.V_MIN)
        self._command("VOLT" + self._field(V, self.SET_DP[0]))

    def set_current(self, I: float):
        """Constant-current setpoint (the actuator for the controller)."""
        self._check_limit(I, self.I_max, "current")
        self._command("CURR" + self._field(I, self.SET_DP[1]))

    def set_ovp(self, V: float):
        self._command("SOVP" + self._field(V, self.SET_DP[0]))

    def set_ocp(self, I: float):
        self._command("SOCP" + self._field(I, self.SET_DP[1]))

    def output_on(self):
        self._command("SOUT0")   # 0 = ON (manual)

    def output_off(self):
        self._command("SOUT1")   # 1 = OFF

    # ---- Instrument interface ----------------------------------------------
    def read(self) -> dict:
        V, I, mode = self.display()
        return {"V_bk_V": V, "I_bk_A": I, "P_bk_W": V * I, "CC_bk": mode}

    def probe(self) -> bool:
        try:
            V, I = self.max_ratings()
            return 0 < V <= 100 and 0 < I <= 200
        except (TimeoutError, ValueError, serial.SerialException):
            return False


# ---- helpers ---------------------------------------------------------------
def list_ports():
    for p in serial.tools.list_ports.comports():
        print(f"{p.device:10s} {p.description}  [{p.hwid}]")


def check(port, baud):
    """Identify the supply and print raw replies to confirm scaling."""
    with BK1900B(port, baud) as dev:
        for cmd in ("GMAX", "GETS", "GETD", "GOVP", "GOCP"):
            try:
                print(f"{cmd:5s} raw -> {dev._command(cmd)}")
            except TimeoutError as e:
                print(f"{cmd:5s} {e}")
        print()
        print(f"max ratings : {dev.V_max} V, {dev.I_max} A   (our 1900B: 16.2 V, 63.0 A)")
        Vs, Is = dev.setpoints()
        print(f"setpoints   : {Vs} V, {Is} A")
        V, I, m = dev.display()
        print(f"display     : {V:.2f} V, {I:.2f} A, {'CC' if m else 'CV'}")
        print("\nCompare 'display' with the front panel. If current is off by 10x, "
              "change DISPLAY_DP in bk1900b.py.")


def run(port, baud, rate_hz, csv_path, set_V=None, set_I=None, on=False, off_on_exit=False):
    period = 1.0 / rate_hz if rate_hz > 0 else 0.0
    n, last_print = 0, -1.0
    with BK1900B(port, baud) as dev:
        if set_V is not None:
            dev.set_voltage(set_V)
        if set_I is not None:
            dev.set_current(set_I)
        Vs, Is = dev.setpoints()
        print(f"Connected: {port} @ {baud}  max {dev.V_max} V / {dev.I_max} A  "
              f"set {Vs} V / {Is} A")
        if on:
            dev.output_on()
            print("Output ON")
        print("Ctrl+C to stop.\n")

        f = writer = None
        if csv_path:
            f = open(csv_path, "w", newline="")
            writer = csv.writer(f)
            writer.writerow(["t_s", "V_bk_V", "I_bk_A", "P_bk_W", "CC_bk"])

        t0 = time.perf_counter()
        next_t = t0
        try:
            while True:
                try:
                    V, I, mode = dev.display()
                except (TimeoutError, ValueError) as e:
                    print(f"\n{e}")
                    V = I = float("nan")
                    mode = -1
                t = time.perf_counter() - t0
                n += 1
                if t - last_print >= 0.1:  # screen at 10 Hz; CSV gets every sample
                    last_print = t
                    if f:
                        f.flush()
                    tag = {0: "CV", 1: "CC"}.get(mode, "--")
                    print(f"\r t = {t:8.3f} s   V = {V:6.2f} V   I = {I:6.2f} A   "
                          f"P = {V * I:7.1f} W   {tag}   {n / t if t else 0:4.0f} Hz   ",
                          end="", flush=True)
                if writer:
                    writer.writerow([f"{t:.4f}", f"{V:.2f}", f"{I:.2f}", f"{V * I:.2f}", mode])
                if period:
                    next_t += period
                    time.sleep(max(0.0, next_t - time.perf_counter()))
        except KeyboardInterrupt:
            t = time.perf_counter() - t0
            print(f"\nStopped. {n} samples in {t:.2f} s = {n / t:.1f} Hz average")
        finally:
            if off_on_exit:
                try:
                    dev.output_off()
                    print("Output OFF")
                except Exception as e:
                    print(f"WARNING: could not turn output off: {e}")
            if f:
                f.close()
                print(f"Saved {csv_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="BK Precision 1900B DC supply")
    ap.add_argument("--list", action="store_true", help="list serial ports")
    ap.add_argument("--port", help="e.g. COM4")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--check", action="store_true", help="identify supply, show raw replies")
    ap.add_argument("--rate", type=float, default=0,
                    help="samples per second (default 0 = as fast as possible)")
    ap.add_argument("--csv", help="log to this CSV")
    ap.add_argument("--set-V", type=float, help="voltage setpoint [V] (CC mode: compliance limit)")
    ap.add_argument("--set-I", type=float, help="current setpoint [A]")
    ap.add_argument("--on", action="store_true", help="turn output ON before logging")
    ap.add_argument("--off-on-exit", action="store_true", help="turn output OFF on Ctrl+C")
    a = ap.parse_args()

    if a.list or not a.port:
        list_ports()
        sys.exit(0)
    if a.check:
        check(a.port, a.baud)
        sys.exit(0)
    run(a.port, a.baud, a.rate, a.csv, a.set_V, a.set_I, a.on, a.off_on_exit or a.on)
