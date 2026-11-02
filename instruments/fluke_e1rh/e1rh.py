"""Fluke Endurance E1RH pyrometer: temperature over Ethernet (TCP) or serial.

TODO: not implemented yet.
"""

from instruments.common.base import Instrument


class E1RH(Instrument):
    name = "fluke_e1rh"

    def __init__(self, **cfg):
        self.cfg = cfg

    def connect(self):
        raise NotImplementedError("E1RH driver not written yet")

    def read(self) -> dict:
        raise NotImplementedError
