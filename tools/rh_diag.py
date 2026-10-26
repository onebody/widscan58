#!/usr/bin/env python3
"""One-shot diagnostic for a RotorHazard node link. GPL-3.0-or-later.

Answers "does the board speak RotorHazard's node protocol on this port?" the
way RotorHazard 4.x does it, without starting a server: opens the port with
DTR/RTS cleared (as the server does, so a native-USB ESP32 is not reset), waits
the server's boot delay, then sends the discovery reads and checks the replies
byte for byte.

    python3 tools/rh_diag.py                 # environment + candidate ports
    python3 tools/rh_diag.py --all           # probe every candidate port
    python3 tools/rh_diag.py -p /dev/ttyACM0 # probe one
    python3 tools/rh_diag.py -p /dev/ttyAMA0 --baud 115200

Only pyserial is needed. Paste the whole output when reporting a problem: the
host section says whether the port can be opened at all (dialout, console on
the Pi's UART, Bluetooth owning ttyAMA0, ...) and the probe section says which
discovery byte fails.
"""
import argparse
import glob
import os
import platform
import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is missing: python3 -m pip install pyserial")

# Reads RotorHazard's discover() makes, in order, as (command, payload size, name).
READS = [
    (0x22, 2, "READ_REVISION_CODE"),
    (0x39, 1, "READ_MULTINODE_COUNT"),
    (0x3C, 1, "READ_NODE_SLOTIDX"),
    (0x03, 2, "READ_FREQUENCY"),
    (0x11, 2, "READ_RHFEAT_FLAGS"),
    (0x3D, 16, "READ_FW_VERSION"),
]
CANDIDATES = ["/dev/ttyACM*", "/dev/ttyUSB*", "/dev/serial*", "/dev/ttyAMA*", "/dev/ttyS*", "/dev/cu.usbmodem*"]


def environment():
    print("== host ==")
    print("host:", platform.platform())
    print("python:", sys.version.split()[0], "pyserial:", serial.__version__)
    try:
        with open("/proc/cmdline") as f:
            cmdline = f.read().strip()
        print("cmdline:", cmdline)
        for c in ("console=serial0", "console=ttyAMA0", "console=ttyS0"):
            if c in cmdline:
                print(f"  ! {c} is still set: the kernel console shares the port RotorHazard needs")
    except OSError:
        pass
    if hasattr(os, "getgroups"):
        names = []
        try:
            import grp
            names = [grp.getgrgid(g).gr_name for g in os.getgroups()]
        except (ImportError, KeyError):
            pass
        print("user:", os.environ.get("USER") or os.getuid(), "groups:", " ".join(names) or "(none)")
        if sys.platform.startswith("linux") and names and "dialout" not in names and "uucp" not in names:
            print("  ! not in dialout/uucp: opening a serial port will fail with EACCES")
    for unit in ("serial-getty@ttyAMA0", "serial-getty@ttyS0", "hciuart", "bluetooth"):
        try:
            state = os.popen(f"systemctl is-active {unit} 2>/dev/null").read().strip()
        except OSError:
            state = ""
        if state:
            print(f"systemd {unit}: {state}")


def ports():
    """Every port name worth trying, with a description."""
    found = []
    for p in list_ports.comports():
        found.append((p.device, p.description))
    seen = {d for d, _ in found}
    for pattern in CANDIDATES:
        for path in sorted(glob.glob(pattern)):
            if path not in seen:
                found.append((path, "not enumerated by pyserial"))
                seen.add(path)
    for path in sorted(glob.glob("/dev/serial/by-id/*")):
        try:
            target = os.path.realpath(path)
        except OSError:
            continue
        print(f"  by-id {path} -> {target}")
    return found


def open_like_rh(port, baud):
    """RotorHazard's open: clear DTR/RTS before opening, then a 2 s settle."""
    s = serial.Serial(port=None, baudrate=baud, timeout=0.25)
    s.setDTR(0)
    s.setRTS(0)
    s.setPort(port)
    s.open()
    return s


def probe(port, baud):
    print(f"\n== probe {port} @ {baud} 8N1 ==")
    try:
        s = open_like_rh(port, baud)
    except Exception as e:
        print(f"  OPEN FAILED: {type(e).__name__}: {e}")
        return False
    ok = True
    try:
        time.sleep(2.0)  # the server's bootloader chill time
        s.reset_input_buffer()
        s.write(b"INFO\n")
        time.sleep(0.2)
        info = s.read(256)
        print("  INFO ->", info.decode(errors="replace").strip() or "(no reply)")
        if b"ERR " in info:
            print("  ! the board refused INFO; it is not running a widscan58 firmware, or the port is busy")

        for cmd, size, name in READS:
            s.reset_input_buffer()
            t0 = time.monotonic()
            s.write(bytes([cmd]))
            data = s.read(size + 1)
            ms = (time.monotonic() - t0) * 1000
            want = sum(data[:-1]) & 0xFF if data else None
            good = len(data) == size + 1 and want == data[-1]
            shown = " ".join(f"{b:02x}" for b in data) or "(nothing)"
            print(f"  0x{cmd:02x} {name:<22} {shown:<52} {ms:6.1f} ms  {'ok' if good else 'FAIL'}")
            if not good:
                ok = False
                if not data:
                    print("     no bytes: wrong port/wiring/baud, nothing listening, or the board is not running")
                else:
                    print("     bad length/checksum: bytes from something else on the line (console, banner, other device)")
    except Exception as e:
        ok = False
        print(f"  IO FAILED: {type(e).__name__}: {e}")
    finally:
        s.close()

    if port.startswith(("/dev/ttyAMA", "/dev/ttyS", "/dev/serial")):
        print("  note: on the Pi this must be the PL011 with dtoverlay=disable-bt and no serial console:")
        print("        sudo raspi-config > Interface Options > Serial Port: login shell No, hardware Yes")
        print("        /boot/firmware/config.txt: enable_uart=1, dtoverlay=disable-bt; no console=serial0 in cmdline.txt")
        print("        wiring: Pi pin 10 (GPIO15 RX) <- board GP11 TX, Pi pin 8 (GPIO14 TX) -> board GP12 RX, GND common")
    print("  verdict:", "board answers RotorHazard discovery on this port" if ok else "DISCOVERY FAILS on this port")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", help="port to probe")
    ap.add_argument("--baud", type=int, default=115200, help="baud rate (default 115200, what Widscan58 uses)")
    ap.add_argument("--all", action="store_true", help="probe every candidate port")
    ap.add_argument("--list", action="store_true", help="only list the environment and ports")
    a = ap.parse_args()

    environment()
    print("\n== candidate ports ==")
    found = ports()
    for dev, desc in found:
        print(f"  {dev:<28} {desc}")
    if not found:
        print("  (none: no serial device is attached, or the kernel has no driver for it)")

    if a.list:
        return
    targets = [a.port] if a.port else ([dev for dev, _ in found] if a.all else [])
    if not targets:
        print("\n(no port given: pass -p <port>, or --all to probe the candidates above)")
        return
    good = [p for p in targets if probe(p, a.baud)]
    print("\nsummary:", f"{len(good)} port(s) work" if good else "nothing answered RotorHazard discovery")
    if not good:
        sys.exit(1)


if __name__ == "__main__":
    main()
