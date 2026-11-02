"""
Fluke Connect BLE protocol decoding for the Fluke 37x FC clamp meters.

Protocol details come from community reverse-engineering (not Fluke docs):
  - jwanga/fluke-connect-client, docs/PROTOCOL.md  (binary 290F + ASCII 2901 layouts)
  - LambdaFox "ClampConnect" (376 FC: service 1800 / notify 2901, UTF-8 display text)
The 374 FC shares the 376 FC platform, so the same UUIDs are expected. Run
discover.py once to confirm on your unit.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

# --- UUIDs (base B698xxxx-7562-11E2-B50D-00163E46F8FE) -----------------------
def _u(slot: str) -> str:
    return f"b698{slot.lower()}-7562-11e2-b50d-00163e46f8fe"

SERVICE_READING = _u("1800")
CHAR_ASCII      = _u("2901")   # 17 B: format byte + 16 ASCII display chars (notify)
CHAR_BINARY     = _u("290F")   # 8 B (or 16 B w/ secondary) binary record (notify)
SERVICE_CONN    = _u("1801")
CHAR_LOCATOR    = _u("2905")   # write 1/0 -> locator LED

# --- Binary record enums (subset; see fluke-connect-client src/protocol/enums.rs)
STATE = {0: "normal", 1: "blank", 2: "inactive", 3: "invalid", 4: "OL",
         5: "AD_overload", 6: "open_tc", 7: "discharge", 8: "leads",
         9: "greater_than", 10: "missing_phase", 11: "error", 12: "less_than",
         13: "empty"}
MAGNITUDE = {0: ("", 1.0), 1: ("G", 1e9), 2: ("M", 1e6), 3: ("k", 1e3),
             4: ("m", 1e-3), 5: ("u", 1e-6), 6: ("n", 1e-9), 7: ("p", 1e-12)}
UNIT = {0: "", 1: "V AC", 2: "V DC", 3: "A AC", 4: "A DC", 5: "Hz", 7: "degC",
        8: "degF", 11: "ohm", 14: "s", 15: "F", 18: "W", 34: "A AC+DC", 35: "%"}

ASCII_PREFIX = {"n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6, " ": 1.0, "": 1.0}


@dataclass
class Reading:
    value: Optional[float]      # SI base units (A, V, ...) ; None if OL/blank/invalid
    unit: str                   # e.g. "A DC", "A AC", "V DC"
    state: str                  # "normal", "OL", "blank", ...
    display: str = ""           # what the meter shows, e.g. "0.7 A dc"
    source: str = ""            # "binary" or "ascii"
    raw_hex: str = ""
    extra: dict = field(default_factory=dict)


# --- Binary (290F) -----------------------------------------------------------
def parse_binary(data: bytes) -> Optional[Reading]:
    """Decode the primary 8-byte record. Returns None if too short."""
    if len(data) < 8:
        return None
    w0, w1 = struct.unpack_from("<II", data, 0)
    mant   = w0 & 0x1FFFFF
    state  = (w0 >> 21) & 0xF
    dp     = (w0 >> 25) & 0x7
    mag    = (w0 >> 28) & 0x7
    neg    = (w0 >> 31) & 0x1
    unit_c = w1 & 0xFF
    func   = (w1 >> 8) & 0xFF
    attr   = (w1 >> 26) & 0x1F

    st = STATE.get(state, f"state{state}")
    prefix, scale = MAGNITUDE.get(mag, ("?", 1.0))
    unit = UNIT.get(unit_c, f"unit{unit_c}")

    value = None
    disp = st
    if st == "normal" and mant != 0x1FFFFF:
        shown = mant / (10 ** dp) * (-1 if neg else 1)
        value = shown * scale
        disp = f"{shown:.{dp}f} {prefix}{unit}"
    return Reading(value=value, unit=unit, state=st, display=disp,
                   source="binary", raw_hex=data.hex(" "),
                   extra={"function": func, "attribute": attr})


# --- ASCII (2901) ------------------------------------------------------------
def parse_ascii(data: bytes) -> Optional[Reading]:
    """Decode the display-text characteristic.

    Documented layout: 1 format byte (0x00) + 16 chars:
      [0:6] reading (right-justified)  [6] multiplier  [7:11] unit
      [11:13] 'ac'/'dc'                [13] '*' bolt    [14:16] 'in' inrush
    Falls back to a loose whitespace parse if the layout doesn't match.
    """
    if not data:
        return None
    raw = data
    if data[0] in (0x00, 0x01) and len(data) >= 17:
        data = data[1:]
        txt = data.decode("utf-8", errors="replace").replace("\x00", " ")
    else:                                # not the fixed 17-byte layout
        txt = data.decode("utf-8", errors="replace").replace("\x00", " ")
        return _parse_ascii_loose(txt, raw)
    txt16 = txt.ljust(16)

    num_s  = txt16[0:6].strip()
    mult   = txt16[6]
    unit_s = txt16[7:11].strip()
    acdc   = txt16[11:13].strip().upper()

    if not unit_s:                       # layout mismatch -> loose parse
        return _parse_ascii_loose(txt, raw)

    unit = f"{unit_s} {acdc}".strip()
    if num_s.upper().startswith("OL"):
        return Reading(None, unit, "OL", txt.strip(), "ascii", raw.hex(" "))
    try:
        shown = float(num_s)
    except ValueError:
        return Reading(None, unit, "blank" if set(num_s) <= set("- ") else "invalid",
                       txt.strip(), "ascii", raw.hex(" "))
    value = shown * ASCII_PREFIX.get(mult, 1.0)
    return Reading(value, unit, "normal", " ".join(txt.split()), "ascii", raw.hex(" "),
                   extra={"bolt": txt16[13] == "*", "inrush": txt16[14:16] == "in"})


def _parse_ascii_loose(txt: str, raw: bytes) -> Reading:
    import re
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*([numkM]?)\s*([A-Za-z]+)?\s*(ac|dc|AC|DC)?", txt)
    if not m:
        st = "OL" if "OL" in txt else "invalid"
        return Reading(None, "", st, txt.strip(), "ascii", raw.hex(" "))
    shown = float(m.group(1))
    value = shown * ASCII_PREFIX.get(m.group(2) or "", 1.0)
    unit = f"{m.group(3) or ''} {(m.group(4) or '').upper()}".strip()
    return Reading(value, unit, "normal", " ".join(txt.split()), "ascii", raw.hex(" "))
