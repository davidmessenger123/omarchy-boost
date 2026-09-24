# Boost

An Omarchy bar widget that lets you cap your CPU's max boost clock on the fly.

- **Left click** opens a panel with a continuous slider (1.0 GHz up to the
  CPU's own reported max boost) and preset chips: **BASE**, evenly-spaced
  steps up to **MAX**.
- **Right click** cycles those presets without opening the panel.
- **Middle click** re-syncs the readout with sysfs.
- The bar button shows the live cap (and package temperature in the tooltip).

Changes apply to every online core immediately, persist in the widget's
shell.json entry after the privileged helper succeeds, and are re-applied at
boot by the `cpu-cap-boost.service` systemd unit. The boot helper reads only
its validated, root-owned state file; an absent or malformed file fails closed.

## Install

```sh
omarchy plugin add https://github.com/davidmessenger123/omarchy-boost.git --enable
```

The widget needs passwordless access to one separately installed, root-owned
helper (to write `scaling_max_freq`) and a boot service (to re-apply the cap
on reboot). The installer verifies one opened snapshot of the helper against a
pinned SHA-256 digest before writing it, then writes a narrowly scoped polkit
rule and the service, deriving the target user automatically:

```sh
sudo python3 ~/.config/omarchy/plugins/davidjm.boost/setup.py
```

(It is idempotent — safe to re-run. Use `--dry-run` to see what it writes
first.) To remove the root integration and its persisted state, run:

```sh
sudo python3 ~/.config/omarchy/plugins/davidjm.boost/setup.py --uninstall
```

Finally add it to the bar:

```sh
omarchy bar put davidjm.boost --section right
```

## How it works

| File | Purpose |
| --- | --- |
| `Boost.qml` | The bar widget: readout, slider panel, presets, popup |
| `boostctl.py` | Unprivileged read-only monitor: `get` sysfs and sensor state |
| `boostset.py` | Root-owned fixed-path helper: validates and applies `set`/`apply` |
| `persist.py` | Validated, transactionally locked writer for the widget's shell.json entry |
| `setup.py` | Installs/removes the root helper, polkit rule, service, and state |
| `tests/test_boost.py` | Standard-library tests for privilege boundaries, validation, persistence, and cleanup primitives |
| `/var/lib/omarchy-boost/maxboost` | Root-owned last-applied cap, written only after a successful apply |

`persist.py` updates only `davidjm.boost` entries after re-reading the latest
configuration under the shared `.shell.json.lock`. Its shared transaction
journal restores the prior file and mode after an interrupted write.

`boostset.py` writes only fixed
`/sys/devices/system/cpu/cpu*/cpufreq/scaling_max_freq` controls and the
fixed `/var/lib/omarchy-boost/maxboost` state file. It rejects extra
arguments, non-finite/out-of-range caps, symlinked or foreign state files, and
serializes sysfs application with root-state persistence under one lock. It
snapshots every target's prior cap and restores those values if any write,
readback, or persistence step fails. The widget elevates this separate helper
through `pkexec`; the unprivileged `boostctl.py get` path is never authorized
by the polkit rule.

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

`setup.py` installs these root-owned artifacts with the target user already
filled in:

1. The separate helper at
   `/usr/local/libexec/omarchy-boost/boostset.py` and its private state
   directory `/var/lib/omarchy-boost/`.
2. The passwordless polkit rule, restricted to that installed helper:

   ```js
   // /etc/polkit-1/rules.d/50-davidjm-boost.rules
   polkit.addRule(function (action, subject) {
     if (action.id == "org.freedesktop.policykit.exec" &&
         subject.local &&
         subject.user == "YOURUSER" &&
         action.lookup("program") ==
           "/usr/local/libexec/omarchy-boost/boostset.py") {
       return polkit.Result.YES;
     }
   });
   ```

3. The boot-persistence systemd unit:

   ```ini
   # /etc/systemd/system/cpu-cap-boost.service
   [Unit]
   Description=Apply persisted CPU max-boost cap
   After=multi-user.target
   ConditionPathExists=/var/lib/omarchy-boost/maxboost

   [Service]
   Type=oneshot
   ExecStart=/usr/bin/python3 -I /usr/local/libexec/omarchy-boost/boostset.py apply
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
   ```

   and enables it (`systemctl enable cpu-cap-boost.service` after a
   `daemon-reload`). `--uninstall` disables/removes the service, rule, helper,
   lock file, and state directory without following symlinks.

   The polkit rule never authorizes the user-writable plugin directory. The
   only root-writable application paths are fixed sysfs controls and the fixed
   root-owned state file.

## Notes

- The cap applies per-core; cores keep their own turbo ceilings (e.g. some at
  4.9 GHz, others 5.0 GHz), so "MAX" restores each core's native maximum.
- `/var/lib/omarchy-boost/maxboost` holds the last successfully applied value
  (a number, or `max`); an invalid or missing file makes the boot unit fail
  closed instead of choosing a fallback cap.
- Toggling the Omarchy power profile to Balanced/Performance does not fight the
  slider: `scaling_max_freq` is the harder limit and HWP honors the lower of
  the two.
- The slider range is derived from the running CPU's reported ceiling, with a
  1.0 GHz lower bound.
