"""BK1900B driver against a fake supply (no hardware needed):  pytest tests/test_bk1900b.py"""

import pytest

from instruments.bk1900b import bk1900b as bk


class FakeSupply:
    """Mimics the 1900B's ASCII replies over serial."""

    def __init__(self, *a, **k):
        self.Vset, self.Iset, self.on = 5.0, 10.0, False
        self.out = b""
        self.sent = []
        self.is_open = True

    def reset_input_buffer(self):
        self.out = b""

    def write(self, data):
        cmd = data.decode().strip()
        self.sent.append(cmd)
        if cmd == "GETD":
            V, I = (self.Vset, 2.5) if self.on else (0.0, 0.0)
            reply = f"{round(V * 100):04d}{round(I * 100):04d}0"
        elif cmd == "GETS":
            reply = f"{round(self.Vset * 10):03d}{round(self.Iset * 10):03d}"
        elif cmd == "GMAX":
            reply = "160600"
        elif cmd.startswith("VOLT"):
            self.Vset, reply = int(cmd[4:]) / 10, None
        elif cmd.startswith("CURR"):
            self.Iset, reply = int(cmd[4:]) / 10, None
        elif cmd.startswith("SOUT"):
            self.on, reply = cmd[4] == "0", None
        else:
            reply = None
        self.out = (f"{reply}\r" if reply else "").encode() + b"OK\r"

    def read_until(self, term=b"\r"):
        i = self.out.find(term)
        if i < 0:
            chunk, self.out = self.out, b""
        else:
            chunk, self.out = self.out[: i + 1], self.out[i + 1:]
        return chunk

    def close(self):
        self.is_open = False


@pytest.fixture
def dev(monkeypatch):
    monkeypatch.setattr(bk.serial, "Serial", FakeSupply)
    d = bk.BK1900B("COMX")
    d.connect()
    yield d
    d.close()


def test_max_and_probe(dev):
    assert (dev.V_max, dev.I_max) == (16.0, 60.0)
    assert dev.probe()


def test_set_and_read(dev):
    dev.set_current(30.5)
    dev.set_voltage(12.5)
    assert dev.ser.sent[-2:] == ["CURR305", "VOLT125"]
    assert dev.setpoints() == (12.5, 30.5)
    dev.output_on()
    assert dev.ser.sent[-1] == "SOUT0"
    r = dev.read()
    assert r["V_bk_V"] == 12.5 and r["I_bk_A"] == 2.5 and r["CC_bk"] == 0
    assert r["P_bk_W"] == pytest.approx(31.25)
    dev.output_off()
    assert dev.ser.sent[-1] == "SOUT1"


def test_limits(dev):
    with pytest.raises(ValueError):
        dev.set_current(61)
    with pytest.raises(ValueError):
        dev.set_voltage(-1)
