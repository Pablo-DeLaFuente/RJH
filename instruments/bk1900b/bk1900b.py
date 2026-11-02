"""BK Precision 1900B DC supply: set/read voltage and current over USB virtual COM.

TODO: not implemented yet.
"""

from instruments.common.base import Instrument


class BK1900B(Instrument):
    name = "bk1900b"

    def __init__(self, **cfg):
        self.cfg = cfg

    def connect(self):
        raise NotImplementedError("BK1900B driver not written yet")

    def read(self) -> dict:
        raise NotImplementedError
