# Boost

An Omarchy bar widget that lets you cap — or lift — your CPU's max boost
clock on the fly.

- **Left click** opens a panel with a continuous slider (1.0–5.0 GHz) and
  preset chips: **BASE**, **3.0**, **3.5**, **4.0**, **MAX**.
- **Right click** cycles those presets without opening the panel.
- **Middle click** re-syncs the readout with sysfs.
- The bar button shows the live cap (and package temperature in the tooltip).

Changes apply to every online core immediately, persist in the widget's
shell.json entry, and are re-applied at boot by the `cpu-cap-boost.service`
systemd unit.

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

## One-time setup (needs root)

1. Install the polkit rule:

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

2. Point `cpu-cap-boost.service` at the persisted value:

   ```ini
   # /etc/systemd/system/cpu-cap-boost.service
   [Unit]
   Description=Apply persisted CPU max-boost cap
   After=multi-user.target

   [Service]
   Type=oneshot
   ExecStart=/home/YOURUSER/.config/omarchy/plugins/davidjm.boost/boostctl.py apply
   RemainAfterExit=yes

   [Install]
   WantedBy=multi-user.target
   ```

   then `systemctl daemon-reload && systemctl enable cpu-cap-boost.service`.

3. Add to the bar: `omarchy bar put davidjm.boost --section right`.

## Notes

- The cap applies per-core; cores keep their own turbo ceilings (e.g. some at
  4.9 GHz, others 5.0 GHz), so "MAX" restores each core's native maximum.
- The `maxboost` file holds the last value written (a number, or `max`).
- Toggling the Omarchy power profile to Balanced/Performance does not fight the
  slider: `scaling_max_freq` is the harder limit and HWP honors the lower of
  the two.
- This plugin is intentionally laptop-tuned (i9-11950H): slider range 1.0–5.0
  GHz, BASE = the chip's `base_frequency`.