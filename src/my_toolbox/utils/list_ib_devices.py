#!/usr/bin/env python3
"""List InfiniBand devices and their link state."""

from pathlib import Path
from typing import List

IB_SYSFS = Path("/sys/class/infiniband")


def _read_port_state(port_path: Path) -> str:
    try:
        return (port_path / "state").read_text().strip()
    except OSError:
        return "unknown"


def _read_port_rate(port_path: Path) -> int:
    """Return link rate in Gbps, or -1 on failure."""
    try:
        return int((port_path / "rate").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return -1


def get_active_ib_devices() -> List[str]:
    """Return names of active, high-speed, non-Ethernet IB devices."""
    if not IB_SYSFS.exists():
        return []

    devices = []
    for dev in sorted(IB_SYSFS.iterdir()):
        if "eth" in dev.name.lower():
            continue

        port_path = dev / "ports" / "1"
        if not port_path.is_dir():
            continue

        state = _read_port_state(port_path)
        if "ACTIVE" not in state.upper():
            continue

        # Filter low-speed devices (< 100 Gbps)
        rate = _read_port_rate(port_path)
        if 0 <= rate < 100:
            continue

        devices.append(dev.name)

    return devices


def list_ib_devices():
    """Print IB device status (CLI entry point)."""
    if not IB_SYSFS.exists():
        print("No InfiniBand sysfs found.")
        return

    for dev in sorted(IB_SYSFS.iterdir()):
        port_path = dev / "ports" / "1"
        state = _read_port_state(port_path)

        if state == "4: ACTIVE":
            print(f"{dev.name} is ACTIVE")
        else:
            print(f"{dev.name} is NOT active (state={state})")


def main():
    list_ib_devices()


if __name__ == "__main__":
    main()
