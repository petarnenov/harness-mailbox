---
name: da-python-test-conventions
description: "Test rules for the harness-mailbox dashboard: Python unittest, node --test for the buildless frontend, isolated Make/Docker test and coverage runs. Preloaded into the Python test writer and with production rules into writer/reviewer."
user-invocable: false
globs: []
---

RULEBOOK: da-python-tests@2026-11-23.1

# Python test rules

These rules cover `tests/*.py`, `tests/js/*.test.mjs` and `workspace/.agents/skills/mailbox-orchestrator/scripts/test_*.py`. Production rules are in `da-python-conventions` (§K1–§K12). Run everything through the repository Make targets from the repository root.

## §T1. Test layers and locations
- **§T1.1 (P2)** Python tests use the standard-library `unittest` in `tests/` (dashboard) or beside the orchestrator scripts as `test_*.py`; use `unittest.mock` only for boundaries, not for the unit under test.
- **§T1.2 (P2)** Build mailbox fixtures in a `tempfile.TemporaryDirectory` with only the records the case needs; never point tests at the live mailbox.
- **§T1.3 (P2)** Test HTTP behavior against a real `ThreadingHTTPServer` on an ephemeral local port and assert status, headers and body.
- **§T1.4 (P2)** Frontend tests are `node --test` files in `tests/js/` that load `static/app.js` in a `vm` context with inert DOM stubs; test pure functions directly.
- **§T1.5 (P3)** Validate shell, Docker and Compose changes with `make lint`, `make doctor` or a recorded build/run, and keep that output as evidence.

## §T2. Assertions and safety cases
- **§T2.1 (P1)** Safety tests assert negative outcomes: mutating HTTP methods return `405`, symlinked or out-of-root paths are rejected, oversized or non-finite JSON becomes a warning, and the gate denies unknown, unmet, cyclic, stale or tampered inputs.
- **§T2.2 (P1)** Verify that atomic publication never overwrites an existing record and leaves no temporary file behind on failure.
- **§T2.3 (P2)** Assert explicit expected values; one behavior per test with a name that states it. Use subTests for input matrices and near-misses for multi-condition rules.
- **§T2.4 (P3)** Tests without meaningful assertions or skipped tests do not count toward coverage.
- **§T2.5 (P2)** When a check counts or reads evidence, fail it if the read itself fails; never let an empty read pass as a zero count.

## §T3. Determinism and isolation
- **§T3.1 (P1)** Tests run with networking disabled and the source mounted read-only; they must not need network access, real credentials, real Jira/AIO or real mailbox data.
- **§T3.2 (P2)** Pin time, UUIDs and environment variables through injected values or `unittest.mock.patch`, and restore process-wide state after each test.
- **§T3.3 (P2)** Write only inside the test's temporary directory; never depend on test order or shared mutable fixtures.

## §T4. Fixtures and secrets
- **§T4.1 (P1)** Fixtures are synthetic: no real tokens, Jira payloads with personal data, or copies of live mailbox records.
- **§T4.2 (P2)** Before deleting or renaming a fixture or demo record, search tests and demo data for references.

## §T5. Coverage
- **§T5.1 (P1)** Every touched production module reaches at least 85% measured line coverage. Measure Python with `make coverage` (`.coverage-output/coverage.json`) and report per-module figures, not only a total.
- **§T5.2 (P2)** Map changed branches and acceptance criteria to tests. Do not describe an estimate or hand count as measured coverage.
- **§T5.3 (P2)** JavaScript coverage of touched `static/*.js` must come from a recorded measurement (for example `node --test --experimental-test-coverage` in the isolated Node image); configuration, shell and Docker changes need named validation evidence instead of a coverage figure.

## §T6. Regression discipline
- **§T6.1 (P2)** For a reported bug, write a regression test that fails on the base revision for the stated reason and passes on the fix, using a detached worktree when needed.
- **§T6.2 (P2)** Pin existing behavior that a changed function or branch must preserve, unless an existing test already proves it.
- **§T6.3 (P3)** Add a comment only when the test name cannot preserve the reason for a regression guard.

## §T7. Production testability
- **§T7.1 (P2)** Keep projection and gate decisions in functions that take the mailbox root and inputs explicitly.
- **§T7.2 (P2)** Inject time and generated identifiers where tests must pin them; avoid hidden wall-clock calls inside rules.
- **§T7.3 (P3)** Production code must not detect test execution or relax path, gate or read-only checks when tests run.

## §T8. Commands and evidence
- **§T8.1 (P2)** Run `make test`, `make lint`, `make check` and `make coverage` from the repository root; they run in isolated containers. Do not substitute host Python or Node runs as gate evidence.
- **§T8.2 (P2)** The integrated gate is `make check` plus `make coverage` on the settled change; do not claim either passed unless it was run against that revision.
- **§T8.3 (P2)** Record the tested revision, the exact command and the coverage report path with every coverage claim.
