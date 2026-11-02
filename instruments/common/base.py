"""Shared base class every instrument driver inherits from."""

import inspect


class Instrument:
    """Common interface: connect(), read() -> dict, close().

    read() returns a flat dict of named values with units in the key,
    e.g. {"T_ct3_C": 523.4} or {"V_bk_V": 4.2, "I_bk_A": 30.1}, so the
    manager can merge every instrument into one CSV row.

    Drivers that define probe() -> bool (a quick "is this really me?" query)
    can be auto-detected on unknown ports by instruments.connect.
    """

    name = "instrument"

    def connect(self):
        raise NotImplementedError

    def read(self) -> dict:
        raise NotImplementedError

    def close(self):
        pass

    @classmethod
    def from_config(cls, cfg: dict):
        """Build from the instrument's section of instruments.yaml.

        Only keys the driver's __init__ accepts are passed through, so the
        yaml can also hold things like enabled/match/serial_number.
        """
        params = inspect.signature(cls.__init__).parameters
        if any(p.kind is p.VAR_KEYWORD for p in params.values()):
            return cls(**cfg)
        return cls(**{k: v for k, v in cfg.items() if k in params})

    @classmethod
    def can_probe(cls) -> bool:
        return callable(getattr(cls, "probe", None))

    @classmethod
    def detect(cls, port: str, cfg: dict) -> bool:
        """Open `port` and ask the instrument to identify itself."""
        if not cls.can_probe():
            return False
        inst = cls.from_config({**cfg, "port": port})
        try:
            inst.connect()
            return bool(inst.probe())
        except Exception:
            return False
        finally:
            try:
                inst.close()
            except Exception:
                pass

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()
