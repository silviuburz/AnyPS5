"""Short adversarial checks for the synthetic execution supervisor and reports."""

import contextlib
import copy
import io
import json
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from execution_harness import OUTPUT_LIMIT, phase_passed, supervise, validate_report, write_report
from test_linux_entry_argv import run_fixture

RELINKER = Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 else None


class SupervisorTests(unittest.TestCase):
    def run_child(self, code, budget=2, expected=0):
        return supervise([sys.executable, "-c", code], time.monotonic() + budget, "Z", expected)

    def test_exact_exit_and_missing_expectation(self):
        phase = self.run_child("print('out'); import sys; print('err', file=sys.stderr)")
        self.assertTrue(phase_passed(phase))
        self.assertEqual(phase["stdout"], "out\n")
        self.assertEqual(phase["stderr"], "err\n")
        phase["expected_returncode"] = 1
        self.assertFalse(phase_passed(phase))
        phase["expected_returncode"] = None
        self.assertFalse(phase_passed(phase))

    def test_unexpected_crash(self):
        phase = self.run_child("import os, signal; os.kill(os.getpid(), signal.SIGSEGV)")
        self.assertEqual(phase["returncode"], -signal.SIGSEGV)
        self.assertFalse(phase_passed(phase))

    def test_hang(self):
        started = time.monotonic()
        phase = self.run_child("import time; time.sleep(30)", budget=0.8)
        self.assertTrue(phase["timeout"])
        self.assertFalse(phase_passed(phase))
        self.assertLess(time.monotonic() - started, 1.2)

    def test_each_output_limit(self):
        for fd in (1, 2):
            with self.subTest(fd=fd):
                phase = self.run_child(
                    f"import os; os.write({fd}, b'x' * ({OUTPUT_LIMIT} + 1))")
                self.assertTrue(phase["output_limit"])
                self.assertFalse(phase_passed(phase))
                name = "stdout" if fd == 1 else "stderr"
                self.assertEqual(phase[name + "_bytes"], OUTPUT_LIMIT)
                self.assertEqual(len(phase[name]), OUTPUT_LIMIT)

    def test_exact_output_limit(self):
        phase = self.run_child(f"import os; os.write(1, b'x' * {OUTPUT_LIMIT})")
        self.assertTrue(phase_passed(phase))

    def test_both_pipes_and_disabled_core_dumps(self):
        phase = self.run_child(
            "import os, resource\n"
            "assert resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)\n"
            "os.write(1, b'x' * 131072)\n"
            "os.write(2, b'y' * 131072)\n")
        self.assertTrue(phase_passed(phase))
        self.assertEqual(phase["stdout_bytes"], 131072)
        self.assertEqual(phase["stderr_bytes"], 131072)

    def assert_descendant_stopped(self, pid):
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            try:
                state = Path(f"/proc/{pid}/stat").read_text().split(") ")[1].split()[0]
            except FileNotFoundError:
                return
            if state == "Z":
                return
            time.sleep(0.01)
        self.fail("process-group descendant survived cleanup")

    def test_descendant_retains_pipes(self):
        for parent in ("os._exit(0)", "time.sleep(30)"):
            with self.subTest(parent=parent):
                phase = self.run_child(
                    "import os, signal, time\n"
                    "pid = os.fork()\n"
                    "if pid == 0:\n"
                    " signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                    " print(os.getpid(), flush=True)\n"
                    " time.sleep(30)\n"
                    "else:\n"
                    " time.sleep(0.05)\n"
                    f" {parent}\n", budget=1.0)
                self.assertTrue(phase["complete"])
                self.assertEqual(phase["timeout"], parent != "os._exit(0)")
                self.assert_descendant_stopped(int(phase["stdout"]))

    def test_descendant_without_pipes(self):
        phase = self.run_child(
            "import os, signal, time\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            " signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            " print(os.getpid(), flush=True)\n"
            " os.close(1); os.close(2)\n"
            " time.sleep(30)\n"
            "else:\n"
            " time.sleep(0.05)\n"
            " os._exit(0)\n")
        self.assertTrue(phase_passed(phase))
        self.assert_descendant_stopped(int(phase["stdout"]))

    def test_interruption_cleans_group(self):
        def interrupted(*_):
            raise KeyboardInterrupt

        previous = signal.signal(signal.SIGALRM, interrupted)
        try:
            signal.setitimer(signal.ITIMER_REAL, 0.2)
            phase = self.run_child(
                "import os, signal, time\n"
                "if os.fork() == 0:\n"
                " signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                " print(os.getpid(), flush=True)\n"
                "time.sleep(30)\n")
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
        self.assertTrue(phase["interrupted"])
        self.assertFalse(phase_passed(phase))
        self.assert_descendant_stopped(int(phase["stdout"]))

    def test_missing_binary(self):
        phase = supervise(["/nonexistent/anyps5-relinker"], time.monotonic() + 2, "conversion", 0)
        self.assertFalse(phase_passed(phase))
        self.assertIsNotNone(phase["error"])


class FixtureReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if RELINKER is None:
            raise RuntimeError("self-tests require the built relinker")
        cls.directory = tempfile.TemporaryDirectory(prefix="anyps5-harness-selftest-")
        with contextlib.redirect_stdout(io.StringIO()):
            cls.report, cls.path = run_fixture(RELINKER, cls.directory.name)
        if cls.report["outcome"] != "PASS":
            raise AssertionError(cls.report)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_actual_contract_and_report_roundtrip(self):
        report = json.loads(self.path.read_text())
        validate_report(report)
        self.assertEqual([p["returncode"] for p in report["phases"]],
                         [0, -signal.SIGTRAP, -signal.SIGILL])
        self.assertEqual(report["expectation"]["kind"], "reviewed-synthetic-fixture")

    def test_wrong_or_missing_expectations(self):
        for expected in (0, None):
            report = copy.deepcopy(self.report)
            report["phases"][1]["expected_returncode"] = expected
            with self.assertRaises(ValueError):
                validate_report(report)
        report = copy.deepcopy(self.report)
        report["expectation"]["cases"] = {}
        with self.assertRaises(ValueError):
            validate_report(report)

    def test_conversion_failure_never_launches_guest(self):
        with tempfile.TemporaryDirectory(prefix="anyps5-failed-conversion-") as directory:
            def failed_conversion(command, deadline, phase, expected):
                Path(command[-1]).write_bytes(b"stale output")
                return supervise([sys.executable, "-c", "import sys; sys.exit(7)"],
                                 deadline, phase, expected)

            with patch("execution_harness.supervise", side_effect=failed_conversion) as mocked:
                with contextlib.redirect_stdout(io.StringIO()):
                    report, path = run_fixture(RELINKER, directory)
            self.assertEqual(mocked.call_count, 1)
            self.assertEqual(report["outcome"], "FAIL")
            self.assertIn("conversion failed", report["reason"])
            self.assertEqual(json.loads(path.read_text())["phases"][0]["returncode"], 7)

    def test_missing_relinker_report(self):
        with contextlib.redirect_stdout(io.StringIO()):
            report, path = run_fixture(Path(self.directory.name) / "missing", self.directory.name)
        self.assertEqual(report["outcome"], "FAIL")
        self.assertEqual(len(report["phases"]), 1)
        validate_report(json.loads(path.read_text()))

    def test_execution_failures_and_shared_budget(self):
        for code in (
                "pass",
                "import os, signal; os.kill(os.getpid(), signal.SIGSEGV)",
                "import time; time.sleep(30)"):
            with self.subTest(code=code):
                def substituted(command, deadline, phase, expected):
                    if phase == "conversion":
                        return supervise(command, deadline, phase, expected)
                    return supervise([sys.executable, "-c", code], deadline, phase, expected)

                with patch("execution_harness.supervise", side_effect=substituted):
                    with contextlib.redirect_stdout(io.StringIO()):
                        report, path = run_fixture(RELINKER, self.directory.name, budget=1.0)
                self.assertEqual(report["outcome"], "FAIL")
                self.assertIn("Z failed", report["reason"])
                self.assertEqual(len(report["phases"]), 2)
                self.assertLess(report["elapsed_seconds"], 1.0)
                validate_report(json.loads(path.read_text()))

    def test_malformed_reports(self):
        for key in self.report:
            report = copy.deepcopy(self.report)
            del report[key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_report(report)
        for key in ("identity", "host", "limits", "expectation"):
            for field in self.report[key]:
                report = copy.deepcopy(self.report)
                del report[key][field]
                with self.subTest(key=key, field=field), self.assertRaises(ValueError):
                    validate_report(report)
        for field in self.report["phases"][0]:
            report = copy.deepcopy(self.report)
            del report["phases"][0][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_report(report)
        for mutate in (
                lambda r: r.update(schema_version=2),
                lambda r: r.update(phases=r["phases"][:1]),
                lambda r: r.update(elapsed_seconds=float("nan")),
                lambda r: r["limits"].update(total_seconds=-1),
                lambda r: r.update(elapsed_seconds=21),
                lambda r: r["phases"][0].update(returncode="0"),
                lambda r: r["phases"][0].update(stdout="x" * (OUTPUT_LIMIT + 1)),
                lambda r: r["phases"][1].update(complete=False),
                lambda r: r["identity"].update(fixture_sha256="invalid")):
            report = copy.deepcopy(self.report)
            mutate(report)
            with self.assertRaises(ValueError):
                validate_report(report)
        for outcome in ("UNVERIFIED", "SKIPPED", "UNSUPPORTED"):
            report = copy.deepcopy(self.report)
            report["outcome"] = outcome
            report["phases"][1]["timeout"] = True
            with self.assertRaises(ValueError):
                validate_report(report)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(self.path.read_text()[:-10])
        with self.assertRaises(ValueError):
            write_report(self.report, Path(__file__).parent)


if __name__ == "__main__":
    unittest.main()
