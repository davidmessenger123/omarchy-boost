#!/usr/bin/python3
"""Read-only CPU boost state for the Boost bar widget."""

import argparse
import glob
import json
import math
import os
import stat
import sys
import time

CPU_ROOT = "/sys/devices/system/cpu"
MAX_FREQ_KHZ = 10_000_000
MIN_SENSOR = -50000
MAX_SENSOR = 200000


def _cpu_number(directory):
    name = os.path.basename(os.path.dirname(directory))
    if not name.startswith("cpu"):
        return None
    try:
        return int(name[3:])
    except ValueError:
        return None


def _read_int(path, lower=0, upper=MAX_FREQ_KHZ):
    try:
        with open(path, encoding="utf-8") as fh:
            value = int(fh.read(128).strip().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    if value < lower or value > upper:
        return None
    return value


def _is_online(directory):
    path = os.path.join(os.path.dirname(directory), "online")
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read(64).strip() == "1"
    except FileNotFoundError:
        return True
    except OSError:
        return False


def cpu_dirs(online_only=False):
    paths = glob.glob(os.path.join(CPU_ROOT, "cpu[0-9]*", "cpufreq"))
    paths.sort(key=lambda path: _cpu_number(path) if _cpu_number(path) is not None else -1)
    return [path for path in paths if os.path.isdir(path) and (not online_only or _is_online(path))]


def _read_text(path, limit=4096):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read(limit + 1)
    except OSError:
        return ""


def _sensor_value(base, stem):
    value = _read_int(os.path.join(base, stem + "_input"), MIN_SENSOR, MAX_SENSOR)
    return None if value is None else value / 1000.0


def _package_sensor(hwmon_root="/sys/class/hwmon"):
    try:
        entries = sorted(os.scandir(hwmon_root), key=lambda entry: entry.name)
    except OSError:
        return None
    values = []
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=True):
                continue
            name = _read_text(os.path.join(entry.path, "name"), 128).strip()
            if name not in ("coretemp", "k10temp", "zenpower"):
                continue
            candidates = []
            for filename in sorted(os.listdir(entry.path)):
                if not filename.endswith("_label"):
                    continue
                stem = filename[:-len("_label")]
                label = _read_text(os.path.join(entry.path, stem + "_label"), 128).strip()
                normalized = label.lower().replace(" ", "")
                if name == "coretemp" and (normalized == "packageid0" or normalized.startswith("packageid")):
                    candidates.append(stem)
                elif name in ("k10temp", "zenpower") and (normalized in ("tctl", "tdie", "package", "packageid0") or normalized.startswith("package")):
                    candidates.append(stem)
            if not candidates and name in ("k10temp", "zenpower"):
                candidates.append("temp1")
            for stem in candidates:
                value = _sensor_value(entry.path, stem)
                if value is not None and math.isfinite(value):
                    values.append(value)
        except OSError:
            continue
    return round(max(values), 1) if values else None


def package_temp(hwmon_root="/sys/class/hwmon"):
    return _package_sensor(hwmon_root)


def _cpu_times():
    try:
        with open("/proc/stat", encoding="ascii") as handle:
            line = handle.readline()
    except OSError:
        return None
    fields = line.split()
    if not fields or fields[0] != "cpu" or len(fields) < 5:
        return None
    try:
        values = [int(value) for value in fields[1:9]]
    except ValueError:
        return None
    if any(value < 0 for value in values):
        return None
    return sum(values), values[3] + (values[4] if len(values) > 4 else 0)


def utilization(previous, current):
    if previous is None or current is None:
        return None
    total_delta = current[0] - previous[0]
    idle_delta = current[1] - previous[1]
    if total_delta <= 0 or idle_delta < 0 or idle_delta > total_delta:
        return None
    return round(100.0 * (total_delta - idle_delta) / total_delta, 1)


def current_frequency():
    values = []
    for directory in cpu_dirs(online_only=True):
        value = _read_int(os.path.join(directory, "scaling_cur_freq"))
        if value is None:
            value = _read_int(os.path.join(directory, "cpuinfo_cur_freq"))
        if value is not None:
            values.append(value)
    return round(sum(values) / len(values) / 1e6, 2) if values else None


def base_freq():
    for directory in cpu_dirs():
        value = _read_int(os.path.join(directory, "base_frequency"), 100_000, MAX_FREQ_KHZ)
        if value is not None:
            return value
    for entry in sorted(glob.glob(os.path.join(CPU_ROOT, "cpu[0-9]*", "acpi_cppc", "nominal_freq"))):
        value = _read_int(entry, 1, MAX_FREQ_KHZ)
        if value is not None:
            return value * 1_000 if value < 100_000 else value
    return None


def cpu_info():
    model = "Unknown CPU"
    cores = None
    threads = 0
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            for line in fh:
                key, _, value = line.partition(":")
                name = key.strip().lower()
                text = value.strip()
                if name == "model name" and model == "Unknown CPU":
                    model = text or model
                elif name == "cpu cores" and cores is None:
                    try:
                        cores = int(text)
                    except ValueError:
                        pass
                elif name == "processor":
                    threads += 1
    except OSError:
        pass
    return {"model": model, "cores": cores, "threads": threads}


def persisted():
    return None


def get_state(previous_cpu=None, include_internal=False):
    caps = []
    turbos = []
    for directory in cpu_dirs(online_only=True):
        cap = _read_int(os.path.join(directory, "scaling_max_freq"), 1, MAX_FREQ_KHZ)
        turbo = _read_int(os.path.join(directory, "cpuinfo_max_freq"), 1, MAX_FREQ_KHZ)
        if cap is not None:
            caps.append(cap)
        if turbo is not None:
            turbos.append(turbo)
    current_cpu = _cpu_times()
    base = base_freq()
    info = cpu_info()
    state = {
        "version": 1,
        "source": "fallback",
        "time": int(time.time()),
        "max": round(max(caps) / 1e6, 1) if caps else None,
        "turbo": round(max(turbos) / 1e6, 1) if turbos else None,
        "base": round(base / 1e6, 1) if base else None,
        "temp": package_temp(),
        "freq": current_frequency(),
        "power": None,
        "util": utilization(previous_cpu, current_cpu),
        "model": info["model"],
        "cores": info["cores"],
        "threads": info["threads"],
        "default": persisted(),
    }
    if include_internal:
        state["_cpuTimes"] = current_cpu
    return state


def write_state(state_file, state):
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(state_file, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise ValueError("refusing unsafe monitor state file")
        os.ftruncate(fd, 0)
        os.fchmod(fd, 0o600)
        payload = (json.dumps(state, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short monitor state write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def monitor(state_file, interval=2.0):
    previous_cpu = None
    sequence = 0
    instance = str(os.getpid())
    while True:
        try:
            state = get_state(previous_cpu, include_internal=True)
            previous_cpu = state.pop("_cpuTimes", None)
            sequence += 1
            state["seq"] = sequence
            state["instance"] = instance
            state["time"] = int(time.time())
            write_state(state_file, state)
        except Exception:
            pass
        time.sleep(interval)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="boostctl.py", description=__doc__)
    parser.add_argument("--state-file")
    parser.add_argument("command", nargs="?", choices=["get", "monitor"])
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.command == "get":
        print(json.dumps(get_state(), allow_nan=False, separators=(",", ":")))
        return 0
    if args.command == "monitor":
        if not args.state_file:
            parser.error("monitor needs --state-file")
        monitor(args.state_file)
        return 0
    parser.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
