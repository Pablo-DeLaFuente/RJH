"""
Fluke Endurance E1RH 2-color pyrometer: temperature over Ethernet (TCP) or RS485.

STATUS: first draft, NOT yet tested on the instrument. Run --check first.

What is confirmed from the Endurance Series Users Manual (Rev. 2.1, 2022):
  Ethernet : TCP/IP v4, default port 6363 (settable 1-65535); UDP 36363 fixed;
             web server on port 80 (also serves MJPEG video, see video.py)
  RS485    : default 38400 baud, 8N1; multidrop address 1-32 (000 = off)
  ASCII    : commands are UPPER CASE. Replies start with "!" (acknowledge);
             in multidrop mode the 3-digit address goes in front (e.g. "001?T").
             Out-of-range values return a range error.
  Factory default output is BURST mode with burst string "UTSI"
  (the sensor streams lines on its own, without being asked).

Assumed from the Raytek/Fluke ASCII family (same protocol style) - VERIFY
with --check / --raw and the manual's Command List (section "ASCII Command List"):
  ?T   target temperature          -> !T1234.5
  ?I   internal (sensor) temperature
  ?E   emissivity    E=0.950 sets it
  ?S   slope (2-color)  S=1.000 sets it
  ?U   unit          U=C sets degC
  terminator: CR (we send "\\r"; replies may end with CR or CR LF)

The driver works in either mode:
  poll  : sends ?T and waits for !T...      (default)
  burst : just reads the newest streamed line and pulls T out of it

Usage (from the repo root):
    python -m instruments.fluke_e1rh.e1rh --host 192.168.0.10 --check
    python -m instruments.fluke_e1rh.e1rh --host 192.168.0.10 --listen 5   # show raw stream 5 s
    python -m instruments.fluke_e1rh.e1rh --host 192.168.0.10 --raw "?T"   # send any command
    python -m instruments.fluke_e1rh.e1rh --host 192.168.0.10              # live T
    python -m instruments.fluke_e1rh.e1rh --host 192.168.0.10 --csv data/e1rh_test.csv
    python -m instruments.fluke_e1rh.e1rh --serial COM5 --check            # RS485 adapter
"""

import argparse
import csv
import math
import re
import socket
import sys
import threading
import time

import serial

from instruments.common.base import Instrument

# token = command letter(s) + number, e.g. "T1234.5", "I045.2", "S1.000", "UC"
TOKEN = re.compile(r"\b(U)([CF])\b|([A-Z])\s*([-+]?\d+(?:\.\d+)?)")


# ---- transports: same write()/readline() for TCP and serial ----------------
class _TCP:
    def __init__(self, host, port, timeout):
        self.timeout = timeout
        try:
            # connecting gets a longer timeout than each read
            self.sock = socket.create_connection((host, int(port)), timeout=3.0)
        except socket.timeout:
            raise TimeoutError(
                f"E1RH: no answer from {host}:{port} in 3 s. Check: (1) `ping {host}` works; "
                f"(2) laptop Ethernet IP is on the same subnet (e.g. {'.'.join(host.split('.')[:3])}.20); "
                f"(3) TCP port in the sensor's Interface menu (default 6363); "
                f"(4) DataTemp / other software is closed.") from None
        except ConnectionRefusedError:
            raise ConnectionError(
                f"E1RH: {host} is reachable but refused port {port}. Check the TCP port in "
                f"the sensor's Interface menu, and close DataTemp.") from None
        self.sock.settimeout(timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.buf = b""

    def write(self, data: bytes):
        self.sock.sendall(data)

    def readline(self) -> bytes:
        """Return one line ending in CR or LF, or b'' on timeout."""
        while True:
            m = re.search(rb"[\r\n]", self.buf)
            if m:
                line, self.buf = self.buf[:m.end()], self.buf[m.end():]
                return line
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                return b""
            if not chunk:
                raise ConnectionError("E1RH closed the TCP connection")
            self.buf += chunk

    def flush_input(self):
        self.sock.setblocking(False)
        try:
            while self.sock.recv(4096):
                pass
        except (BlockingIOError, OSError):
            pass
        finally:
            self.sock.settimeout(self.timeout)
        self.buf = b""

    def close(self):
        self.sock.close()


class _Serial:
    def __init__(self, port, baudrate, timeout):
        self.ser = serial.Serial(port, baudrate, bytesize=8, parity="N", stopbits=1,
                                 timeout=timeout, write_timeout=timeout)

    def write(self, data: bytes):
        self.ser.write(data)

    def readline(self) -> bytes:
        return self.ser.read_until(b"\r")

    def flush_input(self):
        self.ser.reset_input_buffer()

    def close(self):
        self.ser.close()


# ---- driver -----------------------------------------------------------------
class E1RH(Instrument):
    name = "fluke_e1rh"

    def __init__(self, mode="tcp", host=None, tcp_port=6363, port=None, baudrate=38400,
                 address=None, transfer="poll", timeout=0.5, retries=2):
        """
        mode     : "tcp" (Ethernet/PoE) or "serial" (RS485 via USB adapter)
        host     : IP address (tcp)          tcp_port: default 6363
        port     : COM port (serial)         baudrate: default 38400
        address  : multidrop address 1-32, or None if not in multidrop
        transfer : "poll" (ask for every reading) or "burst" (read the stream)
        """
        self.mode = mode
        self.host, self.tcp_port = host, tcp_port
        self.port, self.baudrate = port, baudrate
        self.address = address
        self.transfer = transfer
        self.timeout = timeout
        self.retries = retries
        self.io = None
        self._lock = threading.Lock()

    # ---- connection -------------------------------------------------------
    def connect(self):
        if self.mode == "tcp":
            if not self.host:
                raise ValueError("E1RH tcp mode needs host (IP address)")
            self.io = _TCP(self.host, self.tcp_port, self.timeout)
        elif self.mode == "serial":
            if not self.port:
                raise ValueError("E1RH serial mode needs port (e.g. COM5)")
            self.io = _Serial(self.port, self.baudrate, self.timeout)
        else:
            raise ValueError(f"mode must be tcp or serial, got {self.mode!r}")
        time.sleep(0.05)
        self.io.flush_input()

    def close(self):
        if self.io:
            self.io.close()
            self.io = None

    # ---- low level --------------------------------------------------------
    def _prefix(self) -> str:
        return f"{int(self.address):03d}" if self.address else ""

    def send(self, cmd: str):
        self.io.write((self._prefix() + cmd.upper() + "\r").encode("ascii"))

    def readline(self) -> str:
        return self.io.readline().decode("ascii", "replace").strip()

    def command(self, cmd: str) -> str:
        """Send e.g. '?T' or 'E=0.95' and return the matching '!...' reply text.

        Burst lines that arrive in between are skipped. The reply is returned
        without the address and '!' (e.g. 'T1234.5').
        """
        want = cmd.upper().lstrip("?").split("=")[0]   # '?T' -> 'T', 'E=0.95' -> 'E'
        last = None
        with self._lock:
            for _ in range(self.retries + 1):
                self.io.flush_input()
                self.send(cmd)
                deadline = time.perf_counter() + self.timeout
                while time.perf_counter() < deadline:
                    line = self.readline()
                    if not line:
                        break
                    last = line
                    body = line.split("!", 1)[1] if "!" in line else None
                    if body is not None and body.startswith(want):
                        return body
                    if line.startswith("*") or "ERR" in line.upper():
                        raise ValueError(f"E1RH rejected {cmd!r}: {line!r}")
        raise TimeoutError(f"E1RH no reply to {cmd!r} (last line: {last!r})")

    def query(self, letter: str) -> str:
        """'T' -> '1234.5' (text after the letter)."""
        return self.command("?" + letter)[len(letter):].strip()

    @staticmethod
    def _float(text: str) -> float:
        try:
            return float(text)
        except ValueError:
            return math.nan   # over/under range, sensor error code, etc.

    @staticmethod
    def parse_tokens(line: str) -> dict:
        """'UC T1234.5 S1.000 I045.2' -> {'U': 'C', 'T': 1234.5, 'S': 1.0, 'I': 45.2}"""
        out = {}
        for u, unit, key, num in TOKEN.findall(line.replace("!", " ")):
            if u:
                out["U"] = unit
            else:
                out[key] = float(num)
        return out

    # ---- public API -------------------------------------------------------
    def target_temp(self) -> float:
        if self.transfer == "burst":
            return self.latest_burst().get("T", math.nan)
        return self._float(self.query("T"))

    def internal_temp(self) -> float:
        return self._float(self.query("I"))

    def emissivity(self) -> float:
        return self._float(self.query("E"))

    def slope(self) -> float:
        return self._float(self.query("S"))

    def unit(self) -> str:
        return self.query("U")

    def set_emissivity(self, e: float) -> str:
        if not 0.1 <= e <= 1.1:
            raise ValueError("emissivity must be 0.100-1.100")
        return self.command(f"E={e:.3f}")

    def set_slope(self, s: float) -> str:
        if not 0.85 <= s <= 1.15:
            raise ValueError("slope must be 0.850-1.150")
        return self.command(f"S={s:.3f}")

    def set_unit_c(self) -> str:
        return self.command("U=C")

    def latest_burst(self) -> dict:
        """Burst mode: read everything waiting and parse the newest full line."""
        newest = None
        with self._lock:
            t_end = time.perf_counter() + self.timeout
            while True:
                line = self.readline()
                if line:
                    newest = line
                    if time.perf_counter() > t_end:
                        break
                    continue
                if newest or time.perf_counter() > t_end:
                    break
        if newest is None:
            raise TimeoutError("E1RH: no burst data (sensor may be in poll mode)")
        return self.parse_tokens(newest)

    # ---- Instrument interface ----------------------------------------------
    def read(self) -> dict:
        return {"T_e1rh_C": self.target_temp()}

    def probe(self) -> bool:
        try:
            t = self.internal_temp()
            return -20 < t < 120   # sensor housing should be near ambient
        except (TimeoutError, ValueError, OSError, serial.SerialException):
            return False


# ---- helpers ---------------------------------------------------------------
def make(a) -> E1RH:
    if a.serial:
        return E1RH(mode="serial", port=a.serial, baudrate=a.baud, address=a.address,
                    transfer=a.transfer)
    return E1RH(mode="tcp", host=a.host, tcp_port=a.tcp_port, address=a.address,
                transfer=a.transfer)


def listen(dev, seconds):
    """Print whatever the sensor sends on its own (burst stream)."""
    print(f"Listening {seconds} s for streamed lines...")
    t_end = time.perf_counter() + seconds
    n = 0
    while time.perf_counter() < t_end:
        line = dev.readline()
        if line:
            n += 1
            print(f"  {line!r}  -> {dev.parse_tokens(line)}")
    print(f"{n} lines ({n / seconds:.1f} per s). "
          f"{'Sensor is in BURST mode.' if n else 'Nothing streamed: sensor is in POLL mode.'}")


def check(dev):
    for cmd in ("?T", "?I", "?E", "?S", "?U"):
        try:
            print(f"{cmd:4s} -> {dev.command(cmd)!r}")
        except (TimeoutError, ValueError) as e:
            print(f"{cmd:4s} {e}")
    print("\nCompare T with the sensor display. If replies time out, try --listen 3 "
          "(burst mode?) or check IP/port in the sensor's Interface menu.")


def run(dev, rate_hz, csv_path):
    period = 1.0 / rate_hz if rate_hz > 0 else 0.0
    n, last_print = 0, -1.0
    f = writer = None
    if csv_path:
        f = open(csv_path, "w", newline="")
        writer = csv.writer(f)
        writer.writerow(["t_s", "T_e1rh_C"])
    t0 = time.perf_counter()
    next_t = t0
    try:
        while True:
            try:
                T = dev.target_temp()
            except (TimeoutError, ValueError) as e:
                print(f"\n{e}")
                T = math.nan
            t = time.perf_counter() - t0
            n += 1
            if t - last_print >= 0.1:
                last_print = t
                if f:
                    f.flush()
                print(f"\r t = {t:8.3f} s   T = {T:7.1f} °C   {n / t if t else 0:5.1f} Hz   ",
                      end="", flush=True)
            if writer:
                writer.writerow([f"{t:.4f}", f"{T:.1f}"])
            if period:
                next_t += period
                time.sleep(max(0.0, next_t - time.perf_counter()))
    except KeyboardInterrupt:
        t = time.perf_counter() - t0
        print(f"\nStopped. {n} samples in {t:.2f} s = {n / t:.1f} Hz average")
    finally:
        if f:
            f.close()
            print(f"Saved {csv_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Fluke Endurance E1RH pyrometer")
    ap.add_argument("--host", help="sensor IP address (Ethernet/PoE)")
    ap.add_argument("--tcp-port", type=int, default=6363)
    ap.add_argument("--serial", help="COM port of the RS485 adapter instead of Ethernet")
    ap.add_argument("--baud", type=int, default=38400)
    ap.add_argument("--address", type=int, help="multidrop address 1-32 (if enabled)")
    ap.add_argument("--transfer", choices=["poll", "burst"], default="poll")
    ap.add_argument("--check", action="store_true", help="send ?T ?I ?E ?S ?U, show replies")
    ap.add_argument("--listen", type=float, metavar="SEC", help="print streamed lines for SEC s")
    ap.add_argument("--raw", help='send one command, e.g. "?T" or "E=0.95"')
    ap.add_argument("--rate", type=float, default=0, help="samples per second (0 = max)")
    ap.add_argument("--csv", help="log to this CSV")
    a = ap.parse_args()

    if not (a.host or a.serial):
        ap.error("give --host IP (Ethernet) or --serial COMx (RS485)")
    with make(a) as dev:
        print(f"Connected: {a.host + ':' + str(a.tcp_port) if a.host else a.serial}")
        if a.listen:
            listen(dev, a.listen)
        elif a.raw:
            dev.send(a.raw)
            t_end = time.perf_counter() + 1.0
            while time.perf_counter() < t_end:
                line = dev.readline()
                if line:
                    print(repr(line))
        elif a.check:
            check(dev)
        else:
            run(dev, a.rate, a.csv)
    sys.exit(0)
