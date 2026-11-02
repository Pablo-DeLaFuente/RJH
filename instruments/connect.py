"""Find each instrument on this computer and save its port.

    python -m instruments.connect             # detect, print table, save
    python -m instruments.connect --list      # list every serial port only
    python -m instruments.connect --no-probe  # match by USB name only, send nothing

How each instrument is found, in order:
  1. USB serial number remembered from last time (survives COM number changes)
  2. Port name / USB ID from the `match:` section of instruments.yaml
  3. Probing: send the driver's identify query to each unclaimed port

Results go to instruments/instruments.local.yaml (one per computer, not in git).
The manager runs this automatically, so you rarely need to call it yourself.
"""

import argparse
import socket
from datetime import datetime
from pathlib import Path

import serial.tools.list_ports
import yaml

HERE = Path(__file__).parent
BASE_CONFIG = HERE / "instruments.yaml"
LOCAL_CONFIG = HERE / "instruments.local.yaml"

# Ports that hang or misbehave when opened blindly
SKIP_WORDS = ("bluetooth",)


def _registry():
    from instruments.manager import REGISTRY  # late import avoids a cycle
    return REGISTRY


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def list_ports():
    return sorted(serial.tools.list_ports.comports(), key=lambda p: p.device)


def _vid_pid(p) -> str:
    return f"{p.vid:04x}:{p.pid:04x}" if p.vid is not None else ""


def _matches(p, match: dict) -> bool:
    desc = f"{p.description} {p.manufacturer or ''} {p.product or ''}".lower()
    if any(s.lower() in desc for s in match.get("description", []) or []):
        return True
    ids = [s.lower() for s in match.get("vid_pid", []) or []]
    return bool(ids) and _vid_pid(p) in ids


def _tcp_ok(host, port, timeout=1.0) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def detect(base=None, local=None, probe=True, only_enabled=True, verbose=True) -> dict:
    """Return {name: {port, serial_number, description, method}} for found instruments."""
    base = base if base is not None else load_yaml(BASE_CONFIG)
    local = local if local is not None else load_yaml(LOCAL_CONFIG)
    registry = _registry()
    ports = [p for p in list_ports()
             if not any(w in p.description.lower() for w in SKIP_WORDS)]
    claimed, found = set(), {}

    def take(name, p, method):
        claimed.add(p.device)
        found[name] = {"port": p.device, "serial_number": p.serial_number or "",
                       "description": p.description, "method": method}

    names = [n for n, s in base.items()
             if n in registry and isinstance(s, dict) and (s.get("enabled") or not only_enabled)]
    serial_names = []
    for name in names:
        cfg = base[name]
        if name == "fluke_e1rh" and cfg.get("mode") == "tcp":
            ok = _tcp_ok(cfg["host"], cfg["tcp_port"])
            found[name] = {"port": f'{cfg["host"]}:{cfg["tcp_port"]}',
                           "method": "network ok" if ok else "network UNREACHABLE"}
        elif "ble_address" in cfg:
            found[name] = {"port": cfg.get("ble_address") or "-",
                           "method": "bluetooth (not scanned yet)"}
        else:
            serial_names.append(name)

    # 1. remembered USB serial number
    for name in serial_names:
        sn = (local.get(name) or {}).get("serial_number")
        if sn:
            for p in ports:
                if p.serial_number == sn and p.device not in claimed:
                    take(name, p, "serial number"); break

    # 2. port name / USB ID
    for name in serial_names:
        if name in found:
            continue
        hits = [p for p in ports if p.device not in claimed
                and _matches(p, base[name].get("match", {}) or {})]
        if len(hits) == 1:
            take(name, hits[0], "name match")
        elif len(hits) > 1 and probe and registry[name].can_probe():
            for p in hits:  # several identical adapters: ask each one
                if registry[name].detect(p.device, base[name]):
                    take(name, p, "name match + probe"); break

    # 3. probe everything still unclaimed
    if probe:
        for name in serial_names:
            if name in found or not registry[name].can_probe():
                continue
            for p in ports:
                if p.device in claimed:
                    continue
                if verbose:
                    print(f"  probing {p.device} for {name}...")
                if registry[name].detect(p.device, base[name]):
                    take(name, p, "probe"); break

    for name in serial_names:
        found.setdefault(name, {"port": None, "method": "NOT FOUND"})
    return found


def save_local(found: dict, path: Path = None):
    path = path or LOCAL_CONFIG
    local = load_yaml(path)
    for name, info in found.items():
        if info.get("port") and "serial_number" in info:  # serial instruments only
            local[name] = {k: info[k] for k in ("port", "serial_number", "description")}
    header = (f"# Auto-generated by instruments.connect on {socket.gethostname()} "
              f"at {datetime.now():%Y-%m-%d %H:%M}.\n"
              "# Ports for THIS computer only. Safe to delete; it is rebuilt.\n")
    with open(path, "w") as f:
        f.write(header)
        yaml.safe_dump(local, f, sort_keys=False)


def print_table(found: dict):
    print(f"\n{'instrument':14s} {'port':22s} how found")
    print("-" * 60)
    for name, info in found.items():
        print(f"{name:14s} {str(info.get('port') or '-'):22s} {info['method']}")
    print()


def print_ports():
    for p in list_ports():
        print(f"{p.device:8s} {p.description}  [USB {_vid_pid(p) or '-'}"
              f"  SN {p.serial_number or '-'}]")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Detect instruments and save their ports")
    ap.add_argument("--list", action="store_true", help="list serial ports and exit")
    ap.add_argument("--no-probe", action="store_true", help="don't send queries to ports")
    ap.add_argument("--all", action="store_true", help="include disabled instruments")
    a = ap.parse_args()

    print("Serial ports on this computer:")
    print_ports()
    if a.list:
        raise SystemExit
    found = detect(probe=not a.no_probe, only_enabled=not a.all)
    print_table(found)
    save_local(found)
    print(f"Saved to {LOCAL_CONFIG.name}")
