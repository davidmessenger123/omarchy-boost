#!/usr/bin/env python3
"""Validate and atomically persist davidjm.boost settings in shell.json."""

import base64
import errno
import fcntl
import json
import math
import os
import re
import secrets
import stat
import sys

PLUGIN_ID = "davidjm.boost"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".config", "omarchy", "shell.json")
LOCK_NAME = ".shell.json.lock"
JOURNAL_NAME = ".shell.json.transaction.json"
LEGACY_JOURNAL_NAME = ".bar-editor.transaction.json"
BACKUP_SUFFIX = ".bak-editor"
MAX_CONFIG_BYTES = 1 << 20
MAX_JOURNAL_BYTES = 8 << 20
MAX_INPUT_BYTES = 256 * 1024
MAX_STRING = 4096
MAX_COLLECTION = 100
KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")
MIN_GHZ = 0.1
MAX_GHZ = 100.0


class PersistenceError(Exception):
    pass


def _flags(value):
    return value | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _ensure_user_dir(path):
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
            raise PersistenceError(f"cannot create config directory {current}: {exc}") from exc
        try:
            fd = os.open(current, _flags(os.O_RDONLY | os.O_DIRECTORY))
        except OSError as exc:
            raise PersistenceError(f"cannot open config directory {current}: {exc}") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode):
                raise PersistenceError(f"config path is not a directory: {current}")
            if index == last and info.st_uid != os.geteuid():
                raise PersistenceError(f"config directory is not owned by the current user: {current}")
            if index == last and info.st_mode & 0o022:
                raise PersistenceError(f"config directory is group/world writable: {current}")
        finally:
            os.close(fd)


def _open_checked(path, flags, mode=0o600, max_bytes=MAX_CONFIG_BYTES, missing_ok=False):
    try:
        fd = os.open(path, _flags(flags), mode)
    except OSError as exc:
        if missing_ok and exc.errno == errno.ENOENT:
            return None
        raise PersistenceError(f"cannot open {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PersistenceError(f"refusing non-regular config file: {path}")
        if info.st_uid != os.geteuid():
            raise PersistenceError(f"refusing config file owned by another user: {path}")
        if info.st_nlink != 1:
            raise PersistenceError(f"refusing hard-linked config file: {path}")
        if info.st_mode & 0o022:
            raise PersistenceError(f"refusing group/world-writable config file: {path}")
        if info.st_size > max_bytes:
            raise PersistenceError(f"refusing oversized config file: {path}")
        return fd
    except Exception:
        os.close(fd)
        raise


def _reject_constant(token):
    raise ValueError(token)


def _read_optional(path, limit=MAX_CONFIG_BYTES):
    fd = _open_checked(path, os.O_RDONLY | os.O_NONBLOCK, missing_ok=True)
    if fd is None:
        return b"", 0o600, False
    try:
        before = os.fstat(fd)
        chunks = []
        total = 0
        while total <= limit:
            chunk = os.read(fd, min(65536, limit - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise PersistenceError(f"refusing oversized config file: {path}")
            chunks.append(chunk)
        after = os.fstat(fd)
        if (before.st_dev != after.st_dev or before.st_ino != after.st_ino
                or before.st_size != after.st_size
                or getattr(before, "st_mtime_ns", int(before.st_mtime * 1000000000))
                != getattr(after, "st_mtime_ns", int(after.st_mtime * 1000000000))):
            raise PersistenceError(f"config file changed while reading: {path}")
        return b"".join(chunks), stat.S_IMODE(before.st_mode), True
    finally:
        os.close(fd)


def _read_config_snapshot(path):
    raw, mode, exists = _read_optional(path)
    if not exists:
        raise PersistenceError(f"shell.json not found: {path}")
    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PersistenceError(f"invalid shell.json: {path}") from exc
    if not isinstance(value, dict):
        raise PersistenceError("shell.json must contain an object")
    return value, mode


def _read_config(path):
    return _read_config_snapshot(path)[0]


def _atomic_write(path, data, mode=0o600):
    parent = os.path.dirname(path)
    _ensure_user_dir(parent)
    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.geteuid() or existing.st_nlink != 1:
            raise PersistenceError(f"refusing unsafe config target: {path}")
    temp_path = os.path.join(parent, "." + os.path.basename(path) + "." + secrets.token_hex(12) + ".tmp")
    fd = os.open(temp_path, _flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL), mode)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise PersistenceError("short config write")
            view = view[written:]
        os.fchmod(fd, mode)
        os.fsync(fd)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(fd)
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


def _secure_unlink(path):
    parent = os.path.dirname(path)
    _ensure_user_dir(parent)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    directory_fd = os.open(parent, _flags(os.O_RDONLY | os.O_DIRECTORY))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _transaction_journal_paths(config_path):
    root = os.path.dirname(config_path)
    return [
        os.path.join(root, JOURNAL_NAME),
        os.path.join(root, LEGACY_JOURNAL_NAME),
    ]


def _transaction_target_allowed(config_path, target):
    root = os.path.dirname(config_path)
    allowed = {
        config_path,
        config_path + BACKUP_SUFFIX,
        os.path.join(root, "shell.json"),
        os.path.join(root, "shell.toml"),
        os.path.join(root, "shell.json" + BACKUP_SUFFIX),
        os.path.join(root, "shell.toml" + BACKUP_SUFFIX),
    }
    if target in allowed:
        return True
    profile_root = os.path.join(root, "bar-profiles")
    if os.path.dirname(target) != profile_root:
        return False
    name = os.path.basename(target)
    if name.endswith(BACKUP_SUFFIX):
        name = name[:-len(BACKUP_SUFFIX)]
    return name.endswith(".json") and bool(PROFILE_RE.fullmatch(name[:-5]))


def _decode_journal_bytes(value, exists):
    if not exists:
        return b""
    if not isinstance(value, str) or len(value) > ((MAX_CONFIG_BYTES * 4) // 3 + 8):
        raise PersistenceError("invalid transaction journal")
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeError) as exc:
        raise PersistenceError("invalid transaction journal") from exc
    if len(raw) > MAX_CONFIG_BYTES:
        raise PersistenceError("invalid transaction journal")
    return raw


def _journal_operations(config_path, journal):
    if not os.path.lexists(journal):
        return []
    content, mode, exists = _read_optional(journal, MAX_JOURNAL_BYTES)
    if not exists:
        return []
    if mode & 0o077:
        raise PersistenceError("invalid transaction journal")
    try:
        data = json.loads(content.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise PersistenceError("invalid transaction journal") from exc
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("operations"), list):
        raise PersistenceError("invalid transaction journal")
    if len(data["operations"]) > 16:
        raise PersistenceError("invalid transaction journal")
    operations = []
    seen = set()
    reserved = set(_transaction_journal_paths(config_path))
    reserved.add(os.path.join(os.path.dirname(config_path), LOCK_NAME))
    for item in data["operations"]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise PersistenceError("invalid transaction journal")
        target = os.path.abspath(item["path"])
        if target in seen or target in reserved or not _transaction_target_allowed(config_path, target):
            raise PersistenceError("invalid transaction journal")
        seen.add(target)
        mode_value = item.get("mode")
        exists = item.get("exists")
        backup_exists = item.get("backup_exists")
        if (isinstance(mode_value, bool) or not isinstance(mode_value, int)
                or mode_value < 0 or mode_value > 0o7777
                or not isinstance(exists, bool) or not isinstance(backup_exists, bool)):
            raise PersistenceError("invalid transaction journal")
        operations.append({
            "path": target,
            "mode": mode_value,
            "exists": exists,
            "old": _decode_journal_bytes(item.get("old"), exists),
            "backup_exists": backup_exists,
            "backup": _decode_journal_bytes(item.get("backup"), backup_exists),
        })
    return operations


def _restore_operations(operations):
    for item in reversed(operations):
        target = item["path"]
        if item["exists"]:
            _atomic_write(target, item["old"], item["mode"])
        else:
            _secure_unlink(target)
        backup = target + BACKUP_SUFFIX
        if item["backup_exists"]:
            _atomic_write(backup, item["backup"], item["mode"])
        else:
            _secure_unlink(backup)


def recover_pending_transaction(config_path):
    recovered = False
    for journal in _transaction_journal_paths(config_path):
        operations = _journal_operations(config_path, journal)
        if operations:
            _restore_operations(operations)
            _secure_unlink(journal)
            recovered = True
        elif os.path.lexists(journal):
            _secure_unlink(journal)
    return recovered


def _make_journal(config_path, entries, journal):
    if os.path.lexists(journal):
        raise PersistenceError("transaction journal already exists")
    operations = []
    seen = set()
    for target, content in entries:
        target = os.path.abspath(target)
        if target in seen or not _transaction_target_allowed(config_path, target):
            raise PersistenceError("invalid transaction target")
        if not isinstance(content, bytes) or len(content) > MAX_CONFIG_BYTES:
            raise PersistenceError("invalid transaction content")
        seen.add(target)
        old, mode, exists = _read_optional(target)
        backup, _, backup_exists = _read_optional(target + BACKUP_SUFFIX)
        operations.append({
            "path": target,
            "mode": mode,
            "exists": exists,
            "old": old if exists else None,
            "backup_exists": backup_exists,
            "backup": backup if backup_exists else None,
        })
    payload_operations = []
    for item in operations:
        payload_operations.append({
            "path": item["path"],
            "mode": item["mode"],
            "exists": item["exists"],
            "old": base64.b64encode(item["old"]).decode("ascii") if item["exists"] else None,
            "backup_exists": item["backup_exists"],
            "backup": base64.b64encode(item["backup"]).decode("ascii") if item["backup_exists"] else None,
        })
    payload = json.dumps(
        {"version": 1, "operations": payload_operations},
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(payload) > MAX_JOURNAL_BYTES:
        raise PersistenceError("transaction journal is too large")
    _atomic_write(journal, payload, 0o600)
    return operations


def _write_transaction(config_path, payload):
    journal = _transaction_journal_paths(config_path)[0]
    operations = _make_journal(config_path, [(config_path, payload)], journal)
    prior = operations[0]
    try:
        old, mode, exists = _read_optional(config_path)
        if exists != prior["exists"] or old != prior["old"] or mode != prior["mode"]:
            raise PersistenceError("transaction source changed")
        _atomic_write(config_path, payload, mode)
        actual, actual_mode, actual_exists = _read_optional(config_path)
        if not actual_exists or actual != payload or actual_mode != mode:
            raise PersistenceError("write verification failed")
    except BaseException:
        try:
            _restore_operations(operations)
            _secure_unlink(journal)
        except BaseException:
            pass
        raise
    _secure_unlink(journal)


def _number(value, name, allow_empty=False):
    if allow_empty and value == "":
        return ""
    if isinstance(value, bool):
        raise PersistenceError(f"{name} must be a finite GHz value")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PersistenceError(f"{name} must be a finite GHz value") from exc
    if not math.isfinite(number) or number < MIN_GHZ or number > MAX_GHZ:
        raise PersistenceError(f"{name} must be finite and between 0.1 and 100 GHz")
    return f"{number:.1f}"


def _presets(value):
    if not isinstance(value, str):
        raise PersistenceError("presets must be a string")
    if len(value) > 512 or any(ord(char) < 32 for char in value):
        raise PersistenceError("presets is invalid")
    if not value.strip():
        return ""
    output = []
    for token in value.split(","):
        item = token.strip().lower()
        if not item:
            continue
        if item in ("base", "turbo", "max", "boost", "highest"):
            output.append("base" if item == "base" else "turbo")
            continue
        output.append(_number(item, "preset"))
    return ",".join(output)


def _safe_value(value, depth=0):
    if depth > 4:
        raise PersistenceError("settings value is too deeply nested")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > 2**63 - 1:
            raise PersistenceError("settings integer is too large")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PersistenceError("settings number must be finite")
        return value
    if isinstance(value, str):
        if len(value) > MAX_STRING or "\x00" in value:
            raise PersistenceError("settings string is invalid")
        return value
    if isinstance(value, list):
        if len(value) > MAX_COLLECTION:
            raise PersistenceError("settings list is too large")
        return [_safe_value(item, depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > MAX_COLLECTION:
            raise PersistenceError("settings object is too large")
        output = {}
        for key, item in value.items():
            if not isinstance(key, str) or not KEY_RE.fullmatch(key):
                raise PersistenceError("settings key is invalid")
            output[key] = _safe_value(item, depth + 1)
        return output
    raise PersistenceError("settings value has an unsupported type")


def validate_changes(changes):
    if not isinstance(changes, dict):
        raise PersistenceError("settings payload must be an object")
    output = {}
    plugin_id = changes.get("id")
    if plugin_id is not None and plugin_id != PLUGIN_ID:
        raise PersistenceError("plugin id does not match")
    for key, value in changes.items():
        if key == "id":
            continue
        if key == "maxGHz":
            output[key] = "max" if isinstance(value, str) and value.strip().lower() == "max" else _number(value, key)
        elif key in ("capMax", "baseGHz"):
            if value is None:
                output[key] = ""
            elif isinstance(value, str) and not value.strip():
                output[key] = ""
            else:
                output[key] = _number(value, key)
        elif key == "presets":
            output[key] = _presets(value)
        else:
            output[key] = _safe_value(value)
    return output


def _update_entries(data, changes):
    found = False
    bar = data.get("bar")
    layout = bar.get("layout") if isinstance(bar, dict) else None
    if not isinstance(layout, dict):
        raise PersistenceError("shell.json has no bar layout")
    for region in ("left", "center", "right"):
        entries = layout.get(region)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and entry.get("id") == PLUGIN_ID:
                entry.update(changes)
                found = True
    if not found:
        raise PersistenceError(f"{PLUGIN_ID} not found in bar layout")


def persist_changes(changes, config_path=None):
    path = os.path.abspath(config_path or CONFIG_PATH)
    _ensure_user_dir(os.path.dirname(path))
    lock_path = os.path.join(os.path.dirname(path), LOCK_NAME)
    lock_fd = _open_checked(lock_path, os.O_RDWR | os.O_CREAT, 0o600, 4096)
    try:
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        recover_pending_transaction(path)
        data, _ = _read_config_snapshot(path)
        _update_entries(data, validate_changes(changes))
        payload = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        if len(payload) > MAX_CONFIG_BYTES:
            raise PersistenceError("updated shell.json exceeds maximum size")
        _write_transaction(path, payload)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
    return True


def main():
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        print("usage: persist.py '<json-object>'", file=sys.stderr)
        return 2
    if len(sys.argv[1].encode("utf-8")) > MAX_INPUT_BYTES:
        print("persist.py: settings payload is too large", file=sys.stderr)
        return 1
    try:
        changes = json.loads(sys.argv[1], parse_constant=_reject_constant)
        persist_changes(changes)
    except (OSError, PersistenceError, json.JSONDecodeError, ValueError) as exc:
        print(f"persist.py: {exc}", file=sys.stderr)
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
