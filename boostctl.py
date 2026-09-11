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


def base_freq():
    """Best available 'base' clock: base_frequency, else the CPPC nominal."""
    for d in cpu_dirs():
        base = read_int(os.path.join(d, "base_frequency"))
        if base is not None:
            return base
    for entry in sorted(glob.glob(os.path.join(CPU_ROOT, "cpu[0-9]*", "acpi_cppc", "nominal_freq"))):
        nominal = read_int(entry)
        if nominal is not None:
            return nominal
    return None


def cpu_info():
    """Model name, physical cores per socket, and logical thread count from
    /proc/cpuinfo — vendor-agnostic, so the widget can label itself on
    basically any x86 (or ARM) machine without a lookup table."""
    model = "Unknown CPU"
    cores = None
    threads = 0
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            for line in fh:
                key, _, value = line.partition(":")
                k = key.strip().lower()
                v = value.strip()
                if k == "model name" and model == "Unknown CPU":
                    model = v or model
                elif k == "cpu cores" and cores is None:
                    try:
                        cores = int(v)
                    except ValueError:
                        pass
                elif k == "processor":
                    threads += 1
    except OSError:
        pass
    return {"model": model, "cores": cores, "threads": threads}


def get_state():
    caps, turbos = [], []
    for d in cpu_dirs():
        cap = read_int(os.path.join(d, "scaling_max_freq"))
        turbo = read_int(os.path.join(d, "cpuinfo_max_freq"))
        if cap is not None:
            caps.append(cap)
        if turbo is not None:
            turbos.append(turbo)
    base = base_freq()
    info = cpu_info()
    return {
        "max": round(min(caps) / 1e6, 1) if caps else None,
        "turbo": round(max(turbos) / 1e6, 1) if turbos else None,
        "base": round(base / 1e6, 1) if base else FALLBACK_BASE_GHZ,
        "temp": package_temp(),
        "model": info["model"],
        "cores": info["cores"],
        "threads": info["threads"],
        "default": persisted(),
    }


def package_temp(hwmon_root="/sys/class/hwmon"):
    """Package temperature straight from hwmon (no `sensors` subprocess).

    Intel: coretemp "Package id 0". AMD: k10temp's temp1 (Tctl/Tdie).
    """
    try:
        for entry in sorted(os.listdir(hwmon_root)):
            base = os.path.join(hwmon_root, entry)
            try:
                with open(os.path.join(base, "name"), encoding="utf-8") as fh:
                    name = fh.read().strip()
            except OSError:
                continue
            if name == "coretemp":
                for f in os.listdir(base):
                    if not f.endswith("_label"):
                        continue
                    with open(os.path.join(base, f), encoding="utf-8") as fh:
                        if fh.read().strip() != "Package id 0":
                            continue
                    inp = f[: -len("_label")] + "_input"
                    with open(os.path.join(base, inp), encoding="utf-8") as fh:
                        return round(int(fh.read().strip()) / 1000.0, 1)
            elif name == "k10temp":
                with open(os.path.join(base, "temp1_input"), encoding="utf-8") as fh:
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