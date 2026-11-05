"""
Fluke E1RH live video (MJPEG over HTTP from the sensor's built-in web server).

Stream address (found 2026-11-05 in the sensor's own web page script,
http://<IP>/script/core.js, line 6):
    http://<IP>/camera?action=stream&resolution=720p
Other endpoints in that script: /status and /config (JSON).

    python -m instruments.fluke_e1rh.video --host 192.168.42.132
    python -m instruments.fluke_e1rh.video --host 192.168.42.132 --save data/e1rh_video.avi
    python -m instruments.fluke_e1rh.video --url <full stream URL>      # any other address
"""

import argparse
import time
import urllib.request

import cv2

STREAM_PATH = "/camera?action=stream&resolution=720p"   # confirmed from core.js

GUESS_PATHS = [STREAM_PATH, "/", "/video", "/video.mjpg", "/mjpeg", "/stream", "/video.cgi",
               "/videostream.cgi", "/cam.mjpg", "/live"]


def find_stream(host, timeout=2.0):
    """Return the first URL that answers with an MJPEG (multipart) stream."""
    for path in GUESS_PATHS:
        url = f"http://{host}{path}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                ctype = r.headers.get("Content-Type", "")
                print(f"  {url:40s} {r.status}  {ctype}")
                if "multipart" in ctype or "mjpeg" in ctype.lower():
                    return url
        except Exception as e:
            print(f"  {url:40s} {type(e).__name__}")
    return None


def show(url, save=None, max_retries=20):
    """Show (and optionally record) the stream. The E1RH closes the stream every
    so often ("Stream ends prematurely"), so on a drop we reconnect and carry on;
    give up only after max_retries drops in a row with no frames in between."""
    writer = None
    n, drops, fails_in_a_row = 0, 0, 0
    t0 = time.perf_counter()
    print("q in the video window to quit.")
    quit_ = False
    while not quit_:
        cap = cv2.VideoCapture(url)
        if not cap.isOpened():
            fails_in_a_row += 1
            if fails_in_a_row > max_retries:
                print(f"Could not open {url} after {max_retries} tries")
                break
            time.sleep(0.5)
            continue
        while True:
            ok, frame = cap.read()
            if not ok:
                drops += 1
                fails_in_a_row += 1
                print(f"stream dropped after {n} frames total -> reconnecting "
                      f"({drops} drops so far)")
                break
            fails_in_a_row = 0
            n += 1
            if save and writer is None:
                h, w = frame.shape[:2]
                writer = cv2.VideoWriter(save, cv2.VideoWriter_fourcc(*"MJPG"), 30, (w, h))
            if writer:
                writer.write(frame)
            fps = n / (time.perf_counter() - t0)
            cv2.putText(frame, f"{fps:4.1f} fps  drops {drops}", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
            cv2.imshow("E1RH", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                quit_ = True
                break
        cap.release()
        if fails_in_a_row > max_retries:
            print("Too many drops in a row, stopping.")
            break
    t = time.perf_counter() - t0
    print(f"{n} frames in {t:.1f} s ({n / t if t else 0:.1f} fps), {drops} drops")
    if writer:
        writer.release()
        print(f"Saved {save}")
    cv2.destroyAllWindows()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Fluke E1RH video")
    ap.add_argument("--host", help="sensor IP")
    ap.add_argument("--find", action="store_true", help="try common MJPEG addresses")
    ap.add_argument("--url", help="full stream URL")
    ap.add_argument("--save", help="record to this .avi")
    a = ap.parse_args()
    if a.find:
        if not a.host:
            ap.error("--find needs --host")
        url = find_stream(a.host)
        print(f"\nStream: {url}" if url else "\nNo MJPEG stream found at the guessed paths.")
        if url:
            show(url, a.save)
    elif a.url:
        show(a.url, a.save)
    elif a.host:
        show(f"http://{a.host}{STREAM_PATH}", a.save)
    else:
        ap.error("give --host (uses the known stream address) or --url")
