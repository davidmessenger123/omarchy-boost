#!/usr/bin/python3 -I
"""Apply a validated CPU boost cap through fixed root-owned paths."""

import fcntl
import glob
import math
import os
import secrets
import stat
import sys

CPU_ROOT = "/sys/devices/system/cpu"
STATE_DIR = "/var/lib/omarchy-boost"
STATE_PATH = os.path.join(STATE_DIR, "maxboost")
STATE_OWNER_UID = 0
MAX_PERSIST_BYTES = 128
MIN_GHZ = 0.1
MAX_GHZ = 100.0
MAX_FREQ_KHZ = 10_000_000


class ControlError(Exception):
    pass


def _flags(value):
    return value | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _ensure_root_dir(path):
    path = os.path.abspath(path)
    parts = [part for part in path.split(os.sep) if part]
    current = os.path.sep
    last = len(parts) - 1
    for index, part in enumerate(parts):
        current = os.path.join(current, part)
        try:
            os.mkdir(current, 0o700)
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
    return value if value == "max" else f"{value:.1f}"


def _read_int(path, lower=1, upper=MAX_FREQ_KHZ):
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read(128).strip().split()[0]
        value = int(raw)
    except (OSError, ValueError, IndexError):
        return None
    if value < lower or value > upper:
        return None
    return value


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
        with open(path, encoding="utf-8") as fh:
            return fh.read(64).strip() == "1"
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
            info = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode) or not _online(path):
            continue
        seen.add(number)
        output.append(path)
    return output


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


def _apply_cap(value):
    value = parse_cap(value)
    directories = cpu_dirs()
    if not directories:
        raise ControlError("no online CPU frequency controls found")
    targets = []
    previous = {}
    for directory in directories:
        ceiling = _read_int(os.path.join(directory, "cpuinfo_max_freq"))
        if ceiling is None:
            continue
        target = ceiling if value == "max" else int(round(value * 1_000_000))
        target = min(target, ceiling)
        if target <= 0:
            continue
        control = os.path.join(directory, "scaling_max_freq")
        current = _read_int(control)
        if current is None:
            raise ControlError(f"cannot read current CPU cap at {control}")
        targets.append((control, target))
        previous[control] = current
    if not targets:
        raise ControlError("no valid CPU frequency ceilings found")
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


def _validate_existing(path):
    try:
        fd = _open_checked(path, os.O_RDONLY | os.O_NONBLOCK)
    except ControlError:
        if not os.path.lexists(path):
            return False
        raise
    os.close(fd)
    return True


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


def _write_persisted_unlocked(value, path):
    value = parse_cap(value)
    parent = os.path.dirname(path)
    _ensure_root_dir(parent)
    _validate_existing(path)
    payload = (_canonical_cap(value) + "\n").encode("ascii")
    temp_path = os.path.join(parent, "." + os.path.basename(path) + "." + secrets.token_hex(12) + ".tmp")
    temp_fd = os.open(temp_path, _flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL), 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(temp_fd, view)
            if written <= 0:
                raise ControlError("short persistence write")
            view = view[written:]
        os.fchmod(temp_fd, 0o600)
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


def _write_persisted(value, path=None):
    path = path or STATE_PATH
    lock_fd = _acquire_state_lock(path)
    try:
        _write_persisted_unlocked(value, path)
    finally:
        _release_state_lock(lock_fd)


def set_cap(value, state_path=None):
    path = state_path or STATE_PATH
    lock_fd = _acquire_state_lock(path)
    previous = None
    applied = None
    try:
        applied, previous = _apply_cap(value)
        _write_persisted_unlocked(applied, path)
    except Exception as exc:
        if previous is not None:
            rollback_errors = _restore_caps(previous)
            if rollback_errors:
                details = "; ".join(rollback_errors)
                raise ControlError(f"{exc}; rollback failed: {details}") from exc
        raise
    finally:
        _release_state_lock(lock_fd)
    return applied


def apply_persisted(state_path=None):
    path = state_path or STATE_PATH
    lock_fd = _acquire_state_lock(path)
    try:
        return cap_all(_read_persisted_unlocked(path))
    finally:
        _release_state_lock(lock_fd)


def main():
    if os.geteuid() != 0:
        print("boostset.py must run as root", file=sys.stderr)
        return 1
    if len(sys.argv) == 3 and sys.argv[1] == "set":
        operation = lambda: set_cap(sys.argv[2])
    elif len(sys.argv) == 2 and sys.argv[1] == "apply":
        operation = apply_persisted
    else:
        print("usage: boostset.py set <GHz|max> | apply", file=sys.stderr)
        return 2
    try:
        operation()
        return 0
    except (ControlError, OSError, ValueError) as exc:
        print(f"boostset.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
