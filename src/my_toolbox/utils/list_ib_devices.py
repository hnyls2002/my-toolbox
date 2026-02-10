#!/usr/bin/env python3
"""List InfiniBand devices and their link state."""

from pathlib import Path

IB_SYSFS = Path("/sys/class/infiniband")


def list_ib_devices():
    if not IB_SYSFS.exists():
        print("No InfiniBand sysfs found.")
        return

    for dev in sorted(IB_SYSFS.iterdir()):
        state_file = dev / "ports" / "1" / "state"
        try:
            state = state_file.read_text().strip()
        except OSError:
            state = "unknown"

        if state == "4: ACTIVE":
            print(f"{dev.name} is ACTIVE")
        else:
            print(f"{dev.name} is NOT active (state={state})")


def main():
    list_ib_devices()


if __name__ == "__main__":
    main()
