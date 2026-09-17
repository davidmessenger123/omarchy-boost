#!/usr/bin/env python3
"""One-shot root setup for davidjm.boost.

Installs a root-owned copy of boostctl.py and generates the two system files
the widget needs:

  * /usr/libexec/davidjm-boost/boostctl.py  -- root-owned helper (the plugin
    copy under ~/.config is user-writable, so polkit must never grant it
    passwordless execution)
  * /etc/polkit-1/rules.d/50-davidjm-boost.rules  -- passwordless `pkexec`
    elevation for the *installed* helper (and only that script)
  * /etc/systemd/system/cpu-cap-boost.service     -- re-applies the persisted
    boost cap at boot, reading maxboost from this plugin's dir via
    --persist-dir

Install paths are derived from the real install location and the invoking
user, so there are no placeholders to fill in. Run once after the plugin is
added (and again after an update to refresh the installed helper):

    sudo python3 ~/.config/omarchy/plugins/davidjm.boost/setup.py

Add --dry-run to print what would be written without touching anything.
"""

import argparse
import os
import shutil
import subprocess
import sys

POLKIT_RULE = "/etc/polkit-1/rules.d/50-davidjm-boost.rules"
SYSTEMD_UNIT = "/etc/systemd/system/cpu-cap-boost.service"
INSTALL_BIN = "/usr/libexec/davidjm-boost/boostctl.py"


def real_user():
    return os.environ.get("SUDO_USER") or os.environ.get("USER")


def js_escape(value):
    """Keep interpolated paths safe inside the polkit JS string literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def polkit_rule(user, boostctl):
    user = f'"{js_escape(user)}"'
    boostctl = f'"{js_escape(boostctl)}"'
    return f"""polkit.addRule(function (action, subject) {{
  if (action.id == "org.freedesktop.policykit.exec" &&
      subject.user == {user} &&
      action.lookup("program") == {boostctl}) {{
    return polkit.Result.YES;
  }}
}});
"""


def systemd_unit(boostctl, persist_dir):
    return f"""[Unit]
Description=Apply persisted CPU max-boost cap
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {boostctl} apply --persist-dir {persist_dir}
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""


def write(path, content):
    tmp = f"{path}.new"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    print(f"wrote {path}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.strip().splitlines()[0],
        epilog="Run with sudo so the polkit rule, systemd unit, and installed helper can be written.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be written without touching the system",
    )
    args = ap.parse_args()

    if os.geteuid() != 0 and not args.dry_run:
        print(
            "Run as root:  sudo python3 ~/.config/omarchy/plugins/davidjm.boost/setup.py",
            file=sys.stderr,
        )
        sys.exit(1)

    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    user = real_user()
    if not user or user == "root":
        print(
            "Could not determine the target user. Run with `sudo` from your normal "
            "user session, not as a root shell.",
            file=sys.stderr,
        )
        sys.exit(1)

    src = os.path.join(plugin_dir, "boostctl.py")
    if not os.path.isfile(src):
        print(f"boostctl.py not found at {src}", file=sys.stderr)
        sys.exit(1)

    rule = polkit_rule(user, INSTALL_BIN)
    unit = systemd_unit(INSTALL_BIN, plugin_dir)

    print(f"target user    : {user}")
    print(f"plugin dir     : {plugin_dir}")
    print(f"installed copy : {INSTALL_BIN}")

    if args.dry_run:
        print(f"\n===== would INSTALL {INSTALL_BIN} =====")
        print("(copy of boostctl.py, chmod 0755)")
        print(f"\n===== would write {POLKIT_RULE} =====")
        print(rule, end="")
        print(f"===== would write {SYSTEMD_UNIT} =====")
        print(unit, end="")
        return

    os.makedirs(os.path.dirname(INSTALL_BIN), exist_ok=True)
    shutil.copy2(src, INSTALL_BIN)
    os.chmod(INSTALL_BIN, 0o755)
    print(f"installed {INSTALL_BIN}")

    write(POLKIT_RULE, rule)
    write(SYSTEMD_UNIT, unit)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "cpu-cap-boost.service"], check=True)
    print("\nDone. Add the widget to the bar with:")
    print("  omarchy bar put davidjm.boost --section right")


if __name__ == "__main__":
    main()