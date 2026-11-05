"""
Fluke E1RH acquisition: record temperature + video together on one clock.

    python -m instruments.fluke_e1rh.acquisition --host 192.168.42.132
    python -m instruments.fluke_e1rh.acquisition --host 192.168.42.132 --name run1 --rate 50
    python -m instruments.fluke_e1rh.acquisition --host 192.168.42.132 --no-video
    python -m instruments.fluke_e1rh.acquisition --host 192.168.42.132 --duration 60

Stop with q in the preview window or Ctrl+C in the terminal.

Each run gets its own folder:  data/<date_time>_<name>/
  temperature.csv  t_s, t_mmss, wall_iso, epoch_ms, T_e1rh_C        (every reading)
  frames.csv       frame, t_s, t_mmss, wall_iso, epoch_ms, T_e1rh_C (one row per video
                   frame: when it arrived + the newest temperature at that moment)
  video.avi        the raw frames (MJPG). Its built-in frame rate is only nominal;
                   use frames.csv for the real time of each frame.
  run_info.json    start time, sensor settings (E, slope, unit, Tint), counts, drops

How it works (3 threads sharing one SessionClock):
  temperature thread : polls ?T at --rate Hz, writes temperature.csv
  video thread       : reads the MJPEG stream, reconnects on drops, writes video.avi
                       + frames.csv
  main thread        : preview window (OpenCV windows must live in the main thread)
                       and the status line
"""

import argparse
import csv
import json
import math
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2

from instruments.common.timebase import TIME_COLUMNS, SessionClock
from instruments.fluke_e1rh.e1rh import E1RH
from instruments.fluke_e1rh.video import STREAM_PATH


class Shared:
    """Values the threads hand to each other (simple attributes + one lock)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.T = math.nan          # newest temperature
        self.n_temp = 0
        self.frame = None          # newest video frame (for the preview)
        self.n_frames = 0
        self.drops = 0
        self.errors = []
        self.n_temp_err = 0
        self.video_state = "off"


def temperature_loop(dev: E1RH, clock: SessionClock, sh: Shared, path: Path, rate_hz: float):
    period = 1.0 / rate_hz if rate_hz > 0 else 0.0
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([*TIME_COLUMNS, "T_e1rh_C"])
        next_t = time.perf_counter()
        last_flush = 0.0
        while not sh.stop.is_set():
            try:
                T = dev.target_temp()
            except Exception as e:           # keep logging through a bad read
                T = math.nan
                sh.n_temp_err += 1
                if len(sh.errors) < 50:
                    sh.errors.append(f"t={clock.elapsed():.2f} temp: {e}")
            st = clock.stamp()                # stamp right after the reading arrives
            w.writerow([*st.values(), "" if math.isnan(T) else f"{T:.1f}"])
            with sh.lock:
                sh.T = T
                sh.n_temp += 1
            now = time.perf_counter()
            if now - last_flush > 0.5:        # live CSV other programs can read
                f.flush()
                last_flush = now
            if period:
                next_t += period
                time.sleep(max(0.0, next_t - time.perf_counter()))


def video_loop(url: str, clock: SessionClock, sh: Shared, video_path: Path, frames_path: Path,
               max_fails=20):
    writer = None
    fails = 0
    with open(frames_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["frame", *TIME_COLUMNS, "T_e1rh_C"])
        while not sh.stop.is_set():
            sh.video_state = "connecting"
            # 5 s limits on opening and on each read, so a stuck stream can't hang us
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG,
                                   [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000,
                                    cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000])
            if not cap.isOpened():
                sh.video_state = f"open failed ({fails + 1})"
                fails += 1
                if fails > max_fails:
                    sh.errors.append(f"video: could not open {url}")
                    break
                time.sleep(0.5)
                continue
            while not sh.stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    sh.drops += 1
                    fails += 1
                    break                      # reconnect
                st = clock.stamp()
                fails = 0
                sh.video_state = "ok"
                if writer is None:
                    h, wid = frame.shape[:2]
                    writer = cv2.VideoWriter(str(video_path),
                                             cv2.VideoWriter_fourcc(*"MJPG"), 30, (wid, h))
                writer.write(frame)
                with sh.lock:
                    T = sh.T
                    sh.frame = frame
                    sh.n_frames += 1
                    n = sh.n_frames
                w.writerow([n, *st.values(), "" if math.isnan(T) else f"{T:.1f}"])
            cap.release()
            f.flush()
            if fails > max_fails:
                sh.errors.append("video: too many drops in a row, video stopped")
                break
    if writer:
        writer.release()


def sensor_settings(dev: E1RH) -> dict:
    out = {}
    for key, fn in (("emissivity", dev.emissivity), ("slope", dev.slope),
                    ("unit", dev.unit), ("T_internal_C", dev.internal_temp)):
        try:
            out[key] = fn()
        except Exception as e:
            out[key] = f"error: {e}"
    return out


def main(host, tcp_port, name, out_dir, rate_hz, video=True, preview=True, duration=None):
    run_dir = Path(out_dir) / f"{datetime.now():%Y-%m-%d_%H%M%S}{'_' + name if name else ''}"
    run_dir.mkdir(parents=True, exist_ok=True)
    url = f"http://{host}{STREAM_PATH}"
    sh = Shared()

    with E1RH(mode="tcp", host=host, tcp_port=tcp_port) as dev:
        settings = sensor_settings(dev)
        clock = SessionClock()                 # t = 0 for every file in this run
        print(f"Recording to {run_dir.resolve()}")
        print(f"Sensor: {settings}")
        threads = [threading.Thread(target=temperature_loop, name="temperature", daemon=True,
                                    args=(dev, clock, sh, run_dir / "temperature.csv", rate_hz))]
        if video:
            threads.append(threading.Thread(target=video_loop, name="video", daemon=True,
                                            args=(url, clock, sh, run_dir / "video.avi",
                                                  run_dir / "frames.csv")))
        for th in threads:
            th.start()

        print("Stop: q in the preview window, or Ctrl+C here.\n")
        try:
            while not sh.stop.is_set():
                t = clock.elapsed()
                with sh.lock:
                    T, nT, nF, frame = sh.T, sh.n_temp, sh.n_frames, sh.frame
                print(f"\r t = {t:8.3f} s   T = {T:7.1f} °C   temp {nT / t if t else 0:5.0f} Hz"
                      f"  err {sh.n_temp_err}   video {nF / t if t else 0:4.1f} fps "
                      f"[{sh.video_state}]  drops {sh.drops}   ",
                      end="", flush=True)
                if preview and frame is not None:
                    shown = frame.copy()
                    cv2.putText(shown, f"t {t:7.2f} s   T {T:7.1f} C", (10, 40),
                                cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 0), 2)
                    cv2.imshow("E1RH acquisition", shown)
                    if cv2.waitKey(30) & 0xFF == ord("q"):
                        break
                else:
                    time.sleep(0.1)
                if duration and t >= duration:
                    break
        except KeyboardInterrupt:
            pass
        finally:
            sh.stop.set()
            for th in threads:
                th.join(timeout=6)
                if th.is_alive():
                    print(f"\n({th.name} thread still blocked; leaving it - files are flushed)")
            cv2.destroyAllWindows()

    t = clock.elapsed()
    info = {
        "start_wall_iso": clock.start.isoformat(timespec="milliseconds"),
        "duration_s": round(t, 3),
        "host": host, "tcp_port": tcp_port, "video_url": url if video else None,
        "temp_rate_requested_hz": rate_hz,
        "temp_samples": sh.n_temp, "temp_rate_actual_hz": round(sh.n_temp / t, 2) if t else None,
        "video_frames": sh.n_frames, "video_fps_actual": round(sh.n_frames / t, 2) if t else None,
        "video_drops": sh.drops,
        "sensor_settings": settings,
        "temp_errors": sh.n_temp_err,
        "errors": sh.errors[:50],
    }
    (run_dir / "run_info.json").write_text(json.dumps(info, indent=2))
    print(f"\n\n{sh.n_temp} temperature samples, {sh.n_frames} frames, {sh.drops} drops "
          f"in {t:.1f} s")
    if sh.errors:
        print(f"{sh.n_temp_err} temperature errors. First few:")
        for e in sh.errors[:5]:
            print("   ", e)
    print(f"Saved {run_dir.resolve()}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="E1RH temperature + video recorder")
    ap.add_argument("--host", required=True, help="sensor IP, e.g. 192.168.42.132")
    ap.add_argument("--tcp-port", type=int, default=6363)
    ap.add_argument("--name", default="", help="label added to the run folder name")
    ap.add_argument("--out", default="data", help="parent folder for runs (default data)")
    ap.add_argument("--rate", type=float, default=100, help="temperature samples/s (default 100)")
    ap.add_argument("--no-video", action="store_true", help="temperature only")
    ap.add_argument("--no-preview", action="store_true", help="record video without a window")
    ap.add_argument("--duration", type=float, help="stop after this many seconds")
    a = ap.parse_args()
    main(a.host, a.tcp_port, a.name, a.out, a.rate, video=not a.no_video,
         preview=not a.no_preview, duration=a.duration)
