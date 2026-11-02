"""Fluke 374 FC clamp meter: current over Bluetooth LE (protocol to be reverse-engineered; likely needs 'bleak').

TODO: not implemented yet.
"""

from instruments.common.base import Instrument


class Fluke374FC(Instrument):
    name = "fluke_374fc"

    def __init__(self, **cfg):
        self.cfg = cfg

    def connect(self):
        raise NotImplementedError("Fluke374FC driver not written yet")

    def read(self) -> dict:
        raise NotImplementedError
