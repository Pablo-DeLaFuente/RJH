"""
BK 1900B low -> high sweep (open circuit, nothing connected).

Part 1 - current setpoints (output OFF):
    steps the CURR setpoint 0.1 A -> max (63 A) and reads each back with GETS.
    No current flows; this proves every current target is accepted and stored.

Part 2 - voltage output (output ON, 1 A limit, open circuit):
    steps the voltage UP from 1.0 V (the 1900B minimum) to max (16.2 V) and waits at each point until
    the measured output (GETD) reaches the target. Going up only, because with
    no load the supply can raise voltage fast but can only bleed it down slowly.

Ends with output OFF and your original setpoints restored (also on Ctrl+C/error).

    python -m instruments.bk1900b.sweep --port COM4
    python -m instruments.bk1900b.sweep --port COM4 --v-step 0.1 --csv data/bk_sweep.csv
"""

import argparse
import csv
import time
from pathlib import Path

from instruments.bk1900b.bk1900b import BK1900B

I_TARGETS = [0.1, 0.5, 1, 2, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60]


def frange(start, stop, step):
    n = int(round((stop - start) / step))
    return [round(start + k * step, 2) for k in range(n + 1)]


def wait_for(dev, target, tol, timeout):
    t0 = time.perf_counter()
    while True:
        V, I, mode = dev.display()
        dt = time.perf_counter() - t0
        if abs(V - target) <= tol or dt > timeout:
            return V, I, mode, dt, abs(V - target) <= tol
        time.sleep(0.1)


def main(port, baud, v_start, v_step, tol, csv_path):
    rows = []
    with BK1900B(port, baud) as dev:
        V0, I0 = dev.setpoints()
        Vmax, Imax = dev.V_max or 16.2, dev.I_max or 63.0
        print(f"Max ratings {Vmax} V / {Imax} A. Original setpoints {V0} V / {I0} A saved.")
        try:
            # ---- part 1: current setpoints --------------------------------
            print("\nPart 1: current setpoints (output OFF)")
            print(f"{'target A':>9} {'GETS A':>8}  result")
            dev.output_off()
            for I in [*I_TARGETS, Imax]:
                dev.set_current(I)
                _, Is = dev.setpoints()
                ok = abs(Is - I) <= 0.05
                rows.append({"part": "I_set", "target": I, "measured": Is, "time_s": "", "pass": ok})
                print(f"{I:9.1f} {Is:8.1f}  {'PASS' if ok else 'FAIL'}")

            # ---- part 2: voltage output, low -> high ------------------------
            print("\nPart 2: voltage output, open circuit, 1 A limit")
            dev.set_current(1.0)
            dev.set_voltage(v_start)
            dev.output_on()
            # first point: output caps may still hold charge from earlier
            V, *_ = dev.display()
            if V > v_start + tol:
                print(f"  output still at {V:.2f} V from before, waiting for it to bleed "
                      f"down to {v_start} V (up to 60 s)...")
            print(f"{'target V':>9} {'GETD V':>8} {'GETD A':>7} mode {'reach s':>8}  result")
            targets = frange(v_start, Vmax, v_step)
            if targets[-1] < Vmax:
                targets.append(Vmax)
            for k, Vt in enumerate(targets):
                dev.set_voltage(Vt)
                V, I, mode, dt, ok = wait_for(dev, Vt, tol, 60 if k == 0 else 10)
                rows.append({"part": "V_out", "target": Vt, "measured": V,
                             "time_s": round(dt, 2), "pass": ok})
                print(f"{Vt:9.2f} {V:8.2f} {I:7.2f}  {'CC' if mode else 'CV'}  {dt:8.2f}  "
                      f"{'PASS' if ok else 'FAIL'}")
        finally:
            try:
                dev.output_off()
                dev.set_current(I0)
                dev.set_voltage(V0)
                print(f"\nOutput OFF. Restored setpoints {V0} V / {I0} A.")
            except Exception as e:
                print(f"\nWARNING: cleanup failed ({e}). Turn the output off at the panel.")

    n_ok = sum(r["pass"] for r in rows)
    print(f"{n_ok}/{len(rows)} targets hit (voltage tolerance ±{tol} V).")
    fails = [r for r in rows if not r["pass"]]
    for r in fails:
        print(f"  FAIL {r['part']} target {r['target']} -> {r['measured']}")
    if csv_path:
        csv_path = Path(csv_path).resolve()
        csv_path.parent.mkdir(parents=True, exist_ok=True)  # make data/ if missing
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, ["part", "target", "measured", "time_s", "pass"])
            w.writeheader()
            w.writerows(rows)
        print(f"Saved {csv_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="BK 1900B low->high open-circuit sweep")
    ap.add_argument("--port", required=True, help="e.g. COM4")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--v-start", type=float, default=1.0, help="first voltage [V] (1900B minimum is 1.0)")
    ap.add_argument("--v-step", type=float, default=0.5, help="voltage step [V]")
    ap.add_argument("--tol", type=float, default=0.1, help="voltage pass band [V]")
    ap.add_argument("--csv", help="save results, e.g. data/bk_sweep.csv")
    a = ap.parse_args()
    try:
        main(a.port, a.baud, a.v_start, a.v_step, a.tol, a.csv)
    except KeyboardInterrupt:
        print("\nAborted (output was turned OFF).")
