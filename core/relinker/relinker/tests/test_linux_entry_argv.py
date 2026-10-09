"""Check that a Linux relink hands the guest entry the process argc and inline argv array."""

from pathlib import Path
import argparse
import hashlib
import platform
import signal
import struct
import subprocess
import sys
import tempfile
import time

from test_linux_load_alignment import fixture

ENTRY = 0x10
ARGV_CHECK = bytes.fromhex(
    "833f02" "751e"
    "48837f0800" "7417"
    "488b4710"
    "80385a" "750e"
    "80780100" "7508"
    "48837f1800" "7501"
    "cc"
    "0f0b")


def argv_fixture():
    image = fixture()
    struct.pack_into("<Q", image, 24, ENTRY)
    image[0x4000 + ENTRY:0x4000 + ENTRY + len(ARGV_CHECK)] = ARGV_CHECK
    return image


def run_fixture(relinker, report_directory, revision=None, dirty=None, configuration=None,
                budget=20.0):
    from execution_harness import OUTPUT_LIMIT, GRACE, CLEANUP_BUDGET
    from execution_harness import file_hash, phase_passed, supervise, write_report

    relinker = Path(relinker).resolve()
    started = time.monotonic()
    deadline = started + budget
    image = argv_fixture()
    report = {
        "schema_version": 1, "fixture": "linux_entry_argv", "outcome": "FAIL",
        "reason": "fixture did not complete", "elapsed_seconds": 0.0,
        "identity": {
            "source_revision": revision, "dirty": dirty,
            "fixture_sha256": hashlib.sha256(image).hexdigest(),
            "relinker_sha256": None, "generated_sha256": None,
        },
        "host": {"os": platform.system(), "architecture": platform.machine(),
                 "python": platform.python_version(), "build_configuration": configuration},
        "limits": {"total_seconds": budget, "stream_bytes": OUTPUT_LIMIT,
                   "cleanup_seconds": CLEANUP_BUDGET, "grace_seconds": GRACE},
        "expectation": {
            "kind": "reviewed-synthetic-fixture", "source": str(Path(__file__).resolve()),
            "revision": revision, "cases": {"Z": -signal.SIGTRAP, "Z extra": -signal.SIGILL},
        },
        "phases": [],
    }
    try:
        with tempfile.TemporaryDirectory(prefix="anyps5-argv-") as directory:
            source = Path(directory) / "input.elf"
            output = Path(directory) / "output.elf"
            source.write_bytes(image)
            conversion = supervise(
                [str(relinker), "--skip-sce-module", str(source), str(output)],
                deadline, "conversion", 0, cwd=directory)
            report["phases"].append(conversion)
            if relinker.is_file():
                report["identity"]["relinker_sha256"] = file_hash(relinker)
            if not phase_passed(conversion):
                report["reason"] = phase_failure(conversion)
            else:
                report["identity"]["generated_sha256"] = file_hash(output)
                output.chmod(0o755)
                for arguments, expected in ((["Z"], -signal.SIGTRAP), (["Z", "extra"], -signal.SIGILL)):
                    phase = supervise([str(output), *arguments], deadline, " ".join(arguments),
                                      expected, cwd=directory)
                    report["phases"].append(phase)
                    if not phase_passed(phase):
                        report["reason"] = phase_failure(phase)
                        break
                else:
                    report["outcome"] = "PASS"
                    report["reason"] = "Both deliberate signals match the synthetic argv contract; not PS5 equivalence"
            report["elapsed_seconds"] = time.monotonic() - started
            if report["elapsed_seconds"] > budget:
                report["outcome"] = "FAIL"
                report["reason"] = "total fixture budget exceeded"
            path = write_report(report, report_directory)
    except (OSError, ValueError, KeyboardInterrupt) as error:
        report["outcome"] = "FAIL"
        report["reason"] = "supervisor/report error: " + str(error)
        report["elapsed_seconds"] = time.monotonic() - started
        path = write_report(report, report_directory)
    print(report["outcome"] + ": " + report["reason"])
    print("Execution report:", path)
    return report, path


def phase_failure(phase):
    return (f'{phase["case"]} failed: returncode={phase["returncode"]}, '
            f'expected={phase["expected_returncode"]}, timeout={phase["timeout"]}, '
            f'output_limit={phase["output_limit"]}, complete={phase["complete"]}, '
            f'error={phase["error"]}')


def conversion_only(relinker):
    with tempfile.TemporaryDirectory(prefix="anyps5-argv-") as directory:
        source = Path(directory) / "input.elf"
        output = Path(directory) / "output.elf"
        source.write_bytes(argv_fixture())
        result = subprocess.run([str(relinker), "--skip-sce-module", str(source), str(output)],
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, (result.stdout, result.stderr)
    print("Linux entry argv conversion passed; guest execution UNSUPPORTED on this host")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("relinker", type=Path)
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--revision")
    parser.add_argument("--dirty", choices=("true", "false", "unknown"), default="unknown")
    parser.add_argument("--configuration")
    options = parser.parse_args()
    relinker = options.relinker.resolve()
    if not (sys.platform.startswith("linux") and platform.machine() in ("x86_64", "AMD64")):
        conversion_only(relinker)
        return 0
    directory = options.report_dir or Path(tempfile.gettempdir()) / "anyps5-execution-reports"
    dirty = {"true": True, "false": False, "unknown": None}[options.dirty]
    previous = signal.signal(signal.SIGTERM, lambda *_: interrupt())
    try:
        report, _ = run_fixture(relinker, directory,
                                None if options.revision == "unknown" else options.revision,
                                dirty, options.configuration or None)
    finally:
        signal.signal(signal.SIGTERM, previous)
    return 0 if report["outcome"] == "PASS" else 1


def interrupt():
    raise KeyboardInterrupt


if __name__ == "__main__":
    sys.exit(main())
