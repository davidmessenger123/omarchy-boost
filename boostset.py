#!/usr/bin/python3 -I
"""Apply a validated CPU boost policy through fixed root-owned paths."""

import copy
import fcntl
import glob
import json
import math
import os
import re
import secrets
import stat
import sys
import time

CPU_ROOT = "/sys/devices/system/cpu"
DAEMON_PATH = "/usr/local/libexec/omarchy-boost/boostmonitord.py"
STATE_DIR = "/var/lib/omarchy-boost"
STATE_PATH = os.path.join(STATE_DIR, "maxboost")
POLICY_PATH = STATE_PATH + ".policy"
UNINSTALL_PATH = os.path.join(STATE_DIR, ".uninstall")
RUNTIME_DIR = "/run/omarchy-boost"
RUNTIME_STATE_PATH = os.path.join(RUNTIME_DIR, "state.json")
STATE_OWNER_UID = 0
MAX_PERSIST_BYTES = 128
MAX_POLICY_BYTES = 8192
MAX_RUNTIME_BYTES = 65536
MIN_GHZ = 0.1
MAX_GHZ = 100.0
MAX_FREQ_KHZ = 10_000_000
MIN_TEMP_C = -20.0
MAX_TEMP_C = 150.0
MIN_COOLDOWN_SECONDS = 0
MAX_COOLDOWN_SECONDS = 86400
MIN_BOOST_SECONDS = 1
MAX_BOOST_SECONDS = 86400
HEX_RE = re.compile(r"^[0-9a-f]{32}$")


class ControlError(Exception):
    pass


def _reject_constant(token):
    raise ValueError(token)


def _flags(value):
    return value | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _ensure_root_dir(path, mode=0o700):
    path = os.path.abspath(path)
    parts = [part for part in path.split(os.sep) if part]
    current = os.path.sep
    last = len(parts) - 1
    for index, part in enumerate(parts):
        current = os.path.join(current, part)
        try:
            os.mkdir(current, mode)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ControlError(f"cannot create state directory {current}: {exc}") from exc
        try:
            fd = os.open(current, _flags(os.O_RDONLY | os.O_DIRECTORY))
        except OSError as exc:
            raise ControlError(f"cannot open state directory {current}: {exc}") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode):
                raise ControlError(f"state path is not a directory: {current}")
            if index == last and info.st_uid != STATE_OWNER_UID:
                raise ControlError(f"state directory is not root-owned: {current}")
            if index == last and info.st_mode & 0o022:
                raise ControlError(f"state directory is group/world writable: {current}")
        finally:
            os.close(fd)


def _open_checked(path, flags, mode=0o600, max_bytes=MAX_PERSIST_BYTES):
    try:
        fd = os.open(path, _flags(flags), mode)
    except OSError as exc:
        raise ControlError(f"cannot open {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ControlError(f"refusing non-regular file: {path}")
        if info.st_uid != STATE_OWNER_UID:
            raise ControlError(f"refusing file not owned by root: {path}")
        if info.st_nlink != 1:
            raise ControlError(f"refusing hard-linked file: {path}")
        if info.st_mode & 0o022:
            raise ControlError(f"refusing group/world-writable file: {path}")
        if info.st_size > max_bytes:
            raise ControlError(f"refusing oversized file: {path}")
        return fd
    except Exception:
        os.close(fd)
        raise


def _read_file(path, max_bytes=MAX_PERSIST_BYTES):
    fd = _open_checked(path, os.O_RDONLY | os.O_NONBLOCK, max_bytes=max_bytes)
    try:
        chunks = []
        total = 0
        while total <= max_bytes:
            chunk = os.read(fd, min(4096, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ControlError(f"refusing oversized file: {path}")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _validate_existing(path, max_bytes=MAX_PERSIST_BYTES):
    try:
        fd = _open_checked(path, os.O_RDONLY | os.O_NONBLOCK, max_bytes=max_bytes)
    except ControlError:
        if not os.path.lexists(path):
            return False
        raise
    os.close(fd)
    return True


def _read_optional_file(path, max_bytes):
    try:
        return _read_file(path, max_bytes=max_bytes)
    except ControlError:
        if not os.path.lexists(path):
            return None
        raise


def _atomic_write_bytes(path, payload, mode, max_bytes):
    if not isinstance(payload, bytes) or len(payload) > max_bytes:
        raise ControlError("state payload is invalid")
    parent = os.path.dirname(path)
    _ensure_root_dir(parent, 0o755 if parent == RUNTIME_DIR else 0o700)
    _validate_existing(path, max_bytes=max_bytes)
    temp_path = os.path.join(parent, "." + os.path.basename(path) + "." + secrets.token_hex(12) + ".tmp")
    temp_fd = os.open(temp_path, _flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL), mode)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(temp_fd, view)
            if written <= 0:
                raise ControlError("short state write")
            view = view[written:]
        os.fchmod(temp_fd, mode)
        os.fsync(temp_fd)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(temp_fd)
    try:
        os.replace(temp_path, path)
        directory_fd = os.open(parent, _flags(os.O_RDONLY | os.O_DIRECTORY))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def _restore_optional_file(path, raw, max_bytes):
    if raw is None:
        if os.path.lexists(path):
            _validate_existing(path, max_bytes=max_bytes)
            os.unlink(path)
        return
    _atomic_write_bytes(path, raw, 0o600, max_bytes)


def _finite_number(value, lower, upper, name):
    if isinstance(value, bool):
        raise ControlError(f"{name} must be finite")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ControlError(f"{name} must be finite") from exc
    if not math.isfinite(number) or number < lower or number > upper:
        raise ControlError(f"{name} is out of range")
    return number


def parse_cap(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ControlError("cap must be a string or finite GHz value")
    text = str(value).strip().lower()
    if text == "max":
        return "max"
    try:
        number = float(text)
    except ValueError as exc:
        raise ControlError("cap must be a finite GHz value or max") from exc
    if not math.isfinite(number) or number < MIN_GHZ or number > MAX_GHZ:
        raise ControlError("cap must be finite and between 0.1 and 100 GHz")
    return round(number, 1)


def _canonical_cap(value):
    value = parse_cap(value)
    return value if value == "max" else f"{value:.1f}"


def parse_enabled(value):
    if value in (1, "1", True):
        return True
    if value in (0, "0", False):
        return False
    raise ControlError("enabled must be 0 or 1")


def parse_temperature(value):
    return round(_finite_number(value, MIN_TEMP_C, MAX_TEMP_C, "temperature"), 1)


def parse_cooldown(value):
    number = _finite_number(value, MIN_COOLDOWN_SECONDS, MAX_COOLDOWN_SECONDS, "cooldown")
    if abs(number - round(number)) > 0.000001:
        raise ControlError("cooldown must be whole seconds")
    return int(round(number))


def parse_boost_seconds(value):
    number = _finite_number(value, MIN_BOOST_SECONDS, MAX_BOOST_SECONDS, "boost duration")
    if abs(number - round(number)) > 0.000001:
        raise ControlError("boost duration must be whole seconds")
    return int(round(number))


def _default_guard():
    return {
        "enabled": False,
        "highC": 90.0,
        "lowC": 80.0,
        "cap": "3.0",
        "cooldownSec": 60,
        "latched": False,
        "coolSince": None,
    }


def _default_policy(initialized=False):
    return {
        "version": 1,
        "initialized": bool(initialized),
        "baseline": "max",
        "guard": _default_guard(),
        "boostUntil": 0.0,
        "boostCap": "max",
        "bootId": "",
    }


def _normalize_policy(value):
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ControlError("persisted CPU policy is invalid")
    initialized = value.get("initialized")
    if not isinstance(initialized, bool):
        raise ControlError("persisted CPU policy is invalid")
    baseline = _canonical_cap(value.get("baseline"))
    boost_until = _finite_number(value.get("boostUntil"), 0, 4102444800.0, "boost deadline")
    boot_id = value.get("bootId", "")
    if not isinstance(boot_id, str) or (boot_id and not HEX_RE.fullmatch(boot_id)):
        raise ControlError("persisted CPU policy is invalid")
    guard = value.get("guard")
    if not isinstance(guard, dict):
        raise ControlError("persisted thermal guard is invalid")
    enabled = guard.get("enabled")
    latched = guard.get("latched")
    if not isinstance(enabled, bool) or not isinstance(latched, bool):
        raise ControlError("persisted thermal guard is invalid")
    high = parse_temperature(guard.get("highC"))
    low = parse_temperature(guard.get("lowC"))
    if low >= high:
        raise ControlError("thermal guard release temperature must be below trigger temperature")
    cap = _canonical_cap(guard.get("cap"))
    if cap == "max":
        raise ControlError("thermal guard cap must be below the CPU limit")
    cooldown = parse_cooldown(guard.get("cooldownSec"))
    cool_since = guard.get("coolSince")
    if cool_since is not None:
        cool_since = _finite_number(cool_since, 0, 4102444800.0, "cool timestamp")
    return {
        "version": 1,
        "initialized": initialized,
        "baseline": baseline,
        "boostCap": _canonical_cap(value.get("boostCap", "max")),
        "guard": {
            "enabled": enabled,
            "highC": high,
            "lowC": low,
            "cap": cap,
            "cooldownSec": cooldown,
            "latched": latched,
            "coolSince": cool_since,
        },
        "boostUntil": boost_until,
        "bootId": boot_id,
    }


def _read_persisted_unlocked(path):
    _ensure_root_dir(os.path.dirname(path))
    try:
        raw = _read_file(path)
    except ControlError as exc:
        if not os.path.lexists(path):
            raise ControlError("no persisted CPU cap") from exc
        raise
    try:
        text = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ControlError("persisted CPU cap is not ASCII") from exc
    if not text or "\n" in text or "\x00" in text:
        raise ControlError("persisted CPU cap is malformed")
    return parse_cap(text)


def _read_persisted(path=None):
    return _read_persisted_unlocked(path or STATE_PATH)


def _read_policy_unlocked(state_path):
    policy_path = state_path + ".policy"
    raw = _read_optional_file(policy_path, MAX_POLICY_BYTES)
    if raw is not None:
        try:
            policy = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ControlError("persisted CPU policy is malformed") from exc
        return _normalize_policy(policy)
    raw_state = _read_optional_file(state_path, MAX_PERSIST_BYTES)
    if raw_state is None:
        return _default_policy(False)
    try:
        text = raw_state.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ControlError("persisted CPU cap is not ASCII") from exc
    if not text or "\n" in text or "\x00" in text:
        raise ControlError("persisted CPU cap is malformed")
    baseline = parse_cap(text)
    return _default_policy(True) | {"baseline": _canonical_cap(baseline)}


def _boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as handle:
            value = handle.read(128).strip().lower().replace("-", "")
    except OSError:
        return ""
    return value if HEX_RE.fullmatch(value) else ""


def _prepare_policy_for_boot(policy):
    current = _boot_id()
    if not current:
        policy["boostUntil"] = 0.0
        policy["bootId"] = ""
        policy["guard"]["coolSince"] = None
    elif policy.get("bootId") != current:
        policy["boostUntil"] = 0.0
        policy["bootId"] = current
        policy["guard"]["coolSince"] = None
    return policy


def _write_persisted_unlocked(value, path):
    value = parse_cap(value)
    payload = (_canonical_cap(value) + "\n").encode("ascii")
    _atomic_write_bytes(path, payload, 0o600, MAX_PERSIST_BYTES)


def _write_persisted(value, path=None):
    path = path or STATE_PATH
    lock_fd = _acquire_state_lock(path)
    try:
        _write_persisted_unlocked(value, path)
    finally:
        _release_state_lock(lock_fd)


def _acquire_state_lock(path):
    parent = os.path.dirname(path)
    _ensure_root_dir(parent)
    lock_path = path + ".lock"
    lock_fd = _open_checked(lock_path, os.O_RDWR | os.O_CREAT, 0o600, 4096)
    try:
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return lock_fd
    except Exception:
        os.close(lock_fd)
        raise


def _release_state_lock(lock_fd):
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


def _commit_policy_unlocked(policy, state_path):
    policy_path = state_path + ".policy"
    old_policy = _read_optional_file(policy_path, MAX_POLICY_BYTES)
    old_state = _read_optional_file(state_path, MAX_PERSIST_BYTES)
    payload = (json.dumps(policy, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    try:
        _atomic_write_bytes(policy_path, payload, 0o600, MAX_POLICY_BYTES)
        if policy["initialized"]:
            _write_persisted_unlocked(policy["baseline"], state_path)
    except Exception as exc:
        restore_errors = []
        for path, raw, limit in ((policy_path, old_policy, MAX_POLICY_BYTES), (state_path, old_state, MAX_PERSIST_BYTES)):
            try:
                _restore_optional_file(path, raw, limit)
            except Exception as restore_exc:
                restore_errors.append(str(restore_exc))
        if restore_errors:
            raise ControlError(f"{exc}; state rollback failed: {'; '.join(restore_errors)}") from exc
        raise


def _cpu_number(directory):
    name = os.path.basename(os.path.dirname(directory))
    if not name.startswith("cpu"):
        return None
    try:
        return int(name[3:])
    except ValueError:
        return None


def _online(directory):
    path = os.path.join(os.path.dirname(directory), "online")
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read(64).strip() == "1"
    except FileNotFoundError:
        return True
    except OSError:
        return False


def cpu_dirs():
    paths = glob.glob(os.path.join(CPU_ROOT, "cpu[0-9]*", "cpufreq"))
    paths.sort(key=lambda path: _cpu_number(path) if _cpu_number(path) is not None else -1)
    output = []
    seen = set()
    for path in paths:
        number = _cpu_number(path)
        if number is None or number in seen:
            continue
        try:
            info = os.stat(path)
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode) or not _online(path):
            continue
        seen.add(number)
        output.append(path)
    return output


def _read_int(path, lower=1, upper=MAX_FREQ_KHZ):
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read(128).strip().split()[0]
        value = int(raw)
    except (OSError, ValueError, IndexError):
        return None
    if value < lower or value > upper:
        return None
    return value


def _write_sysfs(path, value):
    try:
        fd = os.open(path, _flags(os.O_WRONLY))
    except OSError as exc:
        raise ControlError(f"cannot open CPU control {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1:
            raise ControlError(f"refusing CPU control {path}")
        payload = (str(value) + "\n").encode("ascii")
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise ControlError(f"short write to CPU control {path}")
            view = view[written:]
    finally:
        os.close(fd)


def _apply_cap(value, skip_unchanged=False):
    value = parse_cap(value)
    directories = cpu_dirs()
    if not directories:
        raise ControlError("no online CPU frequency controls found")
    targets = []
    previous = {}
    valid = 0
    for directory in directories:
        ceiling = _read_int(os.path.join(directory, "cpuinfo_max_freq"))
        if ceiling is None:
            raise ControlError(f"cannot read CPU ceiling at {directory}")
        target = ceiling if value == "max" else int(round(value * 1_000_000))
        target = min(target, ceiling)
        if target <= 0:
            raise ControlError(f"invalid CPU cap target at {directory}")
        valid += 1
        control = os.path.join(directory, "scaling_max_freq")
        current = _read_int(control)
        if current is None:
            raise ControlError(f"cannot read current CPU cap at {control}")
        if skip_unchanged and current == target:
            continue
        targets.append((control, target))
        previous[control] = current
    if valid == 0:
        raise ControlError("no valid CPU frequency controls found")
    if not targets:
        return value, previous
    try:
        for control, target in targets:
            _write_sysfs(control, target)
            actual = _read_int(control)
            if actual != target:
                raise ControlError(f"CPU rejected cap at {control}")
    except Exception as exc:
        rollback_errors = _restore_caps(previous)
        if rollback_errors:
            details = "; ".join(rollback_errors)
            raise ControlError(f"{exc}; rollback failed: {details}") from exc
        raise
    return value, previous


def _restore_caps(previous):
    errors = []
    for control, target in reversed(list(previous.items())):
        try:
            _write_sysfs(control, target)
            actual = _read_int(control)
            if actual != target:
                raise ControlError("readback mismatch")
        except (ControlError, OSError) as exc:
            errors.append(f"{control}: {exc}")
    return errors


def cap_all(value):
    return _apply_cap(value)[0]


def _update_guard(policy, temperature, now):
    guard = policy["guard"]
    if guard["latched"]:
        if temperature is None or temperature > guard["lowC"]:
            guard["coolSince"] = None
        else:
            if guard["coolSince"] is None:
                guard["coolSince"] = now
            if now - guard["coolSince"] >= guard["cooldownSec"]:
                guard["latched"] = False
                guard["coolSince"] = None
    elif guard["enabled"] and temperature is not None and temperature >= guard["highC"]:
        guard["latched"] = True
        guard["coolSince"] = None


def _effective_cap(policy, now):
    if policy["boostUntil"] > 0 and policy["boostUntil"] <= now:
        policy["boostUntil"] = 0.0
    if policy["guard"]["latched"]:
        baseline = parse_cap(policy["baseline"])
        guard_cap = parse_cap(policy["guard"]["cap"])
        if baseline == "max":
            return guard_cap
        if guard_cap == "max":
            return baseline
        return _canonical_cap(min(baseline, guard_cap))
    if policy["boostUntil"] > now:
        return policy["boostCap"]
    return policy["baseline"]


def _apply_candidate(candidate, previous_policy, temperature, now, state_path, force=True):
    _update_guard(candidate, temperature, now)
    effective = _effective_cap(candidate, now)
    if candidate["initialized"]:
        applied, previous_caps = _apply_cap(effective, skip_unchanged=not force)
    else:
        applied, previous_caps = None, {}
    if candidate != previous_policy:
        try:
            _commit_policy_unlocked(candidate, state_path)
        except Exception as exc:
            rollback_errors = _restore_caps(previous_caps)
            if rollback_errors:
                raise ControlError(f"{exc}; CPU rollback failed: {'; '.join(rollback_errors)}") from exc
            raise
    return candidate, effective, applied


def _run_change(state_path, mutator, temperature=None, now=None, force=True, allow_uninstall=False):
    lock_fd = _acquire_state_lock(state_path)
    try:
        if not allow_uninstall and os.path.lexists(UNINSTALL_PATH):
            raise ControlError("root integration is being removed")
        policy = _prepare_policy_for_boot(_read_policy_unlocked(state_path))
        previous_policy = copy.deepcopy(policy)
        mutator(policy)
        temperature = package_temp() if temperature is None else temperature
        now = time.time() if now is None else now
        return _apply_candidate(policy, previous_policy, temperature, now, state_path, force)
    finally:
        _release_state_lock(lock_fd)


def set_cap(value, state_path=None, allow_uninstall=False):
    path = state_path or STATE_PATH
    baseline = _canonical_cap(value)

    def mutate(policy):
        policy["initialized"] = True
        policy["baseline"] = baseline
        policy["boostUntil"] = 0.0

    return _run_change(path, mutate, allow_uninstall=allow_uninstall)[1]


def initialize_baseline(value, state_path=None):
    path = state_path or STATE_PATH
    baseline = _canonical_cap(value)

    def mutate(policy):
        if not policy["initialized"]:
            policy["initialized"] = True
            policy["baseline"] = baseline

    return _run_change(path, mutate)[1]


def configure_guard(enabled, high_c, low_c, cap, cooldown_sec, state_path=None):
    path = state_path or STATE_PATH
    enabled = parse_enabled(enabled)
    high_c = parse_temperature(high_c)
    low_c = parse_temperature(low_c)
    if low_c >= high_c:
        raise ControlError("thermal guard release temperature must be below trigger temperature")
    cap = _canonical_cap(cap)
    if cap == "max":
        raise ControlError("thermal guard cap must be below the CPU limit")
    cooldown_sec = parse_cooldown(cooldown_sec)

    def mutate(policy):
        guard = policy["guard"]
        was_latched = guard["latched"]
        guard.update({
            "enabled": enabled,
            "highC": high_c,
            "lowC": low_c,
            "cap": cap,
            "cooldownSec": cooldown_sec,
        })
        if not was_latched:
            guard["coolSince"] = None

    return _run_change(path, mutate)[1]


def start_boost(seconds, state_path=None, cap="max"):
    path = state_path or STATE_PATH
    seconds = parse_boost_seconds(seconds)
    boost_cap = _canonical_cap(cap)
    lock_fd = _acquire_state_lock(path)
    try:
        if os.path.lexists(UNINSTALL_PATH):
            raise ControlError("root integration is being removed")
        policy = _prepare_policy_for_boot(_read_policy_unlocked(path))
        if not policy["initialized"]:
            raise ControlError("manual CPU baseline is not initialized")
        previous_policy = copy.deepcopy(policy)
        temperature = package_temp()
        now = time.time()
        _update_guard(policy, temperature, now)
        if policy["guard"]["latched"]:
            _apply_candidate(policy, previous_policy, temperature, now, path)
            raise ControlError("thermal guard is active")
        policy["boostUntil"] = now + seconds
        policy["boostCap"] = boost_cap
        return _apply_candidate(policy, previous_policy, temperature, now, path)[1]
    finally:
        _release_state_lock(lock_fd)


def reset_policy(state_path=None):
    path = state_path or STATE_PATH
    lock_fd = _acquire_state_lock(path)
    try:
        policy = _default_policy(True)
        policy["bootId"] = _boot_id()
        applied, previous_caps = _apply_cap("max")
        try:
            _commit_policy_unlocked(policy, path)
        except Exception as exc:
            rollback_errors = _restore_caps(previous_caps)
            if rollback_errors:
                raise ControlError(f"{exc}; CPU rollback failed: {'; '.join(rollback_errors)}") from exc
            raise
        return applied
    finally:
        _release_state_lock(lock_fd)


def cancel_boost(state_path=None):
    path = state_path or STATE_PATH

    def mutate(policy):
        policy["boostUntil"] = 0.0

    return _run_change(path, mutate)[1]


def apply_persisted(state_path=None):
    path = state_path or STATE_PATH
    return _run_change(path, lambda policy: None)[1]


def _read_text(path, limit=4096):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read(limit + 1)
    except OSError:
        return ""


def _read_sensor_int(path, lower=-50000, upper=200000):
    value = _read_int(path, lower, upper)
    return None if value is None else value / 1000.0


def package_temp(hwmon_root="/sys/class/hwmon"):
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
            for filename in sorted(os.listdir(entry.path)):
                if not filename.endswith("_label"):
                    continue
                stem = filename[:-len("_label")]
                label = _read_text(os.path.join(entry.path, stem + "_label"), 128).strip().lower().replace(" ", "")
                if name == "coretemp" and (label == "packageid0" or label.startswith("packageid")):
                    value = _read_sensor_int(os.path.join(entry.path, stem + "_input"))
                    if value is not None:
                        values.append(value)
                elif name in ("k10temp", "zenpower") and (label in ("tctl", "tdie", "package", "packageid0") or label.startswith("package")):
                    value = _read_sensor_int(os.path.join(entry.path, stem + "_input"))
                    if value is not None:
                        values.append(value)
            if name in ("k10temp", "zenpower"):
                value = _read_sensor_int(os.path.join(entry.path, "temp1_input"))
                if value is not None:
                    values.append(value)
        except OSError:
            continue
    return round(max(values), 1) if values else None


def _base_frequency():
    for directory in cpu_dirs():
        value = _read_int(os.path.join(directory, "base_frequency"), 100000, MAX_FREQ_KHZ)
        if value is not None:
            return value
    for path in sorted(glob.glob(os.path.join(CPU_ROOT, "cpu[0-9]*", "acpi_cppc", "nominal_freq"))):
        value = _read_int(path, 1, MAX_FREQ_KHZ)
        if value is not None:
            return value * 1000 if value < 100000 else value
    return None


def _cpu_metrics():
    caps = []
    turbos = []
    frequencies = []
    for directory in cpu_dirs():
        cap = _read_int(os.path.join(directory, "scaling_max_freq"))
        turbo = _read_int(os.path.join(directory, "cpuinfo_max_freq"))
        frequency = _read_int(os.path.join(directory, "scaling_cur_freq"))
        if frequency is None:
            frequency = _read_int(os.path.join(directory, "cpuinfo_cur_freq"))
        if cap is not None:
            caps.append(cap)
        if turbo is not None:
            turbos.append(turbo)
        if frequency is not None:
            frequencies.append(frequency)
    base = _base_frequency()
    return {
        "max": round(max(caps) / 1e6, 1) if caps else None,
        "turbo": round(max(turbos) / 1e6, 1) if turbos else None,
        "base": round(base / 1e6, 1) if base else None,
        "freq": round(sum(frequencies) / len(frequencies) / 1e6, 2) if frequencies else None,
    }


def _cpu_info():
    model = "Unknown CPU"
    cores = None
    threads = 0
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as handle:
            for line in handle:
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
    return model, cores, threads


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


def _utilization(previous, current):
    if previous is None or current is None:
        return None
    total_delta = current[0] - previous[0]
    idle_delta = current[1] - previous[1]
    if total_delta <= 0 or idle_delta < 0 or idle_delta > total_delta:
        return None
    return round(100.0 * (total_delta - idle_delta) / total_delta, 1)


def _rapl_zones(root="/sys/class/powercap"):
    candidates = []
    fallback = []
    try:
        entries = sorted(os.scandir(root), key=lambda entry: entry.name)
    except OSError:
        return {}
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=True):
                continue
            name = _read_text(os.path.join(entry.path, "name"), 128).strip().lower()
            energy = _read_int(os.path.join(entry.path, "energy_uj"), 0, 2**63 - 1)
            maximum = _read_int(os.path.join(entry.path, "max_energy_range_uj"), 1, 2**63 - 1)
            if not name or energy is None or maximum is None:
                continue
            item = (energy, maximum)
            if name.startswith("package-"):
                candidates.append((name, item))
            elif name.startswith("psys"):
                fallback.append((name, item))
        except OSError:
            continue
    selected = {}
    for name, item in candidates:
        selected.setdefault(name, item)
    if selected:
        return selected
    for name, item in fallback:
        selected.setdefault(name, item)
    return selected


def _rapl_power(previous, current, elapsed):
    if previous is None or current is None or elapsed <= 0:
        return None, current
    total = 0
    found = False
    for name, item in current.items():
        old_item = previous.get(name)
        if old_item is None:
            continue
        current_energy, maximum = item
        old_energy, old_maximum = old_item
        if maximum != old_maximum:
            continue
        delta = current_energy - old_energy
        if delta < 0:
            delta += maximum
        if delta < 0 or delta >= maximum:
            continue
        total += delta
        found = True
    return (round(total / elapsed / 1e6, 2) if found else None), current


def _write_runtime_state(payload):
    _ensure_root_dir(RUNTIME_DIR, 0o755)
    data = (json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    _atomic_write_bytes(RUNTIME_STATE_PATH, data, 0o644, MAX_RUNTIME_BYTES)


def run_daemon(interval=1.0):
    if os.path.lexists(UNINSTALL_PATH):
        return
    interval = _finite_number(interval, 0.05, 60.0, "monitor interval")
    sequence = 0
    instance = secrets.token_hex(12)
    previous_cpu = None
    previous_power = None
    previous_power_time = None
    last_policy = _default_policy(False)
    last_effective = "max"
    while True:
        if os.path.lexists(UNINSTALL_PATH):
            return
        started = time.monotonic()
        now = time.time()
        error = ""
        temperature = package_temp()
        try:
            last_policy, last_effective, _ = _run_change(STATE_PATH, lambda policy: None, temperature, now, force=False)
        except (ControlError, OSError, ValueError) as exc:
            error = str(exc)[:256]
        metrics = _cpu_metrics()
        cpu_times = _cpu_times()
        utilization = _utilization(previous_cpu, cpu_times)
        previous_cpu = cpu_times
        zones = _rapl_zones()
        power, previous_power = _rapl_power(previous_power, zones, now - (previous_power_time or now))
        previous_power_time = now
        model, cores, threads = _cpu_info()
        sequence += 1
        guard = last_policy["guard"]
        payload = {
            "version": 1,
            "seq": sequence,
            "instance": instance,
            "bootId": last_policy["bootId"],
            "time": int(now),
            "max": metrics["max"],
            "turbo": metrics["turbo"],
            "base": metrics["base"],
            "temp": temperature,
            "freq": metrics["freq"],
            "power": power,
            "util": utilization,
            "model": model,
            "cores": cores,
            "threads": threads,
            "baseline": last_policy["baseline"],
            "effective": last_effective,
            "initialized": last_policy["initialized"],
            "boostUntil": last_policy["boostUntil"],
            "boostCap": last_policy["boostCap"],
            "guardEnabled": guard["enabled"],
            "guardLatched": guard["latched"],
            "guardHighC": guard["highC"],
            "guardLowC": guard["lowC"],
            "guardCap": guard["cap"],
            "guardCooldown": guard["cooldownSec"],
            "error": error,
        }
        _write_runtime_state(payload)
        time.sleep(max(0.0, interval - (time.monotonic() - started)))


def main():
    if os.geteuid() != 0:
        print("boostset.py must run as root", file=sys.stderr)
        return 1
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    uninstall_max = command == "set" and len(sys.argv) == 3 and str(sys.argv[2]).strip().lower() == "max"
    if os.path.lexists(UNINSTALL_PATH) and command not in ("reset", "run") and not uninstall_max:
        print("boostset.py: root integration is being removed", file=sys.stderr)
        return 1
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "set":
            set_cap(sys.argv[2], allow_uninstall=uninstall_max)
        elif len(sys.argv) == 3 and sys.argv[1] == "initialize":
            initialize_baseline(sys.argv[2])
        elif len(sys.argv) == 7 and sys.argv[1] == "guard":
            configure_guard(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6])
        elif len(sys.argv) == 4 and sys.argv[1] == "boost":
            start_boost(sys.argv[2], cap=sys.argv[3])
        elif len(sys.argv) == 2 and sys.argv[1] == "cancel-boost":
            cancel_boost()
        elif len(sys.argv) == 2 and sys.argv[1] == "reset":
            if not os.path.lexists(UNINSTALL_PATH):
                raise ControlError("reset is reserved for uninstall")
            reset_policy()
        elif len(sys.argv) == 2 and sys.argv[1] == "apply":
            apply_persisted()
        elif len(sys.argv) == 2 and sys.argv[1] == "run":
            if os.path.realpath(sys.argv[0]) != DAEMON_PATH:
                raise ControlError("run is reserved for the systemd monitor")
            run_daemon()
        else:
            print("usage: boostset.py set <GHz|max> | initialize <GHz|max> | guard <enabled> <highC> <lowC> <GHz> <cooldownSec> | boost <seconds> <GHz|max> | cancel-boost | reset | apply | run", file=sys.stderr)
            return 2
        return 0
    except KeyboardInterrupt:
        return 0
    except (ControlError, OSError, ValueError) as exc:
        print(f"boostset.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
