#!/usr/bin/env python3
"""Persist davidjm.boost settings into shell.json and mirror the max GHz cap
to a plain `maxboost` file that cpu-cap-boost.service reads back at boot.

Widget parameters live as FLAT keys on the shell.json bar entry (like stock
widgets), not under a nested `settings` object:

    { "id": "davidjm.boost", "maxGHz": "3.0" }

The shell hot-patches the running widget on save (atomic writes only).

Usage:
    python3 persist.py '{"maxGHz": "3.5"}'   # merge these keys
"""

import json
import os
import sys

PLUGIN_ID = "davidjm.boost"
CONFIG_PATH = os.path.expanduser("~/.config/omarchy/shell.json")
STORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maxboost")


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        return
    changes = json.loads(sys.argv[1])
    changes.pop("id", None)

    if "maxGHz" in changes:
        with open(STORE_PATH, "w", encoding="utf-8") as fh:
            fh.write(str(changes["maxGHz"]) + "\n")

    with open(CONFIG_PATH, encoding="utf-8") as fh:
        data = json.load(fh)

    found = False

    def update(region: str) -> None:
        nonlocal found
        for entry in data.get("bar", {}).get("layout", {}).get(region, []):
            if isinstance(entry, dict) and entry.get("id") == PLUGIN_ID:
                entry.update(changes)
                found = True

    for region in ("left", "center", "right"):
        update(region)

    if not found:
        print(f"{PLUGIN_ID} not found in bar layout", file=sys.stderr)
        sys.exit(1)

    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, CONFIG_PATH)
    print("ok")


if __name__ == "__main__":
    main()