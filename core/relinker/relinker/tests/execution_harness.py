"""Bounded Linux process-group supervision for cooperative synthetic fixtures."""

import hashlib
import json
import math
import os
from pathlib import Path
import resource
import selectors
import shutil
import signal
import subprocess
import time
import uuid

OUTPUT_LIMIT = 256 * 1024
TOTAL_BUDGET = 20.0
GRACE = 0.1
CLEANUP_BUDGET = 0.5


def file_hash(path):
    with open(path, "rb") as stream:
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _disable_core_dumps():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _kill_group(process, sig):
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def supervise(command, deadline, phase, expected, cwd=None):
    """The deadline includes cleanup; children must not escape their session."""
    started = time.monotonic()
    command = list(command)
    executable = command[0]
    if os.path.dirname(executable):
        executable = str(Path(executable).resolve())
    else:
        executable = shutil.which(executable) or executable
    command[0] = executable
    result = {
        "case": phase, "command": command, "expected_returncode": expected,
        "returncode": None, "signal": None, "timeout": False,
        "output_limit": False, "interrupted": False, "complete": False,
        "error": None, "elapsed_seconds": 0.0, "stdout": "", "stderr": "",
        "stdout_bytes": 0, "stderr_bytes": 0,
    }
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    process = None
    killed = False
    try:
        if time.monotonic() >= deadline - CLEANUP_BUDGET:
            result["timeout"] = True
            return result
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True,
            preexec_fn=_disable_core_dumps, cwd=cwd)
        with selectors.DefaultSelector() as selector:
            for name in buffers:
                stream = getattr(process, name)
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            stopping = None
            while True:
                now = time.monotonic()
                if stopping is None:
                    if now >= deadline - CLEANUP_BUDGET:
                        result["timeout"] = True
                    if process.poll() is not None or result["timeout"] or result["output_limit"]:
                        _kill_group(process, signal.SIGTERM)
                        stopping = now
                if stopping is not None and now - stopping >= GRACE and not killed:
                    _kill_group(process, signal.SIGKILL)
                    killed = True
                if killed and process.poll() is not None and not selector.get_map():
                    result["complete"] = True
                    break
                if stopping is not None and now - stopping >= CLEANUP_BUDGET:
                    result["error"] = "incomplete child/pipe cleanup"
                    break
                for key, _ in selector.select(0.01):
                    block = os.read(key.fileobj.fileno(), 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    name = key.data
                    available = OUTPUT_LIMIT - len(buffers[name])
                    buffers[name].extend(block[:available])
                    if len(block) > available:
                        result["output_limit"] = True
    except KeyboardInterrupt:
        result["interrupted"] = True
        result["error"] = result["error"] or "supervisor interrupted"
    except (OSError, ValueError) as error:
        result["error"] = str(error)
    finally:
        if process is not None:
            if not killed:
                _kill_group(process, signal.SIGTERM)
                try:
                    time.sleep(min(GRACE, max(0, deadline - time.monotonic())))
                except KeyboardInterrupt:
                    result["interrupted"] = True
                    result["error"] = result["error"] or "supervisor interrupted"
                _kill_group(process, signal.SIGKILL)
            try:
                process.wait(timeout=max(0.01, min(GRACE, deadline - time.monotonic())))
            except KeyboardInterrupt:
                result["interrupted"] = True
                result["error"] = result["error"] or "supervisor interrupted"
                _kill_group(process, signal.SIGKILL)
                try:
                    process.wait(timeout=GRACE)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    result["error"] = "direct child could not be reaped"
            except subprocess.TimeoutExpired:
                result["error"] = "direct child could not be reaped"
            result["returncode"] = process.returncode
            if process.returncode is not None and process.returncode < 0:
                result["signal"] = -process.returncode
            process.stdout.close()
            process.stderr.close()
        for name, data in buffers.items():
            result[name] = data.decode("utf-8", errors="replace")
            result[name + "_bytes"] = len(data)
        result["elapsed_seconds"] = time.monotonic() - started
    return result


def phase_passed(phase):
    return (phase["expected_returncode"] is not None and phase["complete"]
            and phase["returncode"] == phase["expected_returncode"]
            and not any(phase[key] for key in ("timeout", "output_limit", "interrupted", "error")))


def _fields(value, fields):
    if not isinstance(value, dict):
        raise ValueError("expected report object")
    for key, types in fields.items():
        if key not in value or type(value[key]) not in types:
            raise ValueError("missing or malformed report field: " + key)


def validate_report(report):
    """Reject incomplete schemas and PASS without the exact synthetic contract."""
    nullable_text = (str, type(None))
    _fields(report, {
        "schema_version": (int,), "fixture": (str,), "outcome": (str,), "reason": (str,),
        "identity": (dict,), "host": (dict,), "limits": (dict,),
        "expectation": (dict,), "phases": (list,), "elapsed_seconds": (float, int),
    })
    if report["schema_version"] != 1 or report["fixture"] != "linux_entry_argv":
        raise ValueError("unknown schema or fixture")
    if report["outcome"] not in ("PASS", "FAIL", "UNSUPPORTED", "SKIPPED", "UNVERIFIED"):
        raise ValueError("unknown outcome")
    if not report["reason"]:
        raise ValueError("missing reason")
    _fields(report["identity"], {
        "source_revision": nullable_text, "dirty": (bool, type(None)),
        "fixture_sha256": nullable_text, "relinker_sha256": nullable_text,
        "generated_sha256": nullable_text,
    })
    for key in ("fixture_sha256", "relinker_sha256", "generated_sha256"):
        digest = report["identity"][key]
        if digest is not None and (len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
            raise ValueError("malformed hash: " + key)
    _fields(report["host"], {"os": (str,), "architecture": (str,), "python": (str,),
                             "build_configuration": nullable_text})
    _fields(report["limits"], {"total_seconds": (float, int), "stream_bytes": (int,),
                               "cleanup_seconds": (float, int), "grace_seconds": (float, int)})
    limits = report["limits"]
    if (not math.isfinite(limits["total_seconds"]) or not 0 < limits["total_seconds"] <= TOTAL_BUDGET
            or limits["stream_bytes"] != OUTPUT_LIMIT
            or limits["cleanup_seconds"] != CLEANUP_BUDGET or limits["grace_seconds"] != GRACE):
        raise ValueError("invalid configured limits")
    _fields(report["expectation"], {"kind": (str,), "source": (str,),
                                    "revision": nullable_text, "cases": (dict,)})
    expectation = report["expectation"]
    if (expectation["kind"] != "reviewed-synthetic-fixture" or not expectation["source"]
            or expectation["cases"] != {"Z": -signal.SIGTRAP, "Z extra": -signal.SIGILL}):
        raise ValueError("missing or incorrect synthetic expectations")
    seen = []
    for phase in report["phases"]:
        _fields(phase, {
            "case": (str,), "command": (list,), "expected_returncode": (int, type(None)),
            "returncode": (int, type(None)), "signal": (int, type(None)),
            "timeout": (bool,), "output_limit": (bool,), "interrupted": (bool,),
            "complete": (bool,), "error": nullable_text, "elapsed_seconds": (float, int),
            "stdout": (str,), "stderr": (str,), "stdout_bytes": (int,), "stderr_bytes": (int,),
        })
        if not phase["command"] or any(type(arg) is not str for arg in phase["command"]):
            raise ValueError("malformed command")
        if phase["case"] not in ("conversion", "Z", "Z extra") or phase["case"] in seen:
            raise ValueError("unknown or duplicate phase")
        expected = 0 if phase["case"] == "conversion" else expectation["cases"][phase["case"]]
        if phase["expected_returncode"] != expected:
            raise ValueError("phase uses an unreviewed expectation")
        seen.append(phase["case"])
        code = phase["returncode"]
        if phase["signal"] != (-code if code is not None and code < 0 else None):
            raise ValueError("inconsistent signal")
        for name in ("stdout", "stderr"):
            if (not 0 <= phase[name + "_bytes"] <= OUTPUT_LIMIT
                    or len(phase[name]) > phase[name + "_bytes"]):
                raise ValueError("invalid retained output size")
        if not math.isfinite(phase["elapsed_seconds"]) or phase["elapsed_seconds"] < 0:
            raise ValueError("invalid phase elapsed time")
    if not math.isfinite(report["elapsed_seconds"]) or report["elapsed_seconds"] < 0:
        raise ValueError("invalid elapsed time")
    if report["outcome"] != "FAIL" and any(not phase_passed(p) for p in report["phases"]):
        raise ValueError("failed observation cannot be downgraded")
    if report["outcome"] == "PASS":
        if report["elapsed_seconds"] > limits["total_seconds"]:
            raise ValueError("PASS exceeded total budget")
        if seen != ["conversion", "Z", "Z extra"] or not all(map(phase_passed, report["phases"])):
            raise ValueError("PASS requires all complete matching phases")
        if any(report["identity"][key] is None for key in (
                "fixture_sha256", "relinker_sha256", "generated_sha256")):
            raise ValueError("PASS lacks artifact identities")


def write_report(report, directory):
    validate_report(report)
    directory = Path(directory).resolve()
    source_tree = Path(__file__).resolve().parents[4]
    if directory == source_tree or source_tree in directory.parents:
        if not any((parent / "CMakeCache.txt").is_file() for parent in (directory, *directory.parents)
                   if parent != source_tree and source_tree in parent.parents):
            raise ValueError("reports must be outside the source tree or in a CMake build tree")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("linux_entry_argv-" + uuid.uuid4().hex + ".json")
    with path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write("\n")
    with path.open(encoding="utf-8") as stream:
        validate_report(json.load(stream))
    return path
