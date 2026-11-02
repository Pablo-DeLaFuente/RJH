# RJH — Enthalpy-aware thermal control for Resistive Joule Heating

## Setup
    conda env create -f environment.yml
    conda activate RJH

## Layout
- rjh/devices — instrument drivers (CT3, Fluke E2RH, BK 1900B, Fluke 374 FC)
- rjh/controller — feedforward / observer / MPC
- rjh/calibration — pyrometer calibration
- rjh/heating_cycles — run profiles
- rjh/data_logger — CSV logging
- scripts — entry points
- data — run output (not tracked)
