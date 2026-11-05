"""E1RH driver against a fake sensor on localhost TCP (no hardware):  pytest tests/test_e1rh.py

The fake follows the ASSUMED protocol (?T -> !T1234.5, burst lines 'UC T.. S.. I..').
If the real sensor turns out different, update the fake and the driver together.
"""

import math
import socket
import threading
import time

import pytest

from instruments.fluke_e1rh.e1rh import E1RH


class FakeE1RH(threading.Thread):
    def __init__(self, burst=False):
        super().__init__(daemon=True)
        self.burst = burst
        self.vals = {"T": 1234.5, "I": 35.2, "E": 0.95, "S": 1.0, "U": "C"}
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.stop = False

    def run(self):
        conn, _ = self.srv.accept()
        conn.settimeout(0.02)
        buf = b""
        while not self.stop:
            if self.burst:
                v = self.vals
                conn.sendall(f"U{v['U']} T{v['T']:06.1f} S{v['S']:.3f} I{v['I']:05.1f}\r\n".encode())
                time.sleep(0.02)
            try:
                buf += conn.recv(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            while b"\r" in buf:
                cmd, buf = buf.split(b"\r", 1)
                cmd = cmd.decode()
                if cmd.startswith("?"):
                    k = cmd[1:]
                    val = self.vals.get(k)
                    reply = f"!{k}{val}" if val is not None else "*Syntax Error"
                elif "=" in cmd:
                    k, v = cmd.split("=")
                    self.vals[k] = v if k == "U" else float(v)
                    reply = f"!{k}{v}"
                else:
                    reply = "*Syntax Error"
                conn.sendall((reply + "\r\n").encode())
        conn.close()


@pytest.fixture
def poll_dev():
    fake = FakeE1RH()
    fake.start()
    d = E1RH(mode="tcp", host="127.0.0.1", tcp_port=fake.port)
    d.connect()
    yield d, fake
    fake.stop = True
    d.close()


def test_poll(poll_dev):
    d, fake = poll_dev
    assert d.read() == {"T_e1rh_C": 1234.5}
    assert d.internal_temp() == 35.2
    assert d.probe()
    d.set_emissivity(0.9)
    assert d.emissivity() == 0.9
    assert d.unit() == "C"


def test_rejects(poll_dev):
    d, _ = poll_dev
    with pytest.raises(ValueError):
        d.command("?Z")
    with pytest.raises(ValueError):
        d.set_emissivity(2.0)


def test_burst():
    fake = FakeE1RH(burst=True)
    fake.start()
    d = E1RH(mode="tcp", host="127.0.0.1", tcp_port=fake.port, transfer="burst")
    d.connect()
    try:
        time.sleep(0.1)
        assert d.read()["T_e1rh_C"] == 1234.5
        # poll commands still work while it streams (burst lines are skipped)
        assert d.internal_temp() == 35.2
    finally:
        fake.stop = True
        d.close()


def test_parse_tokens():
    assert E1RH.parse_tokens("UC T1234.5 S1.000 I045.2") == {
        "U": "C", "T": 1234.5, "S": 1.0, "I": 45.2}
    assert math.isnan(E1RH._float("----"))
