#!/usr/bin/env python3
"""Install or remove the root-owned Boost helper, polkit rule, and boot unit."""

import argparse
import errno
import hashlib
import json
import os
import pwd
import re
import secrets
import stat
import subprocess
import sys

POLKIT_RULE = "/etc/polkit-1/rules.d/50-davidjm-boost.rules"
SYSTEMD_UNIT = "/etc/systemd/system/cpu-cap-boost.service"
HELPER_DIR = "/usr/local/libexec/omarchy-boost"
HELPER_PATH = os.path.join(HELPER_DIR, "boostset.py")
STATE_DIR = "/var/lib/omarchy-boost"
STATE_PATH = os.path.join(STATE_DIR, "maxboost")
MAX_SOURCE_BYTES = 1 << 20
HELPER_SHA256 = "61f92f139101544ce50af9cf5eb128a172d471cf4bb90cc59638154cb1ef841c"
USER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.-]{0,31}$")


def real_user():
    candidate = os.environ.get("SUDO_USER") or os.environ.get("USER")
    if not candidate or candidate == "root" or not USER_RE.fullmatch(candidate):
        return None
    try:
        account = pwd.getpwnam(candidate)
    except KeyError:
        return None
    if account.pw_uid == 0:
        return None
    return account.pw_name


def _flags(value):
    return value | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _ensure_root_dir(path, mode=0o755):
    path = os.path.abspath(path)
    parts = [part for part in path.split(os.sep) if part]
    current = os.path.sep
    for part in parts:
        current = os.path.join(current, part)
        try:
            os.mkdir(current, mode)
        except FileExistsError:
            pass
        except OSError as exc:
            raise RuntimeError(f"cannot create directory {current}: {exc}") from exc
        try:
            fd = os.open(current, _flags(os.O_RDONLY | os.O_DIRECTORY))
        except OSError as exc:
            raise RuntimeError(f"cannot open directory {current}: {exc}") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise RuntimeError(f"unsafe root directory: {current}")
        finally:
            os.close(fd)


def _verify_root_dir(path):
    path = os.path.abspath(path)
    parts = [part for part in path.split(os.sep) if part]
    current = os.path.sep
    last = len(parts) - 1
    for index, part in enumerate(parts):
        current = os.path.join(current, part)
        try:
            fd = os.open(current, _flags(os.O_RDONLY | os.O_DIRECTORY))
        except OSError as exc:
            raise RuntimeError(f"cannot verify directory {current}: {exc}") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or (index == last and info.st_mode & 0o022):
                raise RuntimeError(f"unsafe root directory: {current}")
        finally:
            os.close(fd)


def _verify_user_dir(path, uid):
    path = os.path.abspath(path)
    parts = [part for part in path.split(os.sep) if part]
    current = os.path.sep
    last = len(parts) - 1
    for index, part in enumerate(parts):
        current = os.path.join(current, part)
        try:
            fd = os.open(current, _flags(os.O_RDONLY | os.O_DIRECTORY))
        except OSError as exc:
            raise RuntimeError(f"cannot verify directory {current}: {exc}") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode) or (index == last and (info.st_uid != uid or info.st_mode & 0o022)):
                raise RuntimeError(f"unsafe user directory: {current}")
        finally:
            os.close(fd)


def _check_target(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_size > MAX_SOURCE_BYTES:
        raise RuntimeError(f"refusing unsafe existing file: {path}")
    return stat.S_IMODE(info.st_mode)


def write(path, content, mode=0o644):
    parent = os.path.dirname(path)
    _ensure_root_dir(parent)
    _check_target(path)
    data = content.encode("utf-8")
    if len(data) > MAX_SOURCE_BYTES:
        raise RuntimeError(f"content is too large: {path}")
    temp_path = os.path.join(parent, "." + os.path.basename(path) + "." + secrets.token_hex(12) + ".tmp")
    fd = os.open(temp_path, _flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL), mode)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise RuntimeError(f"short write: {path}")
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
    print(f"wrote {path}")


def _read_source(path):
    try:
        fd = os.open(path, _flags(os.O_RDONLY | os.O_NONBLOCK))
    except OSError as exc:
        raise RuntimeError(f"cannot read helper source {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
            raise RuntimeError(f"unsafe helper source: {path}")
        chunks = []
        total = 0
        while total <= MAX_SOURCE_BYTES:
            chunk = os.read(fd, min(65536, MAX_SOURCE_BYTES - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_SOURCE_BYTES:
                raise RuntimeError(f"oversized helper source: {path}")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_helper_source(path):
    data = _read_source(path)
    digest = hashlib.sha256(data).hexdigest()
    if digest != HELPER_SHA256:
        raise RuntimeError(f"helper source digest mismatch: {digest}")
    return data


def install_helper(data):
    write(HELPER_PATH, data.decode("utf-8"), 0o755)


def polkit_rule(user, helper=HELPER_PATH):
    user_value = json.dumps(user)
    helper_value = json.dumps(helper)
    return f"""polkit.addRule(function (action, subject) {{
  if (action.id == "org.freedesktop.policykit.exec" &&
      subject.local &&
      subject.user == {user_value} &&
      action.lookup("program") == {helper_value}) {{
    return polkit.Result.YES;
  }}
}});
"""


def systemd_unit(helper=HELPER_PATH):
    helper_value = helper.replace("\\", "\\\\").replace(" ", "\\x20")
    return f"""[Unit]
Description=Apply persisted CPU max-boost cap
After=multi-user.target
ConditionPathExists={STATE_DIR}/maxboost

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I {helper_value} apply
RemainAfterExit=yes
UMask=0077
NoNewPrivileges=yes
PrivateDevices=yes
PrivateTmp=yes
PrivateNetwork=yes
ProtectSystem=strict
ReadWritePaths=-/sys/devices/system/cpu -/var/lib/omarchy-boost
ProtectHome=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectKernelModules=yes
ProtectHostname=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
RestrictAddressFamilies=AF_UNIX
CapabilityBoundingSet=
AmbientCapabilities=
SystemCallFilter=@system-service
SystemCallFilter=~@mount @module @raw-io @reboot @swap @debug

[Install]
WantedBy=multi-user.target
"""


def _remove_file(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        print(f"not present {path}")
        return
    _verify_root_dir(os.path.dirname(path))
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_size > MAX_SOURCE_BYTES:
        raise RuntimeError(f"refusing unsafe cleanup target: {path}")
    os.unlink(path)
    print(f"removed {path}")


def _cleanup_state():
    try:
        info = os.lstat(STATE_DIR)
    except FileNotFoundError:
        return
    _verify_root_dir(STATE_DIR)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError(f"refusing unsafe state directory: {STATE_DIR}")
    for path in (STATE_PATH, STATE_PATH + ".lock"):
        _remove_file(path)
    try:
        os.rmdir(STATE_DIR)
        print(f"removed {STATE_DIR}")
    except OSError as exc:
        if exc.errno != errno.ENOTEMPTY:
            raise


def _cleanup_legacy_store():
    user = real_user()
    if not user:
        return
    account = pwd.getpwnam(user)
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    _verify_user_dir(plugin_dir, account.pw_uid)
    for name in ("maxboost", "maxboost.lock"):
        path = os.path.join(plugin_dir, name)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_uid != account.pw_uid or info.st_nlink != 1 or info.st_size > MAX_SOURCE_BYTES:
            raise RuntimeError(f"refusing unsafe legacy cleanup target: {path}")
        os.unlink(path)
        print(f"removed {path}")


def uninstall():
    for command in (["systemctl", "disable", "--now", "cpu-cap-boost.service"], ["systemctl", "daemon-reload"]):
        subprocess.run(command, check=False)
    _remove_file(POLKIT_RULE)
    _remove_file(SYSTEMD_UNIT)
    _remove_file(HELPER_PATH)
    try:
        if os.path.lexists(HELPER_DIR):
            _verify_root_dir(HELPER_DIR)
            os.rmdir(HELPER_DIR)
    except FileNotFoundError:
        pass
    except OSError as exc:
        if exc.errno != errno.ENOTEMPTY:
            raise
    _cleanup_state()
    _cleanup_legacy_store()
    subprocess.run(["systemctl", "daemon-reload"], check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    if not args.dry_run and os.geteuid() != 0:
        print("Run as root: sudo python3 setup.py", file=sys.stderr)
        return 1
    if args.uninstall:
        if args.dry_run:
            print("would disable and remove the Boost polkit rule, systemd unit, helper, and state")
            return 0
        try:
            uninstall()
        except (OSError, RuntimeError) as exc:
            print(f"setup.py: {exc}", file=sys.stderr)
            return 1
        print("Boost root integration removed")
        return 0
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    user = real_user()
    if not user:
        print("Could not determine a non-root target user; run with sudo from the user session", file=sys.stderr)
        return 1
    source = os.path.join(plugin_dir, "boostset.py")
    if os.path.islink(source):
        print(f"helper source not found or unsafe: {source}", file=sys.stderr)
        return 1
    try:
        helper_source = read_helper_source(source)
    except (OSError, RuntimeError, UnicodeError) as exc:
        print(f"setup.py: {exc}", file=sys.stderr)
        return 1
    rule = polkit_rule(user)
    unit = systemd_unit()
    digest = hashlib.sha256(helper_source).hexdigest()
    print(f"target user : {user}")
    print(f"helper      : {HELPER_PATH}")
    print(f"sha256      : {digest}")
    if args.dry_run:
        print(f"\n===== would write {POLKIT_RULE} =====")
        print(rule, end="")
        print(f"===== would write {SYSTEMD_UNIT} =====")
        print(unit, end="")
        print(f"===== would install {HELPER_PATH} and create {STATE_DIR} =====")
        return 0
    try:
        _ensure_root_dir(HELPER_DIR)
        _ensure_root_dir(STATE_DIR, 0o700)
        install_helper(helper_source)
        write(POLKIT_RULE, rule)
        write(SYSTEMD_UNIT, unit)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", "cpu-cap-boost.service"], check=True)
    except (OSError, RuntimeError, UnicodeError, subprocess.CalledProcessError) as exc:
        print(f"setup.py: {exc}", file=sys.stderr)
        return 1
    print("Done. Add the widget to the bar with:")
    print("  omarchy bar put davidjm.boost --section right")
    return 0


if __name__ == "__main__":
    sys.exit(main())
