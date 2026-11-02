"""Fluke 374 FC clamp meter: current over Bluetooth LE (bleak).

The meter pushes ~4 readings/s by BLE notification. A background thread keeps
the connection alive (auto-reconnect) and stores the latest value, so read()
is instant and never blocks the manager's sample loop.

Before first use:
  - Close the Fluke Connect phone app (the meter allows ONE BLE connection).
  - Don't pair it in Windows Settings; no pairing is needed.
  - Run the one-time check:   python -m instruments.fluke_374fc.discover
    then put the printed address in instruments.yaml -> fluke_374fc.ble_address
    (optional; without it the driver scans, which takes ~10-30 s).

Usage (from the repo root):
    python -m instruments.fluke_374fc.clamp_ble                       # print live
    python -m instruments.fluke_374fc.clamp_ble --csv data/run1_I.csv
    python -m instruments.fluke_374fc.clamp_ble --raw                 # show bytes
    python -m instruments.fluke_374fc.clamp_ble --measure-R           # welder R_total:
        dial on ohms, leads across the circuit (welder OFF); averages 5 s

In the manager it appears as columns:
    I_clamp_A      latest current [A] (NaN if OL / blank / not an amps function)
    I_clamp_age_s  seconds since that value arrived (spot dropouts / stale data)

Protocol: community reverse-engineering, see protocol.py.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import math
import threading
import time
from typing import Callable, Optional, Tuple

from bleak import BleakClient, BleakScanner

from instruments.common.base import Instrument
from instruments.fluke_374fc.protocol import (CHAR_ASCII, CHAR_BINARY,
                                              SERVICE_READING, Reading,
                                              parse_ascii, parse_binary)

Callback = Callable[[float, Reading], None]


# ---------------------------------------------------------------------------
# async BLE client
# ---------------------------------------------------------------------------
async def find_meter(name_hint: str = "374", timeout: float = 30.0):
    """Return the first device advertising the Fluke reading service or name hint."""
    found = {}

    def _cb(dev, adv):
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        name = adv.local_name or dev.name or ""
        if SERVICE_READING in uuids or name_hint.lower() in name.lower():
            found.setdefault(dev.address, (dev, name))

    async with BleakScanner(detection_callback=_cb):
        t0 = time.monotonic()
        while not found and time.monotonic() - t0 < timeout:
            await asyncio.sleep(0.5)
    return next(iter(found.values()), (None, ""))


class _BleSession:
    """Subscribes to the binary record (290F) and ASCII display (2901).
    Binary is preferred; ASCII is used only until/unless binary shows up."""

    def __init__(self, address: str = "", name_hint: str = "374",
                 on_reading: Optional[Callback] = None, t0: Optional[float] = None,
                 on_status: Callable[[str], None] = print):
        self.address = address
        self.name_hint = name_hint
        self.on_reading = on_reading
        self.on_status = on_status
        self.t0 = time.perf_counter() if t0 is None else t0
        self.connected_address = ""
        self._got_binary = False

    def _emit(self, r: Optional[Reading]):
        if r is not None and self.on_reading:
            self.on_reading(time.perf_counter() - self.t0, r)

    def _on_binary(self, _h, data: bytearray):
        r = parse_binary(bytes(data))
        if r is not None:
            self._got_binary = True
            self._emit(r)

    def _on_ascii(self, _h, data: bytearray):
        if not self._got_binary:
            self._emit(parse_ascii(bytes(data)))

    async def run(self, stop: asyncio.Event, reconnect: bool = True):
        while not stop.is_set():
            target = self.address
            if not target:
                self.on_status("[374FC] scanning...")
                dev, name = await find_meter(self.name_hint)
                if dev is None:
                    self.on_status("[374FC] not found (meter on? BT on? phone app closed?)")
                    continue
                target = dev
                self.on_status(f"[374FC] found {name!r} at {dev.address} "
                               f"(put this in instruments.yaml ble_address)")
            lost = asyncio.Event()
            try:
                async with BleakClient(target, timeout=20.0,
                                       disconnected_callback=lambda _c: lost.set()) as c:
                    self.connected_address = c.address
                    chars = {ch.uuid.lower() for s in c.services for ch in s.characteristics}
                    subs = [u for u in (CHAR_BINARY, CHAR_ASCII) if u in chars]
                    if not subs:
                        raise RuntimeError("reading characteristics not found; "
                                           "run python -m instruments.fluke_374fc.discover")
                    if CHAR_BINARY in subs:
                        await c.start_notify(CHAR_BINARY, self._on_binary)
                    if CHAR_ASCII in subs:
                        await c.start_notify(CHAR_ASCII, self._on_ascii)
                    self.on_status(f"[374FC] connected {c.address}, "
                                   f"streaming {[u[4:8] for u in subs]}")
                    waits = [asyncio.ensure_future(stop.wait()),
                             asyncio.ensure_future(lost.wait())]
                    await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
                    for w in waits:
                        w.cancel()
            except Exception as e:  # noqa: BLE001
                self.on_status(f"[374FC] connection error: {e}")
            if not reconnect or stop.is_set():
                break
            self.on_status("[374FC] link lost, reconnecting...")
            await asyncio.sleep(1.0)


# ---------------------------------------------------------------------------
# Instrument driver (sync, used by instruments.manager)
# ---------------------------------------------------------------------------
class Fluke374FC(Instrument):
    name = "fluke_374fc"

    def __init__(self, ble_address: str = "", name_hint: str = "374",
                 connect_timeout: float = 45.0, on_reading: Optional[Callback] = None):
        self.ble_address = ble_address or ""
        self.port = self.ble_address or "BLE scan"   # shown by the manager
        self.connect_timeout = connect_timeout
        self._user_cb = on_reading
        self._latest: Optional[Tuple[float, Reading]] = None
        self._lock = threading.Lock()
        self._first = threading.Event()
        self._warned_unit = False
        self._session = _BleSession(self.ble_address, name_hint, on_reading=self._cb)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop: Optional[asyncio.Event] = None
        self._thread: Optional[threading.Thread] = None

    def _cb(self, t: float, r: Reading):
        with self._lock:
            self._latest = (time.perf_counter(), r)
        self._first.set()
        if self._user_cb:
            self._user_cb(t, r)

    # ---- Instrument interface -------------------------------------------
    def connect(self):
        """Start the BLE thread and wait for the first reading."""
        ready = threading.Event()

        def _run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._stop = asyncio.Event()
            ready.set()
            self._loop.run_until_complete(self._session.run(self._stop))
            self._loop.close()

        self._thread = threading.Thread(target=_run, name="Fluke374FC", daemon=True)
        self._thread.start()
        ready.wait()
        if not self._first.wait(self.connect_timeout):
            self.close()
            raise TimeoutError(f"no reading from 374 FC within {self.connect_timeout:.0f} s")
        self.port = self._session.connected_address or self.port

    def read(self) -> dict:
        lt = self.latest()
        if lt is None:
            return {"I_clamp_A": math.nan, "I_clamp_age_s": math.nan}
        t_rx, r = lt
        amps = r.state == "normal" and r.value is not None and r.unit.upper().startswith("A")
        if r.state == "normal" and not amps and not self._warned_unit:
            print(f"\n[374FC] meter is on {r.unit!r}, not amps - turn the dial to A")
            self._warned_unit = True
        return {"I_clamp_A": r.value if amps else math.nan,
                "I_clamp_age_s": time.perf_counter() - t_rx}

    def close(self):
        if self._loop and self._stop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread:
            self._thread.join(5.0)

    # ---- extras ---------------------------------------------------------
    def latest(self) -> Optional[Tuple[float, Reading]]:
        """(perf_counter time received, Reading) or None."""
        with self._lock:
            return self._latest


# ---------------------------------------------------------------------------
# standalone logger: every BLE notification -> screen / CSV
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Log Fluke 374 FC readings over BLE")
    ap.add_argument("--address", default="", help="BLE address (skips the scan)")
    ap.add_argument("--csv", help="save every reading, e.g. data/run1_I.csv")
    ap.add_argument("--raw", action="store_true", help="also print raw bytes")
    ap.add_argument("--measure-R", nargs="?", const=5.0, type=float, metavar="SECONDS",
                    help="average the ohms reading for SECONDS (default 5) -> R_total")
    a = ap.parse_args()
    if a.measure_R:
        measure_R(a.address or _yaml_address(), a.measure_R)
        return

    f = w = None
    if a.csv:
        f = open(a.csv, "w", newline="")
        w = csv.writer(f)
        w.writerow(["t_s", "value_SI", "unit", "state", "display", "source", "raw_hex"])

    def on_r(t: float, r: Reading):
        v = "-" if r.value is None else f"{r.value:.4g}"
        extra = f"  ({r.source}: {r.raw_hex})" if a.raw else ""
        print(f"{t:9.3f} s  {v:>10} {r.unit:<8} [{r.state}]{extra}")
        if w:
            w.writerow([f"{t:.3f}", "" if r.value is None else r.value, r.unit,
                        r.state, r.display, r.source, r.raw_hex])
            f.flush()  # live CSV for other programs

    async def _go():
        await _BleSession(a.address, on_reading=on_r).run(asyncio.Event())

    try:
        asyncio.run(_go())
    except KeyboardInterrupt:
        pass
    finally:
        if f:
            f.close()
            print(f"Saved {a.csv}")


def _yaml_address() -> str:
    try:
        from instruments import connect
        return (connect.load_yaml(connect.BASE_CONFIG).get("fluke_374fc") or {}).get("ble_address", "")
    except Exception:  # noqa: BLE001
        return ""


def measure_R(address: str, seconds: float):
    """Average the meter's resistance reading. Welder must be OFF / disconnected."""
    import statistics
    vals, t_start = [], [None]

    def on_r(t, r: Reading):
        is_ohm = r.unit.lower().startswith("ohm")
        if not is_ohm:
            print(f"\r  meter shows {r.display!r} - turn the dial to ohms       ", end="", flush=True)
            return
        if r.state != "normal" or r.value is None:
            print(f"\r  {r.state} - leads open? check connections            ", end="", flush=True)
            return
        if t_start[0] is None:
            t_start[0] = t
            print(f"\n  measuring {seconds:g} s...")
        vals.append(r.value)
        print(f"\r  R = {r.value:.4g} ohm   n={len(vals)}   ", end="", flush=True)
        if t - t_start[0] >= seconds:
            stop.set()

    stop = None

    async def _go():
        nonlocal stop
        stop = asyncio.Event()
        await _BleSession(address, on_reading=on_r, on_status=lambda m: None).run(stop)

    print("Welder OFF. Dial on ohms, leads across the full circuit (incl. cables).")
    try:
        asyncio.run(_go())
    except KeyboardInterrupt:
        pass
    if not vals:
        print("\nno resistance readings received")
        return
    R = statistics.fmean(vals)
    sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
    print(f"\n\nR_total = {R:.4g} ohm  (sd {sd:.2g}, n={len(vals)})")
    print(f"use:  python -m instruments.manager --R {R:.4g}")
    print(f"  or set run.R_total_ohm: {R:.4g} in instruments.yaml")


if __name__ == "__main__":
    main()
