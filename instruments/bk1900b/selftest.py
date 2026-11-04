"""
BK 1900B open-circuit self-test: checks every command we send (output) and
every reading we get back (input), one step at a time so you can compare
with the front panel.

Run with NOTHING connected to the output terminals (open circuit):
    python -m instruments.bk1900b.selftest --port COM4            # press Enter between steps
    python -m instruments.bk1900b.selftest --port COM4 --auto 3   # 3 s per step, no Enter

What it does:
  1. Reads max ratings, setpoints, display (input only, nothing changes)
  2. Output OFF, then steps the voltage + current setpoints (VOLT / CURR)
     and reads them back with GETS -> panel setpoints should follow
  3. Output ON at 2.0 V -> GETD should read ~2.0 V, 0.00 A, CV (open circuit)
     then 5.0 V -> ~5.0 V
  4. Output OFF -> GETD voltage should fall toward 0 V
  5. Restores your original setpoints and leaves the output OFF
     (also on Ctrl+C or any error)

Current can't flow on an open circuit, so I always reads 0.00 A here; the
current *setpoint* is still checked through GETS and the panel.
"""

import argparse
import time

from instruments.bk1900b.bk1900b import BK1900B

V_STEPS = [1.0, 2.5, 5.0]     # V   (kept low on purpose)
I_STEPS = [1.0, 5.0, 10.0]    # A


class Stepper:
    def __init__(self, auto_s):
        self.auto_s = auto_s
        self.n = 0

    def step(self, msg):
        self.n += 1
        print(f"\n[{self.n}] {msg}")

    def pause(self, look_for):
        print(f"    panel should show: {look_for}")
        if self.auto_s:
            time.sleep(self.auto_s)
        else:
            input("    Enter to continue (Ctrl+C to abort) ")


def check(label, got, want, tol):
    ok = abs(got - want) <= tol
    print(f"    {label:12s} {got:7.2f}   expected {want:6.2f}   {'PASS' if ok else 'FAIL'}")
    return ok


def settle(dev, target, tol, timeout=15.0):
    """Poll GETD until V is within tol of target (or timeout). With no load the
    supply can raise V quickly but can only bleed it DOWN slowly (it sources,
    it doesn't sink), so a downward step on an open circuit takes seconds."""
    t0 = time.perf_counter()
    while True:
        V, I, mode = dev.display()
        dt = time.perf_counter() - t0
        if abs(V - target) <= tol or dt > timeout:
            print(f"    settled in {dt:4.1f} s" if abs(V - target) <= tol
                  else f"    NOT settled after {timeout:.0f} s")
            print(f"    GETD         {V:7.2f} V  {I:6.2f} A  {'CC' if mode else 'CV'}")
            return V, I, mode
        time.sleep(0.25)


def display(dev):
    V, I, mode = dev.display()
    print(f"    GETD         {V:7.2f} V  {I:6.2f} A  {'CC' if mode else 'CV'}")
    return V, I, mode


def main(port, baud, auto_s):
    s = Stepper(auto_s)
    results = []
    with BK1900B(port, baud) as dev:
        V0, I0 = dev.setpoints()
        try:
            # 1. input only
            s.step("Read-only: max ratings, setpoints, display")
            print(f"    GMAX         {dev.V_max} V, {dev.I_max} A")
            print(f"    GETS         {V0} V, {I0} A   (saved, restored at the end)")
            display(dev)
            s.pause("nothing changes")

            # 2. setpoints with output off
            s.step("Output OFF")
            dev.output_off()
            time.sleep(0.5)
            display(dev)
            s.pause("output indicator OFF")

            for V, I in zip(V_STEPS, I_STEPS):
                s.step(f"Set {V} V, {I} A (output still OFF)")
                dev.set_voltage(V)
                dev.set_current(I)
                time.sleep(0.3)
                Vs, Is = dev.setpoints()
                results.append(check("GETS V", Vs, V, 0.05))
                results.append(check("GETS I", Is, I, 0.05))
                s.pause(f"setpoints {V:.1f} V / {I:.1f} A")

            # 3. output on, open circuit -> voltage follows setpoint, I = 0
            for V in (2.0, 5.0):
                s.step(f"Output ON at {V} V, 1.0 A limit (open circuit)")
                dev.set_current(1.0)
                dev.set_voltage(V)
                dev.output_on()
                Vm, Im, mode = settle(dev, V, 0.15)
                results.append(check("GETD V", Vm, V, 0.15))
                results.append(check("GETD I", Im, 0.0, 0.05))
                results.append(check("mode CV=0", mode, 0, 0))
                s.pause(f"output ON, ~{V:.2f} V, 0.00 A, CV")

            # 4. output off
            s.step("Output OFF")
            dev.output_off()
            print("    (no load -> output caps bleed down slowly; watching for 15 s)")
            settle(dev, 0.0, 0.2)
            s.pause("output OFF")

        finally:
            # 5. always leave it safe
            try:
                dev.output_off()
                dev.set_current(I0)
                dev.set_voltage(V0)
                print(f"\nRestored setpoints {V0} V / {I0} A, output OFF.")
            except Exception as e:
                print(f"\nWARNING: cleanup failed ({e}). Turn the output off at the panel.")

    n_ok = sum(results)
    print(f"\n{n_ok}/{len(results)} checks passed.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="BK 1900B open-circuit self-test")
    ap.add_argument("--port", required=True, help="e.g. COM4")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--auto", type=float, default=0,
                    help="seconds per step instead of waiting for Enter")
    a = ap.parse_args()
    try:
        main(a.port, a.baud, a.auto)
    except KeyboardInterrupt:
        print("\nAborted (output was turned OFF).")
