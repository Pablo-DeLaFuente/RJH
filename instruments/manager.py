"""Connect to, read, and close every instrument enabled in instruments.yaml.

Ports are detected automatically on start-up (see instruments/connect.py),
so the same code works on any computer.

    python -m instruments.manager                          # auto: match the slowest instrument
    python -m instruments.manager --csv data/run1.csv
    python -m instruments.manager --rate 100               # fixed 100 Hz instead
    python -m instruments.manager --rate 0                 # no pacing, as fast as possible

Sampling rate (auto mode): on start-up each instrument is timed over a few
dozen reads. The sample period is set by the SLOWEST instrument (plus a 10%
margin), so every row has a fresh value from every instrument. Instruments are
read in parallel, so the period is the slowest one's time, not the sum.
A fixed minimum can be forced per instrument with `min_period_ms:` in
instruments.yaml (e.g. if the manual says it only updates every 50 ms).
"""

import csv
import math
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from instruments import connect
from instruments.ct3.ct3 import CT3
from instruments.fluke_e1rh.e1rh import E1RH
from instruments.bk1900b.bk1900b import BK1900B
from instruments.fluke_374fc.clamp_ble import Fluke374FC

REGISTRY = {
    "ct3": CT3,
    "fluke_e1rh": E1RH,
    "bk1900b": BK1900B,
    "fluke_374fc": Fluke374FC,
}


def load_config(autodetect=True) -> dict:
    """instruments.yaml + this computer's detected ports."""
    base = connect.load_yaml(connect.BASE_CONFIG)
    if autodetect:
        found = connect.detect(base=base, verbose=False)
        connect.save_local(found)
        missing = [n for n, i in found.items() if not i.get("port")]
        if missing:
            connect.print_table(found)
            raise RuntimeError(f"Not found: {', '.join(missing)}. Check cables/drivers, "
                               "or set enabled: false in instruments.yaml.")
    local = connect.load_yaml(connect.LOCAL_CONFIG)
    for name, section in local.items():
        if name in base:
            base[name].update(section)
    return base


class InstrumentManager:
    def __init__(self, autodetect=True):
        cfg = load_config(autodetect)
        enabled = {n: sec for n, sec in cfg.items() if sec.get("enabled")}
        self.instruments = {n: REGISTRY[n].from_config(sec) for n, sec in enabled.items()}
        self.min_period_s = {n: sec.get("min_period_ms", 0) / 1000 for n, sec in enabled.items()}
        self._pool = ThreadPoolExecutor(max_workers=max(1, len(self.instruments)))

    def connect_all(self):
        for name, inst in self.instruments.items():
            inst.connect()
            print(f"connected: {name} ({getattr(inst, 'port', '')})")

    @staticmethod
    def _safe_read(name, inst) -> dict:
        try:
            return inst.read()
        except Exception as e:  # one bad read shouldn't kill the run
            print(f"\n{name} read failed: {e}")
            return {}

    def read_all(self) -> dict:
        """Read every instrument at the same moment (in parallel) and merge."""
        if len(self.instruments) == 1:
            (name, inst), = self.instruments.items()
            return self._safe_read(name, inst)
        futures = [self._pool.submit(self._safe_read, n, i) for n, i in self.instruments.items()]
        row = {}
        for fut in futures:
            row.update(fut.result())
        return row

    def measure_periods(self, n_reads=30) -> dict:
        """Time each instrument's read(); returns {name: seconds per read}.

        Uses the 95th percentile (not the average) so occasional slow USB
        replies don't make the loop fall behind.
        """
        periods = {}
        for name, inst in self.instruments.items():
            times = []
            for _ in range(n_reads):
                t = time.perf_counter()
                self._safe_read(name, inst)
                times.append(time.perf_counter() - t)
            p95 = statistics.quantiles(times, n=20)[-1] if len(times) > 1 else times[0]
            periods[name] = max(p95, self.min_period_s.get(name, 0))
        return periods

    def auto_period(self, margin=1.10) -> float:
        """Sample period set by the slowest instrument, rounded up to 1 ms."""
        periods = self.measure_periods()
        print("\ninstrument     time per read")
        for name, p in periods.items():
            print(f"{name:14s} {p * 1000:7.2f} ms")
        slowest = max(periods, key=periods.get)
        period = math.ceil(periods[slowest] * margin * 1000) / 1000
        print(f"-> sampling every {period * 1000:.0f} ms ({1 / period:.0f} Hz), "
              f"set by {slowest}\n")
        return period

    def close_all(self):
        self._pool.shutdown(wait=False)
        for inst in self.instruments.values():
            try:
                inst.close()
            except Exception:
                pass

    def __enter__(self):
        self.connect_all()
        return self

    def __exit__(self, *exc):
        self.close_all()


def run(rate_hz=None, csv_path=None, print_hz=10.0):
    """Sample all instruments.

    rate_hz=None -> auto: period set by the slowest instrument
    rate_hz=0    -> no pacing, as fast as possible
    rate_hz>0    -> fixed rate

    Every sample goes to the CSV; the screen only updates print_hz times per
    second, because printing every sample would slow the loop down.
    """
    f = writer = None
    n = 0
    with InstrumentManager() as mgr:
        if rate_hz is None:
            period = mgr.auto_period()
        else:
            period = 1.0 / rate_hz if rate_hz > 0 else 0.0
        t0 = time.perf_counter()
        next_t = t0
        last_print = 0.0
        try:
            while True:
                row = mgr.read_all()
                t = time.perf_counter() - t0
                n += 1
                if csv_path:
                    if writer is None and row:
                        f = open(csv_path, "w", newline="")
                        writer = csv.DictWriter(f, ["t_s", *row], restval="",
                                                extrasaction="ignore")
                        writer.writeheader()
                    if writer:
                        writer.writerow({"t_s": f"{t:.4f}", **row})
                if t - last_print >= 1.0 / print_hz:
                    last_print = t
                    if f:
                        f.flush()  # live CSV for other programs, ~10x/s
                    vals = "  ".join(f"{k}={v:.1f}" for k, v in row.items())
                    print(f"\rt={t:8.3f} s  {n / t if t else 0:6.0f} Hz  {vals}   ",
                          end="", flush=True)
                if period:
                    next_t += period
                    time.sleep(max(0.0, next_t - time.perf_counter()))
        except KeyboardInterrupt:
            t = time.perf_counter() - t0
            print(f"\n{n} samples in {t:.2f} s = {n / t:.0f} Hz average")
        finally:
            if f:
                f.close()
                print(f"Saved {csv_path}")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Read all enabled instruments")
    ap.add_argument("--rate", type=float, default=None,
                    help="samples per second (default: auto, matched to the slowest "
                         "instrument; 0 = as fast as possible)")
    ap.add_argument("--csv", help="save every sample to this CSV, e.g. data/run1.csv")
    a = ap.parse_args()
    run(a.rate, a.csv)
