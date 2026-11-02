# RJH — Enthalpy-aware thermal control for Resistive Joule Heating

## Setup
    conda env create -f environment.yml
    conda activate RJH

Run everything from the repo root, e.g.

    python -m instruments.connect            # find every instrument's port on this computer
    python -m instruments.ct3.ct3 --port COM3 --csv data/run1.csv
    python -m instruments.manager            # connect + read all enabled instruments

## Layout
- `instruments/` — one folder per instrument, each with all the code that instrument needs
  - `instruments.yaml` — baud rates, IPs, enable flags, and how to recognize each instrument
  - `connect.py` — auto-detects each instrument's port; saves to `instruments.local.yaml` (per computer, not in git)
  - `manager.py` — connect / read / close all enabled instruments (runs detection automatically)
  - `common/` — shared `Instrument` base class and helpers
  - `ct3/` — Micro-Epsilon CT3 pyrometer (serial) — **working**
  - `fluke_e1rh/` — Fluke Endurance E1RH: temperature, video, acquisition, TCP proxy — *stub*
  - `bk1900b/` — BK Precision 1900B DC supply — *stub*
  - `fluke_374fc/` — Fluke 374 FC clamp meter (Bluetooth LE) — *stub*
- `controller/` — control code (Python and MATLAB): feedforward, observer, MPC
- `gui/` — front end (live video, readouts, graphs, CSV export)
- `data/` — run output (not tracked by git)
- `tests/`
