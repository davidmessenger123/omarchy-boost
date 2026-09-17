#!/usr/bin/python3
"""boostctl — read or change the CPU max-boost cap.

Subcommands (from the Boost bar widget):
  get          Print JSON of the current board state (read-only, user).
  set <N.m>    Cap all online cores to N GHz; "max" -> cpuinfo_max_freq.
  apply        Apply the value persisted in ./maxboost (see README); used by
               cpu-cap-boost.service on boot.
  monitor      Write the board state as JSON to a file every 2 s (option
               --state-file); the bar widget watches that file instead of
               spawning `get` twice a second. Never exits.

Options:
  --persist-dir DIR   Where maxboost lives (default: next to this script).
                      The root-owned copy under /usr/libexec uses the user's
                      plugin dir so the boot service reads the same cap the
                      widget persisted.

`set`/`apply` write /sys/devices/system/cpu, which needs root — the widget
invokes them with `pkexec`, and a polkit rule grants this exact script
passwordless elevation (see /etc/polkit-1/rules.d/50-davidjm-boost.rules).
"""

import argparse
import glob
import json
import os
import sys
import time

CPU_ROOT = "/sys/devices/system/cpu"
PERSIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maxboost")


def set_persist_dir(plugin_dir):
    """Point maxboost at the real user-writable plugin dir (the root-owned
    /usr/libexec copy can't write its own)."""
    global PERSIST_PATH
    if plugin_dir:
        PERSIST_PATH = os.path.join(plugin_dir, "maxboost")
    return PERSIST_PATH


def cpu_dirs():
    return sorted(glob.glob(os.path.join(CPU_ROOT, "cpu[0-9]*", "cpufreq")))


def read_int(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return int(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def cap_all(ghz):
    """Cap every online core, never above that core's own reported maximum
    (cpuinfo_max_freq). Cores can have individual turbo ceilings, so each one
    is clamped separately — this is the hard backstop against exceeding what
    the CPU reports, and it applies to `set`, `apply`, and the boot service."""
    wanted = str(ghz).lower()
    try:
        wanted_hz = None if wanted == "max" else int(round(float(wanted) * 1_000_000))
    except ValueError as exc:
        raise ValueError(f"invalid boost cap: {ghz!r}") from exc
    for d in cpu_dirs():
        if wanted_hz is None:
            target = read_int(os.path.join(d, "cpuinfo_max_freq"))
        else:
            target = wanted_hz
        if target is None:
            continue
        ceiling = read_int(os.path.join(d, "cpuinfo_max_freq"))
        if ceiling is not None:
            target = min(target, ceiling)
        if target <= 0:
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
    # Hybrid P+E CPUs: efficiency cores report a lower native turbo, so every
    # core capped at the user's value would make min(caps) read back the small
    # E-core ceiling. The cap that matters is what the top (P) cores hold.
    max_cap = max(caps) if caps else None
    return {
        "max": round(max_cap / 1e6, 1) if max_cap else None,
        "turbo": round(max(turbos) / 1e6, 1) if turbos else None,
        "base": round(base / 1e6, 1) if base else None,
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


def write_state(state_file, state):
    """Write the state JSON in place. The bar's FileView watches via
    QFileSystemWatcher, which binds to the file's inode; a tmp+rename swap
    would orphan the watcher on the first rewrite and freeze the readout.
    The write takes microseconds, so a reader that catches a partial file
    simply parses the next tick."""
    with open(state_file, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
        fh.flush()
        os.fsync(fh.fileno())


def monitor(state_file, interval=2.0):
    """Long-lived state pump for the bar widget: one process instead of a `get`
    spawn every tick. First write lands immediately so the readout isn't blank
    at login; transient sysfs hiccups keep the loop alive."""
    while True:
        try:
            write_state(state_file, get_state())
        except Exception:
            pass
        time.sleep(interval)


def main(argv=None):
    args = argv if argv is not None else sys.argv[1:]
    ap = argparse.ArgumentParser(prog="boostctl.py", description=__doc__)
    ap.add_argument("--persist-dir", default=None, help="dir holding maxboost")
    ap.add_argument("--state-file", default=None, help="monitor JSON output path")
    ap.add_argument("command", nargs="?", choices=["get", "set", "apply", "monitor"])
    ap.add_argument("value", nargs="?")
    parsed = ap.parse_args(args)

    set_persist_dir(parsed.persist_dir)

    if parsed.command == "get":
        print(json.dumps(get_state()))
    elif parsed.command == "set":
        ghz = parsed.value if parsed.value is not None else persisted()
        try:
            if str(ghz).lower() != "max":
                float(ghz)
        except ValueError:
            print(f"invalid boost cap: {ghz!r}", file=sys.stderr)
            sys.exit(2)
        try:
            cap_all(ghz)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(2)
    elif parsed.command == "apply":
        cap_all(persisted())
    elif parsed.command == "monitor":
        if not parsed.state_file:
            print("monitor needs --state-file", file=sys.stderr)
            sys.exit(2)
        monitor(parsed.state_file)
    else:
        ap.print_usage(sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()