# Bounded execution harness

Status: the first Linux x86-64 synthetic milestone is implemented. The separate follow-ups below remain proposals.

## Purpose and limits

[Progress reporting](PROGRESS.md) tracks source completion, not execution equivalence. Add reproducible execution evidence without changing those counts or claiming that every game works.

The first milestone is a Linux x86-64 supervisor for one existing synthetic converted-guest probe. It needs Python's standard library, the existing relinker-only build, and no console, GPU, SDK, game files or new dependencies. This verifies the fixture's execution contract; it does not verify PS5 library semantics.

Runtime limitations remain in [Technical debt](TechnicalDebt.md).

## Running the first milestone

Use the existing [relinker-only build](BUILD.md#relinker-only) with `BUILD_TESTING=ON`. On Linux x86-64 with Python, normal CTest runs automatically include `linux_entry_argv` and `execution_harness`. To run only this milestone:

```sh
ctest --test-dir build-relinker -R '^(linux_entry_argv|execution_harness)$' --output-on-failure
```

The supervisor is [`execution_harness.py`](../../core/relinker/relinker/tests/execution_harness.py); its adversarial checks are [`test_execution_harness.py`](../../core/relinker/relinker/tests/test_execution_harness.py). Both tests have a 25-second CTest timeout. The fixture has one 20-second monotonic budget, reserving 0.5 seconds per active phase for cleanup, with a 0.1-second termination grace. Output retention is limited to 256 KiB per stream per phase.

Each fixture invocation prints its report path. Reports have unique names under `<build>/tests/execution-reports/`, including failed invocations. They are build artifacts, not source files. Direct invocation accepts the relinker path and `--report-dir`; without that option it uses the system temporary directory's `anyps5-execution-reports` directory. Source directories are rejected as report destinations except for separate CMake build trees.

Schema version 1 uses `identity`, `host`, `limits`, `expectation` and `phases` objects alongside the fixture, outcome, reason and elapsed time. Negative Python return codes identify signals; `signal` also records their positive number. Captured streams are UTF-8 text with replacement for undecodable bytes, accompanied by retained byte counts. `validate_report()` rejects missing fields, invalid types, unknown versions, invalid identities and limits, and incomplete or mismatched PASS observations. JSON parsing rejects truncated reports.

CTest supplies source revision and dirty state captured **at CMake configuration time**, plus the build configuration. Reconfigure after changing or committing sources to refresh that identity. Unknown identity fields are JSON `null`. SHA-256 identifies the fixture bytes, relinker and generated executable. Only the reviewed synthetic argv expectations are accepted; reports contain no hardware expectation or PS5-equivalence claim. The existing progress counts and badges are unchanged.

## Existing starting point

[`test_linux_entry_argv.py`](../../core/relinker/relinker/tests/test_linux_entry_argv.py) builds a synthetic ELF using `argv_fixture()`, converts it with `relinker --skip-sce-module`, makes the output executable, and launches it. Its two cases expect `SIGTRAP` for argument `Z` and `SIGILL` for arguments `Z extra`. These signals are intentional fixture outcomes, not generic success conventions.

[`core/relinker/CMakeLists.txt`](../../core/relinker/CMakeLists.txt) registers `linux_entry_argv` with `$<TARGET_FILE:relinker>` when Python is available. This exercises generated guest execution rather than host-linked library calls. It does not exercise the patched system libraries.

The fixture and its expectations are unchanged. Its process-launch plumbing uses the small helper under the existing relinker test directory. There is no generic manifest engine or change to the ELF conversion algorithm in this milestone.

## First implementation

1. Use an isolated temporary directory and argument arrays without shell execution. Accept the built relinker path from CTest; retain the existing fixture construction and conversion arguments.
2. Supervise conversion and each execution separately. Start each child in a new Linux session/process group. Apply a monotonic deadline and terminate the group on timeout, output-limit violation, interruption, and after the leader exits; escalate to `SIGKILL` after a bounded grace period and reap direct children. Test a descendant that retains output pipes after its parent exits.
3. Drain stdout and stderr concurrently with bounded retained bytes. Limit each stream to 256 KiB; exceeding a limit is a failure, not silently truncated success. Bound pipe-drain time as well as child lifetime. Disable core dumps for the deliberate signal fixtures.
4. Use a 20-second total budget per fixture invocation, including conversion, both executions and cleanup; use an outer 25-second CTest timeout. Keep each supervisor self-test short and below the contributor limit.
5. Compare return codes exactly. Accept the two expected signals only for their named cases. Conversion failure, unexpected signal, timeout, incomplete observations or supervisor errors fail. Never launch stale output after conversion fails.
6. Emit one versioned JSON report per invocation outside the source tree. Retain diagnostics on failure before temporary-directory cleanup. No candidate output may automatically become an expectation.

Process groups cover cooperative test children that do not detach. They are not a sandbox or a guarantee of cleaning up arbitrary hostile descendants. Fixtures must not call `setsid` or otherwise escape the group. Supporting arbitrary executables would require a separately designed containment mechanism.

## Result contract

Version 1 reports contain:

- Schema version, fixture/case identifiers and explicit outcome/reason.
- Source revision supplied by the build, dirty-state indicator when known, fixture hash, relinker hash and generated executable hash. Unknown identity fields are explicit, not invented.
- Host OS/architecture, Python version, build configuration, command arguments and configured limits.
- Per-phase exit code or signal, timeout/output-limit indicators, elapsed time, and captured stdout/stderr.
- Expected outcome and provenance: fixture source/revision, specification, or hardware measurement with device/firmware/settings. This milestone uses reviewed synthetic-fixture expectations, not PS5 measurements.

The parent writes the report; the first guest fixture does not need a JSON writer. Report validation must reject missing required fields, unknown schema versions and malformed/truncated data. Record only the observations required by a fixture; do not add address normalization, image tolerances or unspecified-byte masking.

| Outcome | Meaning |
| --- | --- |
| PASS | All required observations match the stated expectation source. |
| FAIL | Execution, supervision, reporting or comparison failed. |
| UNSUPPORTED | The host lacks a declared requirement. |
| SKIPPED | The check was intentionally not exercised. |
| UNVERIFIED | Execution completed but has no accepted expectation source. |

PASS against a synthetic contract must not be presented as hardware-verified PS5 equivalence. A crash or malformed observation cannot be downgraded to UNVERIFIED.

## CTest integration and acceptance

Keep existing test names and build entry points from [Build](BUILD.md). Register the supervised execution case only on Linux x86-64 with Python available. Unsupported platforms retain applicable existing conversion checks but must not claim guest execution. A configuration without Python must visibly report omitted tests.

On the supported CI configuration, missing required binaries or expectation data fail. Optional future fixtures may map explicit unsupported/skipped/unverified outcomes to CTest's configured skip code, but their reports must remain distinct and outside any verified-pass count. A green CTest summary alone is not evidence that omitted tests ran.

Acceptance for the implementation PR:

- Both existing argument cases run through conversion and generated execution and match their intentional signals.
- Wrong expectations, failed conversion, missing binary, unexpected crash and hangs fail with phase-specific diagnostics.
- Process-group descendants are terminated after timeout and leader exit; retained pipes cannot hang the supervisor.
- Output flooding fails within limits; interruption uses the same cleanup path.
- Missing expectations never pass; malformed reports are rejected by report-validation tests.
- Each test meets its budget, is registered with CTest, and passes the existing registration/conventions checks.
- Reports distinguish synthetic expectations from hardware evidence; the progress badges remain unchanged.

## Separate follow-ups

Each follow-up requires its own implementation PR and acceptance tests:

1. Add one converted guest TLS or callback/ABI probe using existing fixture machinery. State exactly which loader/library path it exercises and where its expected values originate.
2. Add one standalone compute output-comparison fixture using the existing Vulkan execution tests. Use reviewed expected buffers with explicit provenance, then test cached and uncached execution. [Hardware oracle](HW_ORACLE.md) measurements on desktop AMD hardware remain distinct from PS5 measurements.
3. Add Windows supervision with Job Objects or an equivalent supported mechanism, with Windows-specific descendant, crash and timeout tests. Do not reuse Linux process-group assumptions.
4. Accept externally collected PS5 observations only when an authorized reference runner is available. Record firmware and collection settings; do not promise console deployment tooling or make reference access a prerequisite for the first milestone.

Arbitrary captured-shader replay requires an audit of captured resources and runtime state before execution can be proposed. Full-game automation, graphics/audio comparisons, deterministic whole-system replay, network services, VR support and universal compatibility are outside this roadmap. Proprietary binaries and captures are not committed to the public repository.
