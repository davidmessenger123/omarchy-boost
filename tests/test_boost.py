import copy
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))

import boostctl
import boostset
import persist
import setup


class BoostSecurityTests(unittest.TestCase):
    def test_cap_parser_rejects_nonfinite_and_out_of_range(self):
        self.assertEqual(boostset.parse_cap("max"), "max")
        self.assertEqual(boostset.parse_cap("3.25"), 3.2)
        for value in ("nan", "inf", "-inf", "0", "100.1", "3junk"):
            with self.subTest(value=value):
                with self.assertRaises(boostset.ControlError):
                    boostset.parse_cap(value)

    def test_state_round_trip_and_invalid_state_fails_closed(self):
        old_uid = boostset.STATE_OWNER_UID
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state", "maxboost")
            boostset.STATE_OWNER_UID = os.geteuid()
            try:
                boostset._write_persisted("3.4", path)
                self.assertEqual(boostset._read_persisted(path), 3.4)
                with open(path, "w", encoding="ascii") as handle:
                    handle.write("nan\n")
                with self.assertRaises(boostset.ControlError):
                    boostset._read_persisted(path)
            finally:
                boostset.STATE_OWNER_UID = old_uid

    def test_state_rejects_symlink_and_hardlink(self):
        old_uid = boostset.STATE_OWNER_UID
        with tempfile.TemporaryDirectory() as directory:
            state_dir = os.path.join(directory, "state")
            os.mkdir(state_dir, 0o700)
            path = os.path.join(state_dir, "maxboost")
            victim = os.path.join(directory, "victim")
            with open(victim, "w", encoding="ascii") as handle:
                handle.write("3.0\n")
            os.symlink(victim, path)
            boostset.STATE_OWNER_UID = os.geteuid()
            try:
                with self.assertRaises(boostset.ControlError):
                    boostset._write_persisted("3.5", path)
                with open(victim, encoding="ascii") as handle:
                    self.assertEqual(handle.read(), "3.0\n")
                os.unlink(path)
                os.link(victim, path)
                with self.assertRaises(boostset.ControlError):
                    boostset._read_persisted(path)
            finally:
                boostset.STATE_OWNER_UID = old_uid

    def test_cap_all_ignores_offline_cpus(self):
        old_root = boostset.CPU_ROOT
        old_write = boostset._write_sysfs
        with tempfile.TemporaryDirectory() as directory:
            for number, online, frequency in (("0", "1", "4000000"), ("1", "0", "4500000")):
                cpu = os.path.join(directory, "cpu" + number, "cpufreq")
                os.makedirs(cpu)
                with open(os.path.join(directory, "cpu" + number, "online"), "w", encoding="ascii") as handle:
                    handle.write(online)
                with open(os.path.join(cpu, "cpuinfo_max_freq"), "w", encoding="ascii") as handle:
                    handle.write("5000000\n")
                with open(os.path.join(cpu, "scaling_max_freq"), "w", encoding="ascii") as handle:
                    handle.write(frequency + "\n")
            boostset.CPU_ROOT = directory
            writes = []

            def fake_write(path, value):
                writes.append((path, value))
                with open(path, "w", encoding="ascii") as handle:
                    handle.write(str(value) + "\n")

            boostset._write_sysfs = fake_write
            try:
                self.assertEqual(boostset.cap_all("3.0"), 3.0)
            finally:
                boostset.CPU_ROOT = old_root
                boostset._write_sysfs = old_write
            self.assertEqual([value for _, value in writes], [3000000])

    def test_hybrid_cap_reports_highest_online_core_limit(self):
        old_root = boostctl.CPU_ROOT
        with tempfile.TemporaryDirectory() as directory:
            for number, cap, ceiling in (("0", "3800000", "4000000"), ("1", "4500000", "5000000")):
                cpufreq = os.path.join(directory, "cpu" + number, "cpufreq")
                os.makedirs(cpufreq)
                with open(os.path.join(cpufreq, "scaling_max_freq"), "w", encoding="ascii") as handle:
                    handle.write(cap + "\n")
                with open(os.path.join(cpufreq, "cpuinfo_max_freq"), "w", encoding="ascii") as handle:
                    handle.write(ceiling + "\n")
            boostctl.CPU_ROOT = directory
            try:
                state = boostctl.get_state()
            finally:
                boostctl.CPU_ROOT = old_root
            self.assertEqual(state["max"], 4.5)
            self.assertEqual(state["turbo"], 5.0)

    def test_partial_sysfs_failure_restores_every_prior_cap(self):
        old_root = boostset.CPU_ROOT
        old_write = boostset._write_sysfs
        with tempfile.TemporaryDirectory() as directory:
            for number, ceiling, cap in (("0", "5000000", "4200000"), ("1", "4500000", "3900000")):
                cpu = os.path.join(directory, "cpu" + number, "cpufreq")
                os.makedirs(cpu)
                with open(os.path.join(cpu, "cpuinfo_max_freq"), "w", encoding="ascii") as handle:
                    handle.write(ceiling + "\n")
                with open(os.path.join(cpu, "scaling_max_freq"), "w", encoding="ascii") as handle:
                    handle.write(cap + "\n")
            boostset.CPU_ROOT = directory
            failed = False

            def flaky_write(path, value):
                nonlocal failed
                if path.endswith(os.path.join("cpu1", "cpufreq", "scaling_max_freq")) and not failed:
                    failed = True
                    raise OSError("injected partial apply failure")
                with open(path, "w", encoding="ascii") as handle:
                    handle.write(str(value) + "\n")

            boostset._write_sysfs = flaky_write
            try:
                with self.assertRaises(OSError):
                    boostset.cap_all("3.5")
            finally:
                boostset.CPU_ROOT = old_root
                boostset._write_sysfs = old_write
            with open(os.path.join(directory, "cpu0", "cpufreq", "scaling_max_freq"), encoding="ascii") as handle:
                self.assertEqual(handle.read().strip(), "4200000")
            with open(os.path.join(directory, "cpu1", "cpufreq", "scaling_max_freq"), encoding="ascii") as handle:
                self.assertEqual(handle.read().strip(), "3900000")

    def test_persistence_failure_rolls_back_applied_caps(self):
        old_root = boostset.CPU_ROOT
        old_write = boostset._write_sysfs
        old_uid = boostset.STATE_OWNER_UID
        with tempfile.TemporaryDirectory() as directory:
            cpu = os.path.join(directory, "cpu0", "cpufreq")
            os.makedirs(cpu)
            with open(os.path.join(cpu, "cpuinfo_max_freq"), "w", encoding="ascii") as handle:
                handle.write("5000000\n")
            control = os.path.join(cpu, "scaling_max_freq")
            with open(control, "w", encoding="ascii") as handle:
                handle.write("4200000\n")
            state_path = os.path.join(directory, "state", "maxboost")
            boostset.CPU_ROOT = directory
            boostset.STATE_OWNER_UID = os.geteuid()

            def fake_write(path, value):
                with open(path, "w", encoding="ascii") as handle:
                    handle.write(str(value) + "\n")

            boostset._write_sysfs = fake_write
            try:
                with mock.patch.object(boostset, "_write_persisted_unlocked", side_effect=OSError("injected persistence failure")):
                    with self.assertRaises(OSError):
                        boostset.set_cap("3.5", state_path)
            finally:
                boostset.CPU_ROOT = old_root
                boostset._write_sysfs = old_write
                boostset.STATE_OWNER_UID = old_uid
            with open(control, encoding="ascii") as handle:
                self.assertEqual(int(handle.read()), 4200000)
            self.assertFalse(os.path.exists(state_path))

    def test_max_restores_each_hybrid_core_native_ceiling(self):
        old_root = boostset.CPU_ROOT
        old_write = boostset._write_sysfs
        with tempfile.TemporaryDirectory() as directory:
            for number, ceiling, cap in (("0", "4500000", "3000000"), ("1", "5000000", "4000000")):
                cpu = os.path.join(directory, "cpu" + number, "cpufreq")
                os.makedirs(cpu)
                with open(os.path.join(cpu, "cpuinfo_max_freq"), "w", encoding="ascii") as handle:
                    handle.write(ceiling + "\n")
                with open(os.path.join(cpu, "scaling_max_freq"), "w", encoding="ascii") as handle:
                    handle.write(cap + "\n")
            boostset.CPU_ROOT = directory
            writes = []

            def fake_write(path, value):
                writes.append((path, value))
                with open(path, "w", encoding="ascii") as handle:
                    handle.write(str(value) + "\n")

            boostset._write_sysfs = fake_write
            try:
                self.assertEqual(boostset.cap_all("max"), "max")
            finally:
                boostset.CPU_ROOT = old_root
                boostset._write_sysfs = old_write
            self.assertEqual([value for _, value in writes], [4500000, 5000000])

    def test_manual_cap_clamps_each_hybrid_core_separately(self):
        old_root = boostset.CPU_ROOT
        old_write = boostset._write_sysfs
        with tempfile.TemporaryDirectory() as directory:
            for number, ceiling in (("0", "4000000"), ("1", "5000000")):
                cpufreq = os.path.join(directory, "cpu" + number, "cpufreq")
                os.makedirs(cpufreq)
                with open(os.path.join(cpufreq, "cpuinfo_max_freq"), "w", encoding="ascii") as handle:
                    handle.write(ceiling + "\n")
                with open(os.path.join(cpufreq, "scaling_max_freq"), "w", encoding="ascii") as handle:
                    handle.write(ceiling + "\n")
            boostset.CPU_ROOT = directory
            writes = []

            def fake_write(path, value):
                writes.append((path, value))
                with open(path, "w", encoding="ascii") as handle:
                    handle.write(str(value) + "\n")

            boostset._write_sysfs = fake_write
            try:
                self.assertEqual(boostset.cap_all("4.5"), 4.5)
            finally:
                boostset.CPU_ROOT = old_root
                boostset._write_sysfs = old_write
            self.assertEqual([value for _, value in writes], [4000000, 4500000])

    def test_cap_and_persistence_are_one_serialized_transaction(self):
        old_root = boostset.CPU_ROOT
        old_write = boostset._write_sysfs
        old_uid = boostset.STATE_OWNER_UID
        with tempfile.TemporaryDirectory() as directory:
            for number in ("0", "1"):
                cpu = os.path.join(directory, "cpu" + number, "cpufreq")
                os.makedirs(cpu)
                with open(os.path.join(cpu, "cpuinfo_max_freq"), "w", encoding="ascii") as handle:
                    handle.write("5000000\n")
                with open(os.path.join(cpu, "scaling_max_freq"), "w", encoding="ascii") as handle:
                    handle.write("4500000\n")
            state_path = os.path.join(directory, "state", "maxboost")
            boostset.CPU_ROOT = directory
            boostset.STATE_OWNER_UID = os.geteuid()
            guard = threading.Lock()
            active = 0
            maximum_active = 0

            def fake_write(path, value):
                nonlocal active, maximum_active
                with guard:
                    active += 1
                    maximum_active = max(maximum_active, active)
                try:
                    time.sleep(0.01)
                    with open(path, "w", encoding="ascii") as handle:
                        handle.write(str(value) + "\n")
                finally:
                    with guard:
                        active -= 1

            boostset._write_sysfs = fake_write
            barrier = threading.Barrier(2)
            errors = []

            def run(value):
                try:
                    barrier.wait()
                    boostset.set_cap(value, state_path)
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=run, args=(value,)) for value in ("3.0", "3.5")]
            try:
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)
            finally:
                boostset.CPU_ROOT = old_root
                boostset._write_sysfs = old_write
                boostset.STATE_OWNER_UID = old_uid
            self.assertEqual(errors, [])
            self.assertEqual(maximum_active, 1)
            with open(state_path, encoding="ascii") as handle:
                persisted = float(handle.read().strip())
            for number in ("0", "1"):
                with open(os.path.join(directory, "cpu" + number, "cpufreq", "scaling_max_freq"), encoding="ascii") as handle:
                    self.assertEqual(int(handle.read()), int(round(persisted * 1_000_000)))

    def test_persist_validates_and_serializes_shell_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            config = os.path.join(directory, "shell.json")
            with open(config, "w", encoding="utf-8") as handle:
                json.dump({"bar": {"layout": {"right": [{"id": persist.PLUGIN_ID, "maxGHz": "3.0"}]}}}, handle)
            with self.assertRaises(persist.PersistenceError):
                persist.persist_changes({"maxGHz": "nan"}, config)
            with open(config, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["bar"]["layout"]["right"][0]["maxGHz"], "3.0")
            persist.persist_changes({"maxGHz": "3.4", "capMax": ""}, config)
            with open(config, encoding="utf-8") as handle:
                entry = json.load(handle)["bar"]["layout"]["right"][0]
            self.assertEqual(entry["maxGHz"], "3.4")
            self.assertEqual(entry["capMax"], "")

    def test_concurrent_shell_writers_preserve_disjoint_fields_and_mode(self):
        script = Path(__file__).resolve().parents[1] / "persist.py"
        with tempfile.TemporaryDirectory() as home:
            config_dir = Path(home) / ".config" / "omarchy"
            config_dir.mkdir(parents=True)
            config = config_dir / "shell.json"
            count = 12
            data = {
                "version": 1,
                "bar": {
                    "layout": {
                        "left": [],
                        "center": [],
                        "right": [{"id": persist.PLUGIN_ID, "maxGHz": "3.0"}],
                    }
                },
            }
            config.write_text(json.dumps(data), encoding="utf-8")
            config.chmod(0o640)
            env = os.environ.copy()
            env["HOME"] = home
            processes = []
            for index in range(count):
                processes.append(subprocess.Popen(
                    [sys.executable, "-I", str(script), json.dumps({f"field{index}": index})],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                ))
            for process in processes:
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, stderr or stdout)
                self.assertEqual(stdout.strip(), "ok")
            result = json.loads(config.read_text(encoding="utf-8"))
            entry = result["bar"]["layout"]["right"][0]
            self.assertEqual({entry[f"field{index}"] for index in range(count)}, set(range(count)))
            self.assertEqual(entry["maxGHz"], "3.0")
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o640)
            self.assertEqual(stat.S_IMODE((config_dir / persist.LOCK_NAME).stat().st_mode), 0o600)
            self.assertFalse((config_dir / persist.JOURNAL_NAME).exists())

    def test_pending_shell_transaction_recovers_before_targeted_update(self):
        with tempfile.TemporaryDirectory() as directory:
            config = os.path.join(directory, "shell.json")
            backup = config + persist.BACKUP_SUFFIX
            old = {
                "version": 1,
                "bar": {"layout": {"right": [{"id": persist.PLUGIN_ID, "maxGHz": "3.0"}]}},
            }
            new = copy.deepcopy(old)
            new["bar"]["layout"]["right"][0]["maxGHz"] = "4.0"
            with open(config, "w", encoding="utf-8") as handle:
                json.dump(old, handle)
            os.chmod(config, 0o640)
            with open(backup, "wb") as handle:
                handle.write(b"old backup\n")
            os.chmod(backup, 0o640)
            journal = os.path.join(directory, persist.JOURNAL_NAME)
            payload = (json.dumps(new, indent=2) + "\n").encode("utf-8")
            persist._make_journal(config, [(config, payload)], journal)
            persist._atomic_write(config, payload, 0o640)
            persist.persist_changes({"maxGHz": "3.4"}, config)
            with open(config, encoding="utf-8") as handle:
                result = json.load(handle)
            self.assertEqual(result["bar"]["layout"]["right"][0]["maxGHz"], "3.4")
            with open(backup, "rb") as handle:
                self.assertEqual(handle.read(), b"old backup\n")
            self.assertEqual(stat.S_IMODE(os.stat(config).st_mode), 0o640)
            self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode), 0o640)
            self.assertFalse(os.path.exists(journal))

    def test_acpi_nominal_frequency_is_normalized_to_khz(self):
        old_root = boostctl.CPU_ROOT
        with tempfile.TemporaryDirectory() as directory:
            cpufreq = os.path.join(directory, "cpu0", "cpufreq")
            os.makedirs(cpufreq)
            cppc = os.path.join(directory, "cpu0", "acpi_cppc")
            os.makedirs(cppc)
            with open(os.path.join(cppc, "nominal_freq"), "w", encoding="ascii") as handle:
                handle.write("3801\n")
            boostctl.CPU_ROOT = directory
            try:
                self.assertEqual(boostctl.base_freq(), 3801000)
            finally:
                boostctl.CPU_ROOT = old_root

    def test_monitor_default_is_fail_closed(self):
        self.assertIsNone(boostctl.persisted())

    def test_read_only_monitor_rejects_privileged_subcommands(self):
        old_argv = sys.argv
        sys.argv = ["boostctl.py", "set", "3.0"]
        try:
            with self.assertRaises(SystemExit):
                boostctl.main()
        finally:
            sys.argv = old_argv

    def test_thermal_guard_latches_and_releases_after_cooldown(self):
        old_uid = boostset.STATE_OWNER_UID
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "state", "maxboost")
            boostset.STATE_OWNER_UID = os.geteuid()
            applied = []
            try:
                with mock.patch.object(boostset, "_apply_cap", side_effect=lambda value, skip_unchanged=False: (applied.append(value) or value, {})), \
                     mock.patch.object(boostset, "package_temp", return_value=50.0), \
                     mock.patch.object(boostset.time, "time", return_value=1000.0):
                    boostset.initialize_baseline("4.0", state_path)
                    boostset.configure_guard("1", "90", "80", "3.0", "60", state_path)
                    applied.clear()
                    policy, effective, _ = boostset._run_change(state_path, lambda value: None, 95.0, 1000.0)
                    self.assertTrue(policy["guard"]["latched"])
                    self.assertEqual(effective, "3.0")
                    policy, effective, _ = boostset._run_change(state_path, lambda value: None, 70.0, 1030.0)
                    self.assertTrue(policy["guard"]["latched"])
                    self.assertEqual(effective, "3.0")
                    policy, effective, _ = boostset._run_change(state_path, lambda value: None, 70.0, 1090.0)
                    self.assertFalse(policy["guard"]["latched"])
                    self.assertEqual(effective, "4.0")
            finally:
                boostset.STATE_OWNER_UID = old_uid
            self.assertEqual(applied[-3:], ["3.0", "3.0", "4.0"])

    def test_countdown_uses_latest_manual_baseline_and_guard_wins(self):
        old_uid = boostset.STATE_OWNER_UID
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "state", "maxboost")
            boostset.STATE_OWNER_UID = os.geteuid()
            applied = []
            try:
                with mock.patch.object(boostset, "_apply_cap", side_effect=lambda value, skip_unchanged=False: (applied.append(value) or value, {})), \
                     mock.patch.object(boostset, "package_temp", return_value=50.0), \
                     mock.patch.object(boostset.time, "time", return_value=1000.0):
                    boostset.initialize_baseline("4.0", state_path)
                    applied.clear()
                    effective = boostset.start_boost(60, state_path)
                    self.assertEqual(effective, "max")
                    policy, effective, _ = boostset._run_change(state_path, lambda value: None, 50.0, 1061.0)
                    self.assertEqual(effective, "4.0")
                    self.assertEqual(policy["boostUntil"], 0.0)
                    boostset.start_boost(60, state_path)
                    boostset.set_cap("3.5", state_path)
                    policy = boostset._read_policy_unlocked(state_path)
                    self.assertEqual(policy["boostUntil"], 0.0)
                    with mock.patch.object(boostset, "package_temp", return_value=95.0):
                        with self.assertRaises(boostset.ControlError):
                            boostset.configure_guard("1", "90", "80", "3.0", "60", state_path)
                            boostset.start_boost(60, state_path)
            finally:
                boostset.STATE_OWNER_UID = old_uid

    def test_telemetry_utilization_and_power_wrap(self):
        self.assertIsNone(boostctl.utilization(None, (100, 50)))
        self.assertAlmostEqual(boostctl.utilization((100, 50), (200, 100)), 50.0)
        previous = {"package-0": (900000, 1000000)}
        current = {"package-0": (100000, 1000000)}
        power, result = boostset._rapl_power(previous, current, 1.0)
        self.assertEqual(result, current)
        self.assertAlmostEqual(power, 0.2)

    def test_persist_validates_guard_and_countdown_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            config = os.path.join(directory, "shell.json")
            with open(config, "w", encoding="utf-8") as handle:
                json.dump({"bar": {"layout": {"right": [{"id": persist.PLUGIN_ID, "maxGHz": "3.0"}]}}}, handle)
            persist.persist_changes({
                "thermalGuard": True,
                "thermalHighC": 90,
                "thermalLowC": 80,
                "thermalCapGHz": "3.0",
                "thermalCooldown": 60,
                "boostMinutes": 10,
                "telemetryEnabled": True,
            }, config)
            with open(config, encoding="utf-8") as handle:
                entry = json.load(handle)["bar"]["layout"]["right"][0]
            self.assertEqual(entry["thermalHighC"], 90)
            self.assertEqual(entry["boostMinutes"], 10)
            with self.assertRaises(persist.PersistenceError):
                persist.persist_changes({"thermalLowC": 95}, config)
            with self.assertRaises(persist.PersistenceError):
                persist.persist_changes({"boostMinutes": 0}, config)

    def test_cpu_dirs_accept_sysfs_cpufreq_symlinks(self):
        old_root = boostset.CPU_ROOT
        with tempfile.TemporaryDirectory() as directory:
            real = os.path.join(directory, "real-cpufreq")
            os.mkdir(real)
            cpu = os.path.join(directory, "cpu0")
            os.mkdir(cpu)
            with open(os.path.join(cpu, "online"), "w", encoding="ascii") as handle:
                handle.write("1\n")
            os.symlink(real, os.path.join(cpu, "cpufreq"))
            boostset.CPU_ROOT = directory
            try:
                self.assertEqual(boostset.cpu_dirs(), [os.path.join(cpu, "cpufreq")])
            finally:
                boostset.CPU_ROOT = old_root

    def test_boot_id_is_normalized(self):
        self.assertRegex(boostset._boot_id(), r"^[0-9a-f]{32}$")

    def test_uninitialized_policy_does_not_touch_sysfs(self):
        old_uid = boostset.STATE_OWNER_UID
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "state", "maxboost")
            boostset.STATE_OWNER_UID = os.geteuid()
            try:
                with mock.patch.object(boostset, "_apply_cap") as apply:
                    policy, effective, _ = boostset._run_change(state_path, lambda value: None, 50.0, 1000.0, force=False)
            finally:
                boostset.STATE_OWNER_UID = old_uid
            self.assertFalse(policy["initialized"])
            self.assertEqual(effective, "max")
            self.assertFalse(os.path.exists(state_path))
            apply.assert_not_called()

    def test_thermal_guard_never_raises_manual_cap(self):
        old_uid = boostset.STATE_OWNER_UID
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "state", "maxboost")
            boostset.STATE_OWNER_UID = os.geteuid()
            try:
                with mock.patch.object(boostset, "_apply_cap", side_effect=lambda value, skip_unchanged=False: (value, {})), \
                     mock.patch.object(boostset, "package_temp", return_value=95.0), \
                     mock.patch.object(boostset.time, "time", return_value=1000.0):
                    boostset.initialize_baseline("2.0", state_path)
                    boostset.configure_guard("1", "90", "80", "3.0", "60", state_path)
                    policy, effective, _ = boostset._run_change(state_path, lambda value: None, 95.0, 1000.0)
                    self.assertTrue(policy["guard"]["latched"])
                    self.assertEqual(effective, "2.0")
            finally:
                boostset.STATE_OWNER_UID = old_uid

    def test_reset_policy_clears_latched_guard(self):
        old_uid = boostset.STATE_OWNER_UID
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "state", "maxboost")
            boostset.STATE_OWNER_UID = os.geteuid()
            try:
                with mock.patch.object(boostset, "_apply_cap", side_effect=lambda value, skip_unchanged=False: (value, {})), \
                     mock.patch.object(boostset, "package_temp", return_value=95.0), \
                     mock.patch.object(boostset.time, "time", return_value=1000.0):
                    boostset.initialize_baseline("4.0", state_path)
                    boostset.configure_guard("1", "90", "80", "3.0", "60", state_path)
                    effective = boostset.reset_policy(state_path)
                    self.assertEqual(effective, "max")
                    policy = boostset._read_policy_unlocked(state_path)
                    self.assertFalse(policy["guard"]["enabled"])
                    self.assertFalse(policy["guard"]["latched"])
            finally:
                boostset.STATE_OWNER_UID = old_uid

    def test_reset_policy_replaces_malformed_state(self):
        old_uid = boostset.STATE_OWNER_UID
        with tempfile.TemporaryDirectory() as directory:
            state_dir = os.path.join(directory, "state")
            os.mkdir(state_dir)
            state_path = os.path.join(state_dir, "maxboost")
            with open(state_path, "wb") as handle:
                handle.write(b"not-a-cap\n")
            with open(state_path + ".policy", "wb") as handle:
                handle.write(b"not-json\n")
            boostset.STATE_OWNER_UID = os.geteuid()
            try:
                with mock.patch.object(boostset, "_apply_cap", side_effect=lambda value, skip_unchanged=False: (value, {})):
                    self.assertEqual(boostset.reset_policy(state_path), "max")
                self.assertEqual(boostset._read_persisted(state_path), "max")
                self.assertEqual(boostset._read_policy_unlocked(state_path)["guard"]["latched"], False)
            finally:
                boostset.STATE_OWNER_UID = old_uid

    def test_countdown_target_respects_manual_ceiling(self):
        old_uid = boostset.STATE_OWNER_UID
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "state", "maxboost")
            boostset.STATE_OWNER_UID = os.geteuid()
            try:
                with mock.patch.object(boostset, "_apply_cap", side_effect=lambda value, skip_unchanged=False: (value, {})), \
                     mock.patch.object(boostset, "package_temp", return_value=50.0), \
                     mock.patch.object(boostset.time, "time", return_value=1000.0):
                    boostset.initialize_baseline("3.0", state_path)
                    effective = boostset.start_boost(60, state_path, cap="4.0")
                    self.assertEqual(effective, "4.0")
            finally:
                boostset.STATE_OWNER_UID = old_uid

    def test_installer_pins_one_helper_source_snapshot(self):
        source = Path(__file__).parents[1] / "boostset.py"
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.assertEqual(setup.HELPER_SHA256, digest)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "boostset.py")
            original = b"#!/usr/bin/python3\nprint('ok')\n"
            with open(path, "wb") as handle:
                handle.write(original)
            with mock.patch.object(setup, "HELPER_SHA256", hashlib.sha256(original).hexdigest()):
                self.assertEqual(setup.read_helper_source(path), original)
            with open(path, "wb") as handle:
                handle.write(original + b"tampered")
            with self.assertRaises(RuntimeError):
                setup.read_helper_source(path)

    def test_boot_unit_sandboxes_root_helper(self):
        unit = setup.systemd_unit()
        for directive in (
            "NoNewPrivileges=yes",
            "PrivateDevices=yes",
            "PrivateNetwork=yes",
            "ProtectSystem=strict",
            "RuntimeDirectory=omarchy-boost",
            "StateDirectory=omarchy-boost",
            "ReadWritePaths=-/sys/devices/system/cpu -/var/lib/omarchy-boost -/run/omarchy-boost",
            "CapabilityBoundingSet=",
            "SystemCallFilter=@system-service",
        ):
            self.assertIn(directive, unit)
        self.assertIn("ExecStart=/usr/bin/python3 -I " + setup.DAEMON_PATH + " run", unit)

    def test_polkit_rule_uses_json_escaping(self):
        rule = setup.polkit_rule('user"x', '/tmp/helper')
        self.assertIn('\\"x', rule)
        self.assertIn("subject.local", rule)
        self.assertNotIn('subject.user == "user"x"', rule)


if __name__ == "__main__":
    unittest.main()
