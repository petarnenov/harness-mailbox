# harness-mailbox dashboard stack reference

Verified from repository files on 2026-11-23. Recheck `Makefile`, `scripts/workflow.sh`, Dockerfiles and `compose.yaml` when they change.

## Repository and architecture

- Built-in dashboard component of the harness-mailbox coordination workspace; Git checkout scoped to `dashboard/` (remote `github.com/petarnenov/harness-mailbox`, default branch `main`). The parent mailbox directory is not a Git repository and its records never enter this one.
- Backend: Python 3.13 standard library only (`python:3.13-slim`). `server.py` is a read-only `ThreadingHTTPServer` serving `/api/dashboard`, `/api/factory`, `/healthz` and the allowlisted static files (`index.html`, `app.js`, `theme.js`, `style.css`, `factory.js` and the three vendored libraries); mutating methods return `405 read_only`. `factory_floor.py` projects the factory animation (lanes, boxes, events) for `/api/factory`. `dependency_graph.py` builds the shared dependency graph; `delivery_process.py` projects delivery stages; `dispatch_gate.py` is the orchestrator's dependency-gate CLI (`check`, `dispatch`, `validate`) and is not exposed over HTTP.
- Frontend: buildless vanilla JavaScript, HTML and CSS in `static/` (`app.js`, `theme.js`, `factory.js`, `index.html`, `style.css`); CSP forbids inline script. `static/vendor/` holds the libraries of the Factory tab, vendored byte for byte and recorded in `static/vendor/VENDOR.md`: PixiJS 8.23.0 with its `unsafe-eval` module (MIT) and GSAP 3.15.1 (GSAP standard licence, not MIT). `factory.js` loads them from the dashboard itself when the tab is first opened.
- Workspace template: `workspace/` ships the interactive `team` menu and the launchers and Copilot extension in `copilot-mailbox/`. It also tracks the mailbox `AGENTS.md`/`CLAUDE.md`, templates and the `mailbox-orchestrator`/`mailbox-register-sessions` skills, including Python scripts (`dispatch_task.py`, `jira_sync.py`, `aio_sync.py`, `reconcile_sessions.py`, …) with `test_*.py` beside them. `workspace.Makefile` becomes the parent's forwarding `Makefile`.
- Demo data: `demo/` holds synthetic mailbox records for `make demo` and `?mode=demo`.
- Runtime: one container from `Dockerfile`, published by `compose.yaml` on `127.0.0.1:17171`, read-only root filesystem, tmpfs `/tmp`, `cap_drop: ALL`, `no-new-privileges`, non-root user `65534`, mailbox mounted read-only at `/data` from `MAILBOX_ROOT` in the ignored `.env`.
- Authority: `README.md` for dashboard behavior; the workspace `AGENTS.md` and `mailbox-orchestrator` references (`delivery-protocol.md`, `dependency-gate.md`, `host-adapters.md`) for the mailbox protocol, Jira/AIO write limits and gates.

## Commands

Run from the repository root (`dashboard/`); the parent mailbox's `Makefile` forwards the same targets.

| Purpose | Command | Evidence |
| --- | --- | --- |
| Install team and prepare the dashboard | `make setup` | installs missing system dependencies, deploys the launcher to the selected mailbox and builds the dashboard |
| Diagnose the installation | `make doctor` | read-only checks for the launcher, PATH, runtime, registry and Docker |
| Check Docker alone | `make doctor-deps` | checks Docker CLI, Compose and daemon |
| Build / start / restart | `make build` / `make up` / `make restart` | dashboard at http://localhost:17171 |
| Health / status / logs | `make health` / `make status` / `make logs` | `/healthz` probe |
| Demo | `make demo` | synthetic data, `.env` unchanged |
| Tools image | `make tools` | isolated Python test and coverage environment |
| Tests | `make test` | `unittest` for `tests/` and the orchestrator scripts, then `node --test tests/js/*.test.mjs`; networking disabled, source mounted read-only |
| Lint | `make lint` | `sh -n` on scripts, `node --check` on `static/app.js`, `static/theme.js` and `static/factory.js` |
| Tests + lint | `make check` | integrated gate |
| Coverage | `make coverage` | Python branch coverage for `server`, `dependency_graph`, `dispatch_gate`, `delivery_process`, `factory_floor` and the orchestrator scripts; reports in `.coverage-output/` (`coverage.json`, `html/`) |

Do not substitute host Python or Node runs as gate evidence; local installations are not required. Only one Make target writing `.coverage-output/` or building images should run at a time.

## Tests and coverage

- Python: standard-library `unittest`; fixtures are temporary mailbox directories. `requirements-dev.txt` pins `coverage==7.17.3` for the tools image.
- JavaScript: `node --test` with `node:vm` loading `static/app.js` or `static/factory.js` against inert DOM stubs (`node:22-alpine`); the factory tests use recording stand-ins for PixiJS and GSAP.
- Every touched production module must reach at least 85% measured line coverage; report per-module figures from `.coverage-output/coverage.json`.
- TODO(target): no Make target measures JavaScript coverage. Until one exists, record a `node --test --experimental-test-coverage tests/js/*.test.mjs` run in the isolated Node image as evidence, or state `unavailable`.

The [Team setup workflow](../../../../.github/workflows/team-setup.yml) runs setup, workflow and launcher regressions on macOS and Ubuntu for pushes and pull requests. Its Windows job exercises PowerShell registration and diagnostics. Full interactive WSL 2 sessions are outside that job.

## Safety invariants

- The server never writes mailbox data, never calls Jira, AIO or other networks and holds no credentials.
- Guarded reads reject symlinks, paths outside the root, files over 4 MB and non-finite JSON numbers; failures become warnings.
- The dispatch gate denies unknown, unmet, cyclic or malformed prerequisites and stale or tampered receipts; there is no bypass.
- Mailbox records are published atomically with UUID identity and UTC timestamps and are never overwritten.
- Only the orchestrator's `jira_sync.py` and `aio_sync.py` may write externally, limited to the events in the workspace `AGENTS.md`; the token file stays outside Git and is never printed.
- `.env`, coverage output, virtual environments, `.claude/goal-runs/` and all mailbox record directories stay ignored.

## Unknowns to preserve

No release process or issue-tracker project is configured for this repository. Do not invent CI commands, ticket keys or deployment targets; record `TODO(target)` where a task needs one.
