# Boost

An Omarchy bar widget that lets you cap your CPU's max boost clock on the fly.

- **Left click** opens a panel with a continuous slider (1.0 GHz up to the
  CPU's own reported max boost) and preset chips: **BASE**, evenly-spaced
  steps up to **MAX**.
- **Right click** cycles those presets without opening the panel.
- **Middle click** re-syncs the readout with sysfs.
- The bar button shows the live cap (and package temperature in the tooltip).

Changes apply to every online core immediately, persist in the widget's
shell.json entry, and are re-applied at boot by the `cpu-cap-boost.service`
systemd unit.

## Install

```sh
omarchy plugin add https://github.com/davidmessenger123/omarchy-boost.git --enable
```

The widget needs passwordless root elevation (to write
`scaling_max_freq`) and a boot service (to re-apply the cap on reboot). A
small installer writes both for you, deriving your real user and plugin
path automatically:

```sh
sudo python3 ~/.config/omarchy/plugins/davidjm.boost/setup.py
```

(It is idempotent — safe to re-run. Use `--dry-run` to see what it writes
first.)

Finally add it to the bar:

```sh
omarchy bar put davidjm.boost --section right
```

## How it works

| File | Purpose |
| --- | --- |
| `Boost.qml` | The bar widget: readout, slider panel, presets, popup |
| `boostctl.py` | Helper: `get` (read sysfs/sensors) and `set`/`apply` (write `scaling_max_freq`) |
| `persist.py` | Persists `maxGHz` into shell.json and mirrors it to `maxboost` |
| `maxboost` | The Last-applied cap, read back by the boot service |

`set`/`apply` write `/sys/devices/system/cpu/cpu*/cpufreq/scaling_max_freq`,
which needs root. The widget elevates **only this script** through `pkexec`;
a polkit rule grants it passwordless access, so dragging the slider never
prompts.

Works on both Intel and AMD x86: with `amd-pstate` the cap is applied as a
CPPC max-performance limit (kernels ≥ 6.5), and `acpi-cpufreq` honours the
write directly. The base figure comes from `base_frequency` (Intel / amd-pstate
≥ 6.5), falling back to the ACPI CPPC `nominal_freq`; temperature is read from
`coretemp` on Intel and `k10temp` (Tctl) on AMD.

There is **no CPU lookup table** — everything is read live from the running
system, so effectively any x86 chip works out of the box:

| Reading | Source |
| --- | --- |
| model name / cores / threads | `/proc/cpuinfo` (shown in the tooltip and panel) |
| full-turbo ("MAX") | `cpuinfo_max_freq` per core |
| base clock ("BASE") | `base_frequency`, else CPPC `nominal_freq` |
| package temperature | `coretemp` (Intel) / `k10temp` Tctl (AMD) |

The slider, the presets, and `applyValue` never allow a cap above the CPU's
reported full-turbo, and `boostctl.py` enforces the same ceiling per core when
it writes `scaling_max_freq` — so neither the widget, a manual `set`, nor the
boot service can ever exceed what the CPU reports.

### Presets (`presets`)

Preset chips and the right-click cycle default to **auto**: BASE uses the
reported base clock, MAX the reported max boost, and the in-between chips are
evenly spaced across that range — a 3.8–4.7 GHz chip gets 3.8 / 4.0 / 4.3 /
4.5 / 4.7, while a 2.6–5.0 GHz chip gets 2.6 / 3.2 / 3.8 / 4.4 / 5.0. The steps
are re-derived from each CPU as it loads, so the buttons always sit inside the
chip's own range instead of falling below BASE. Set a custom comma-separated
list (in the popup or the settings schema) to override; blank = auto.

## Manual max ceiling (`capMax`)

A CPU's **rated** max boost (e.g. 4.7 GHz on a Ryzen 7 5800X) can read higher
in sysfs — PBO/BIOS may expose `cpuinfo_max_freq` as 5.0. The widget trusts the
chip, so "MAX", the slider, and the current cap then follow that higher figure.

If you want "MAX" to mean your own number instead, set the `capMax` setting to
a fixed GHz value (column "Manual max boost (GHz)"; a "Manual max" field in the
popup, blank = from CPU). `maxGHz`/`presets` live in the same settings
location. Example:

```json
"davidjm.boost": {
  "id": "davidjm.boost",
  "maxGHz": "4.7",
  "presets": "base,3.0,3.5,4.0,max",
  "capMax": "4.7"
}
```

The manual ceiling is still capped at the chip's sysfs max (so it can never be
set *above* what the hardware allows), and applying MAX with `capMax` set writes
exactly that value — the "CURRENT CAP" readout then shows 4.7, not 5.0.

## Manual base clock (`baseGHz`)

The base-clock line and the BASE preset detect the base from `base_frequency`
(Intel / amd-pstate ≥ 6.5) or ACPI CPPC `nominal_freq`. On `acpi-cpufreq` Ryzen
without CPPC there is no sysfs base at all; the panel then shows "Base –". Set a
fixed value in the popup's "Base clock (GHz)" field (blank = auto-detect) to
show and cap BASE at your CPU's real base clock, e.g. 3.8 on a 5800X:

```json
"davidjm.boost": {
  "id": "davidjm.boost",
  "maxGHz": "4.7",
  "presets": "base,3.0,3.5,4.0,max",
  "capMax": "4.7",
  "baseGHz": "3.8"
}
```

## What the installer generates (reference)

`setup.py` writes these two files with the placeholders already filled in
from your real user and plugin path:

1. The passwordless polkit rule:

   ```js
   // /etc/polkit-1/rules.d/50-davidjm-boost.rules
   polkit.addRule(function (action, subject) {
     if (action.id == "org.freedesktop.policykit.exec" &&
         subject.user == "YOURUSER" &&
         action.lookup("program") ==
           "/home/YOURUSER/.config/omarchy/plugins/davidjm.boost/boostctl.py") {
       return polkit.Result.YES;
     }
   });
   ```

2. The boot-persistence systemd unit:

   ```ini
   # /etc/systemd/system/cpu-cap-boost.service
   [Unit]
   Description=Apply persisted CPU max-boost cap
   After=multi-user.target

   [Service]
   Type=oneshot
   ExecStart=/usr/bin/python3 /home/YOURUSER/.config/omarchy/plugins/davidjm.boost/boostctl.py apply
   RemainAfterExit=yes

   [Install]
   WantedBy=multi-user.target
   ```

   and enables it (`systemctl enable cpu-cap-boost.service` after a
   `daemon-reload`).

   The polkit rule scopes passwordless `pkexec` to **only** this plugin's
   `boostctl.py`, so nothing else on the system gains rights.

## Notes

- The cap applies per-core; cores keep their own turbo ceilings (e.g. some at
  4.9 GHz, others 5.0 GHz), so "MAX" restores each core's native maximum.
- The `maxboost` file holds the last value written (a number, or `max`).
- Toggling the Omarchy power profile to Balanced/Performance does not fight the
  slider: `scaling_max_freq` is the harder limit and HWP honors the lower of
  the two.
- This plugin is intentionally laptop-tuned (i9-11950H): slider range 1.0–5.0
  GHz, BASE = the chip's `base_frequency`.