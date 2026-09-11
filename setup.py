#!/usr/bin/env python3
"""One-shot root setup for davidjm.boost.

Generates the two system files the widget needs:

  * /etc/polkit-1/rules.d/50-davidjm-boost.rules  -- passwordless `pkexec`
    elevation for this plugin's boostctl.py (and only that script)
  * /etc/systemd/system/cpu-cap-boost.service     -- re-applies the persisted
    boost cap at boot

Both paths are derived from the real install location and the invoking user,
so there are no placeholders to fill in. Run once after the plugin is added:

    sudo python3 ~/.config/omarchy/plugins/davidjm.boost/setup.py

Add --dry-run to print what would be written without touching anything.
"""

import argparse
import os
import subprocess
import sys

POLKIT_RULE = "/etc/polkit-1/rules.d/50-davidjm-boost.rules"
SYSTEMD_UNIT = "/etc/systemd/system/cpu-cap-boost.service"


def real_user():
    return os.environ.get("SUDO_USER") or os.environ.get("USER")


def polkit_rule(user, boostctl):
    return f"""polkit.addRule(function (action, subject) {{
  if (action.id == "org.freedesktop.policykit.exec" &&
      subject.user == "{user}" &&
      action.lookup("program") == "{boostctl}") {{
    return polkit.Result.YES;
  }}
}});
"""


def systemd_unit(boostctl):
    return f"""[Unit]
Description=Apply persisted CPU max-boost cap
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {boostctl} apply
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
        epilog="Run with sudo so the polkit rule and systemd unit can be installed.",
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

    boostctl = os.path.join(plugin_dir, "boostctl.py")
    rule = polkit_rule(user, boostctl)
    unit = systemd_unit(boostctl)

    print(f"target user : {user}")
    print(f"plugin dir  : {plugin_dir}")

    if args.dry_run:
        print(f"\n===== would write {POLKIT_RULE} =====")
        print(rule, end="")
        print(f"===== would write {SYSTEMD_UNIT} =====")
        print(unit, end="")
        return

    if not os.path.isfile(boostctl):
        print(f"boostctl.py not found at {boostctl}", file=sys.stderr)
        sys.exit(1)

    write(POLKIT_RULE, rule)
    write(SYSTEMD_UNIT, unit)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "cpu-cap-boost.service"], check=True)
    print("\nDone. Add the widget to the bar with:")
    print("  omarchy bar put davidjm.boost --section right")


if __name__ == "__main__":
    main()