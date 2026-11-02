"""
One-time check: find the 374 FC, list its GATT services/characteristics,
then dump raw notifications for 15 s.

    python -m instruments.fluke_374fc.discover              # scan 40 s
    python -m instruments.fluke_374fc.discover --probe      # also connect to the strongest
                                                            #   unnamed devices and look for
                                                            #   the Fluke service (meter ~30 cm from PC)
    python -m instruments.fluke_374fc.discover AA:BB:...    # skip scan, use this address

Tips: radio icon must be on the meter's display; turn Bluetooth OFF on your
phone (a connected meter stops advertising); don't pair it in Windows.
"""
import argparse
import asyncio
import time

from bleak import BleakClient, BleakScanner

FLUKE_PREFIX = "b698"          # all Fluke Connect UUIDs start b698xxxx-7562-11e2-...
FLUKE_SERVICE = "b6981800-7562-11e2-b50d-00163e46f8fe"


def is_fluke_adv(dev, adv) -> bool:
    name = (adv.local_name or dev.name or "").lower()
    return ("fc" in name and any(c.isdigit() for c in name)) or "fluke" in name \
        or any(u.lower().startswith(FLUKE_PREFIX) for u in adv.service_uuids)


async def scan(seconds: float):
    print(f"Scanning {seconds:.0f} s (meter on, radio icon showing, phone BT off)...")
    devs = await BleakScanner.discover(timeout=seconds, return_adv=True)
    rows = sorted(devs.values(), key=lambda da: da[1].rssi, reverse=True)
    print(f"{len(rows)} devices; strongest first (bring the meter close -> it should be near the top):")
    for d, adv in rows[:25]:
        mfr = {f"0x{k:04x}": v.hex() for k, v in adv.manufacturer_data.items()}
        flag = "  <-- FLUKE?" if is_fluke_adv(d, adv) else ""
        print(f"  {d.address}  rssi={adv.rssi:>4}  name={adv.local_name or d.name!r}"
              f"  uuids={adv.service_uuids}  mfr={mfr}{flag}")
    return rows


async def has_fluke_service(addr: str) -> bool:
    try:
        async with BleakClient(addr, timeout=8.0) as c:
            return any(s.uuid.lower().startswith(FLUKE_PREFIX) for s in c.services)
    except Exception:  # noqa: BLE001
        return False


async def dump(addr: str):
    print(f"\nConnecting to {addr} ...")
    async with BleakClient(addr, timeout=20.0) as c:
        notifiable = []
        for s in c.services:
            print(f"[service] {s.uuid}  {s.description}")
            for ch in s.characteristics:
                val = ""
                if "read" in ch.properties:
                    try:
                        raw = await c.read_gatt_char(ch)
                        val = f"{raw.hex(' ')}  |{raw.decode('ascii', 'replace')}|"
                    except Exception as e:  # noqa: BLE001
                        val = f"<{e}>"
                print(f"   {ch.uuid}  {','.join(ch.properties):<28} {val}")
                if "notify" in ch.properties:
                    notifiable.append(ch)
        t0 = time.perf_counter()

        def mk(u):
            def cb(_h, d):
                d = bytes(d)
                print(f"{time.perf_counter()-t0:7.3f}  {u[4:8]}  {d.hex(' ')}  |{d.decode('ascii', 'replace')}|")
            return cb
        for ch in notifiable:
            try:
                await c.start_notify(ch, mk(ch.uuid))
            except Exception as e:  # noqa: BLE001
                print(f"   (notify {ch.uuid} failed: {e})")
        print("\nRaw notifications for 15 s (change the reading to see it move):")
        await asyncio.sleep(15)


async def main(a):
    addr = a.address
    if not addr:
        rows = await scan(a.seconds)
        hits = [d for d, adv in rows if is_fluke_adv(d, adv)]
        if hits:
            addr = hits[0].address
        elif a.probe:
            cands = [d for d, adv in rows
                     if not (adv.local_name or d.name) and adv.rssi >= a.min_rssi][:a.max_probe]
            print(f"\nNo Fluke advert seen; probing {len(cands)} unnamed devices with rssi >= {a.min_rssi}...")
            for d in cands:
                print(f"  {d.address} ...", end=" ", flush=True)
                ok = await has_fluke_service(d.address)
                print("FLUKE SERVICE FOUND" if ok else "no")
                if ok:
                    addr = d.address
                    break
        if not addr:
            print("\nNo Fluke FC device found. Check the radio icon on the meter, phone BT off,"
                  "\nthen retry with the meter next to the PC and --probe.")
            return
    print(f"\n>>> Meter address: {addr}   (put this in instruments.yaml -> fluke_374fc.ble_address)")
    await dump(addr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("address", nargs="?", default=None)
    ap.add_argument("--seconds", type=float, default=40.0)
    ap.add_argument("--probe", action="store_true", help="connect to strong unnamed devices to check")
    ap.add_argument("--min-rssi", type=int, default=-60)
    ap.add_argument("--max-probe", type=int, default=8)
    asyncio.run(main(ap.parse_args()))
