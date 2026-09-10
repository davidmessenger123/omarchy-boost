#!/usr/bin/python3
"""boostctl — read or change the CPU max-boost cap.

Subcommands (from the Boost bar widget):
  get          Print JSON of the current board state (read-only, user).
  set <N.m>    Cap all online cores to N GHz; "max" -> cpuinfo_max_freq.
  apply        Apply the value persisted in ./maxboost (see README); used by
               cpu-cap-boost.service on boot.

`set`/`apply` write /sys/devices/system/cpu, which needs root — the widget
invokes them with `pkexec`, and a polkit rule grants this exact script
passwordless elevation (see /etc/polkit-1/rules.d/50-davidjm-boost.rules).
"""

import glob
import json
import os
import sys

CPU_ROOT = "/sys/devices/system/cpu"
PERSIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maxboost")
FALLBACK_BASE_GHZ = 2.6


def cpu_dirs():
    return sorted(glob.glob(os.path.join(CPU_ROOT, "cpu[0-9]*", "cpufreq")))


def read_int(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return int(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def cap_all(ghz):
    wanted = str(ghz).lower()
    for d in cpu_dirs():
        if wanted == "max":
            target = read_int(os.path.join(d, "cpuinfo_max_freq"))
        else:
            target = int(round(float(wanted) * 1_000_000))
        if target is None:
            continue
        with open(os.path.join(d, "scaling_max_freq"), "w", encoding="utf-8") as fh:
            fh.write(f"{target}\n")


def persisted():
    try:
        with open(PERSIST_PATH, encoding="utf-8") as fh:
            value = fh.read().strip()
    except OSError:
        value = ""
    if value.lower() == "max":
        return value
    try:
        float(value)
        return value
    except ValueError:
        return "3.0"


def get_state():
    caps, turbos, bases = [], [], []
    for d in cpu_dirs():
        cap = read_int(os.path.join(d, "scaling_max_freq"))
        turbo = read_int(os.path.join(d, "cpuinfo_max_freq"))
        base = read_int(os.path.join(d, "base_frequency"))
        if cap is not None:
            caps.append(cap)
        if turbo is not None:
            turbos.append(turbo)
        if base is not None:
            bases.append(base)
    return {
        "max": round(min(caps) / 1e6, 1) if caps else None,
        "turbo": round(max(turbos) / 1e6, 1) if turbos else None,
        "base": round(bases[0] / 1e6, 1) if bases else FALLBACK_BASE_GHZ,
        "temp": package_temp(),
        "default": persisted(),
    }


def package_temp():
    """Package temperature straight from hwmon (no `sensors` subprocess)."""
    hwmon = "/sys/class/hwmon"
    try:
        for entry in sorted(os.listdir(hwmon)):
            with open(os.path.join(hwmon, entry, "name"), encoding="utf-8") as fh:
                if fh.read().strip() != "coretemp":
                    continue
            for f in os.listdir(os.path.join(hwmon, entry)):
                if not f.endswith("_label"):
                    continue
                with open(os.path.join(hwmon, entry, f), encoding="utf-8") as fh:
                    if fh.read().strip() != "Package id 0":
                        continue
                inp = os.path.join(hwmon, entry, f[: -len("_label")] + "_input")
                with open(inp, encoding="utf-8") as fh:
                    return round(int(fh.read().strip()) / 1000.0, 1)
    except Exception:
        pass
    return None


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "get":
        print(json.dumps(get_state()))
    elif cmd == "set":
        ghz = sys.argv[2] if len(sys.argv) > 2 else persisted()
        cap_all(ghz)
    elif cmd == "apply":
        cap_all(persisted())
    else:
        print("usage: boostctl.py get | set <GHz|max> | apply", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()