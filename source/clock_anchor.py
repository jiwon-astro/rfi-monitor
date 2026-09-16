"""User-level UTC anchor for an offline Pi. Does NOT change OS/RTC time."""
import argparse
import json
import os
from pathlib import Path
import time

HERE = Path(__file__).resolve().parent


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def atomic_json(path, data):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


class Clock:
    def __init__(self, path=None):
        self.anchor = json.loads(Path(path or HERE / "clock_anchor.json").read_text())
        if self.anchor["boot_id"] != boot_id():
            raise ValueError("Pi rebooted since clock sync. Run sync-clock.ps1 again.")
        age = (time.monotonic_ns() - self.anchor["monotonic_ns"]) / 1e9
        if age < 0 or age > 7 * 86400:
            raise ValueError("Clock anchor expired. Run sync-clock.ps1 again.")
        if self.anchor["uncertainty_ns"] > 5_000_000_000:
            raise ValueError("Clock sync round trip too slow; repeat sync-clock.ps1.")

    def now_ns(self):
        return self.anchor["utc_ns"] + time.monotonic_ns() - self.anchor["monotonic_ns"]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--utc-ns", type=int)
    p.add_argument("--monotonic-ns", type=int)
    p.add_argument("--boot-id")
    p.add_argument("--uncertainty-ns", type=int)
    a = p.parse_args()
    if a.utc_ns is None:
        print(json.dumps({"monotonic_ns": time.monotonic_ns(), "boot_id": boot_id(),
                          "system_utc_ns": time.time_ns()}))
        return
    if a.boot_id != boot_id() or a.monotonic_ns is None or a.uncertainty_ns is None:
        p.error("Invalid/missing clock anchor parameters")
    if not 0 <= a.uncertainty_ns <= 5_000_000_000:
        p.error("Clock uncertainty must be between 0 and 5 seconds")
    record = {"utc_ns": a.utc_ns, "monotonic_ns": a.monotonic_ns,
              "boot_id": a.boot_id, "uncertainty_ns": a.uncertainty_ns,
              "source": "Windows host UTC midpoint of SSH round trip",
              "note": "RTT bound assumes stable host clock; oscillator drift is not calibrated. OS/RTC unchanged."}
    atomic_json(HERE / "clock_anchor.json", record)
    print(json.dumps({"anchor_saved": record, "corrected_utc_ns": Clock().now_ns()}))


if __name__ == "__main__":
    main()
