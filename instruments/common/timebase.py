"""
Shared session clock for every instrument in the RJH rig.

Every sample/frame gets the same four time columns:
  t_s       elapsed seconds since the run started, ms resolution  (12.345)
  t_mmss    the same, human-readable                              (00:12.345)
  wall_iso  real clock time, ms + UTC offset   (2026-11-05T14:03:22.517-07:00)
  epoch_ms  real clock time as Unix milliseconds                 (1793916202517)

Elapsed time comes from time.perf_counter() (monotonic: never jumps if Windows
resyncs its clock). Wall time = wall clock at start + elapsed, so all four
columns come from the SAME tick and always agree. wall_iso / epoch_ms are what
let you line up files from different instruments, programs, or computers later.

Make ONE SessionClock per run and pass it to every logger/thread.
"""

import time
from datetime import datetime

TIME_COLUMNS = ["t_s", "t_mmss", "wall_iso", "epoch_ms"]


def fmt_elapsed(seconds: float) -> str:
    """12.345 -> '00:12.345'; 3723.5 -> '1:02:03.500'"""
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h}:{m:02d}:{s:02d}.{ms:03d}" if h else f"{m:02d}:{s:02d}.{ms:03d}"


class SessionClock:
    def __init__(self):
        # read both clocks back-to-back so they share one reference point
        self._t0 = time.perf_counter()
        self._wall0_ns = time.time_ns()
        self.start = datetime.fromtimestamp(self._wall0_ns / 1e9).astimezone()

    def elapsed(self) -> float:
        return time.perf_counter() - self._t0

    def stamp(self, elapsed: float = None) -> dict:
        """The four time columns for 'now' (or for a given elapsed time)."""
        t = self.elapsed() if elapsed is None else elapsed
        wall_ns = self._wall0_ns + int(t * 1e9)
        wall = datetime.fromtimestamp(wall_ns / 1e9).astimezone()
        return {
            "t_s": f"{t:.3f}",
            "t_mmss": fmt_elapsed(t),
            "wall_iso": wall.isoformat(timespec="milliseconds"),
            "epoch_ms": wall_ns // 1_000_000,
        }
