# Harness mailbox

The dashboard is the built-in web interface for the `harness-mailbox` coordination workspace. It displays local project, Epic, issue and task snapshots at **http://localhost:17171**. Python standard-library backend, buildless HTML/CSS/JavaScript frontend, one Docker container. No runtime package dependencies and no Jira connection or writes.

## Part of the mailbox workspace

The dashboard is a component of **harness-mailbox**, not a separately managed project. Do not add it to `projects.json` or create a project/session registration for it. The registry lists the projects whose work the mailbox coordinates; the dashboard displays their records.

In an existing mailbox workspace, keep the dashboard code under `dashboard/`:

```text
harness-mailbox/
├── README.md
├── projects.json
├── projects/
├── epics/
├── tasks/
├── sessions/
├── inbox/
├── outbox/
├── archive/
└── dashboard/          # this component's source checkout
    ├── server.py
    ├── static/
    ├── tests/
    ├── demo/
    ├── compose.yaml
    ├── Makefile
    ├── scripts/
    └── .env            # local configuration, ignored by Git
```

Run `make setup` from `dashboard/`; it detects the mailbox by its `projects.json`, in the checkout itself or else in the parent directory. You can also set `MAILBOX_ROOT` explicitly. The source checkout is scoped to the component so private coordination records remain outside the published files. Runtime data stays local and is mounted read-only.

## Start with Make

`make setup` installs missing system tools and prepares the project. On **macOS**, it finds or bootstraps [Homebrew](https://docs.brew.sh/Installation), installs Make and [Docker Desktop](https://formulae.brew.sh/cask/docker-desktop) as needed, and starts Docker Desktop. On **Linux** with `apt-get` or `dnf`, it installs missing Make/curl and uses the [official Docker development installer](https://docs.docker.com/engine/install/ubuntu/#install-using-the-convenience-script) when Docker is absent. If an existing engine only lacks Compose, it installs the [Compose CLI plugin](https://docs.docker.com/compose/install/linux/#install-the-plugin-manually) for the current user without replacing the engine. Existing working dependencies are preserved.

If Make is not installed yet, bootstrap it from the component directory first:

```sh
sh scripts/install-system.sh
# Homebrew exposes GNU Make as make through its gnubin directory.
export PATH="/opt/homebrew/opt/make/libexec/gnubin:/usr/local/opt/make/libexec/gnubin:$PATH"
make setup
```

System installation may request administrator authentication. Docker Desktop may require you to finish its first-run prompts; setup waits for readiness for up to 120 seconds and reports an actionable error if it cannot proceed. On Linux, an existing engine must be accessible to your account; setup does not change socket permissions or grant group access. Unsupported systems fail explicitly; use WSL2 on Windows. Python, Node and coverage are installed in isolated container environments; local Python/Node installations are unnecessary. `make doctor` only checks and never invokes the installer.

From an existing mailbox workspace, install the component if `dashboard/` is absent:

```sh
git clone https://github.com/petarnenov/harness-mailbox.git dashboard
cd dashboard
make setup
make doctor
make up
```

If the component is already present, run the same Make commands there. Setup detects the mailbox's `projects.json` in the checkout or its parent, creates `.env` only when absent, and installs a forwarding `Makefile` in the parent workspace if it has none. After setup, all commands also work directly from the mailbox root. Existing `.env` and root `Makefile` files are preserved.

For a mailbox at another location, provide its absolute path:

```sh
MAILBOX_ROOT=/absolute/path/to/mailbox make setup
MAILBOX_ROOT=/absolute/path/to/mailbox make up
```

An explicitly supplied `MAILBOX_ROOT` overrides `.env` for that invocation. Setup saves the selected path when creating a new `.env`; it preserves any existing `.env`, so keep supplying an override or edit that file if you intend to change its saved location. Spaces, dollar signs and hashes in paths are supported; quote paths when setting environment variables.

Open **http://localhost:17171**. The service binds only to `127.0.0.1`, runs as an unprivileged user with a read-only root filesystem, and mounts mailbox data at `/data:ro`.

| Command | Purpose |
| --- | --- |
| `make` / `make help` | List commands |
| `make setup` | Install missing system tools, prepare configuration and container dependencies |
| `make doctor` | Check Docker CLI, Compose, daemon and mailbox configuration without changes |
| `make doctor-deps` | Check the Docker prerequisites without requiring mailbox configuration |
| `make up` / `make start` | Build current source, start, and wait for a healthy dashboard |
| `make down` / `make stop` | Stop the mailbox dashboard |
| `make restart` | Rebuild and recreate the dashboard, then wait for health |
| `make build` | Build the dashboard image |
| `make status` | Show container status |
| `make logs` | Follow logs; Ctrl-C detaches |
| `make health` | Check the running dashboard's HTTP health endpoint |
| `make demo` | Start with synthetic data for this invocation; preserve `.env` |
| `make test` | Build the tools image and run tests in an isolated container |
| `make lint` | Check JavaScript and shell syntax |
| `make check` | Run tests and syntax checks |
| `make coverage` | Run tests with coverage; save reports under `.coverage-output/` |

`make demo` uses the same port and replaces the running dashboard's data mount with the shipped synthetic fixture. Select **Explore demo** for its explicitly labeled demo view. Run `make up` to restore the configured live mount. It never writes or copies demo data into the mailbox.

Setup and initial image builds/downloads need internet access; Docker checks do not pull images. Tests and syntax checks run with networking disabled and the component source mounted read-only. Coverage writes only to the separate `.coverage-output/` directory, which is excluded from Git and Docker build context. Runtime project records, `.env`, session records and local evidence are never part of published source.

Refresh reads current files; no background watcher or Jira synchronization is installed. `make down` stops the dashboard without changing mailbox data. Port conflicts are reported; unrelated services are not stopped.

Dark theme is the default on a fresh visit. The header theme toggle offers light mode and remembers an explicit choice locally; a blocking head script selects it before the stylesheet paints.

The sticky header contains five page tabs: **Overview**, **Dependencies**, **Tasks**, **Estimates**, and **Sources**. Only the active page is displayed. The shared **Team** filter offers **All teams**, **BE**, **FE**, **QA**, and **Unassigned** on every page. It combines with Project and Epic and remains selected across tabs and data refreshes. URLs such as `/#dependencies` are shareable local deep links; browser back/forward restores the selected page. Arrow keys, Home and End navigate tabs. On narrow screens only the tab strip scrolls horizontally.

## What the dashboard shows

- Every registry project, plus unregistered projects discovered in project snapshots, task directories, issue project mappings, and aggregate per-project statistics.
- All available Epic/issue snapshots, local tasks even without Jira mapping, and aggregate-only reported statistics with provenance.
- Separate **Hierarchy** (Project → Epic → issue → subtask → local task) and **Execution prerequisites** views. Execution arrows run prerequisite → dependent across multiple levels. Cycles, unresolved dependencies and nodes outside the selected filter stay visible. Long graphs scroll within the map.
- Colored completion markers distinguish **Verified complete** (green with a check), work in progress, blocked work and unverified completion. Unmet prerequisites are shown separately. A completed prerequisite does not complete the dependent; no recorded prerequisites does not prove readiness to start.
- Unique verified issue progress, separate Jira/local statuses, project allocations, total modeled and remaining effort, explicit estimates and known measured time.
- Empty scope remains **unavailable**, not 0% or 100%. No live data is invented. The checkbox **Explore demo** switches to isolated fixture files shipped in `demo/`; a prominent banner distinguishes them. It never writes or copies demo data to the mailbox.

## Agent compatibility

The dashboard is independent of the agent host. Codex, Claude Code, GitHub Copilot or another local tool can produce the same JSON snapshots using the contract below; the application only reads files. The workspace skills define discovery and message delivery for each host. The displayed agent effort model is a planning convention, not a connection to an agent or billing API.

The workspace template includes two Copilot CLI profiles. Install `workspace/.github/agents/mailbox-*.agent.md` under the mailbox's `.github/agents/` alongside the standard workspace skills. Follow the [two-terminal Copilot walkthrough](workspace/.agents/skills/mailbox-orchestrator/references/host-adapters.md#starting-copilot-cli) to start both roles, register the exact worker session and verify a read-only task through its receipts and reply.

## Delivery package

Changes to this repository go through the `da.loop` delivery skill: `/da.loop <goal>` in Claude Code and Copilot, `$da.loop <goal>` in Codex. `da.commit`, `da.merge <branch>` and `da.sync <project path>` cover commit triage, merges and replication; `da.jira.comment`, `da.jira.estimate.hh` and `da.jira.retro` need an explicit ticket or board and keep their write-confirmation gates. The mailbox workspace's Jira policy still applies to work coordinated through the mailbox.

Canonical skills live in `.claude/skills/da*` and agents in `.claude/agents/da-*.md`; `.agents/skills/da*` holds one relative symlink per skill for Codex. This is a deliberate exception to the workspace convention that `.agents/skills/` is the single source: the package's sync and renderer scripts require the `.claude/` layout. After editing a canonical agent, run `node .claude/skills/da.loop/scripts/render-agents.mjs` to update the Codex and Copilot adapters. The rulebooks are `.claude/skills/da-python-conventions/SKILL.md` and `.claude/skills/da-python-test-conventions/SKILL.md`; commands are in `.claude/skills/da.loop/references/stack.md`. The Codex G1 hook in `.codex/hooks.json` needs project trust through `/hooks`; review `.claude/skills/da.loop/scripts/g1-spec-review-hook.mjs` first. Run history under `.claude/goal-runs/` stays local.

## Snapshot contract

The backend reads only these JSON paths, with a 4 MB per-file bound. Paths outside the data root and symlink files are rejected. Malformed records produce data-quality warnings; valid sources continue to display. Unknown fields are not used to infer completion. Writers should publish snapshots atomically as required by the mailbox protocol.

`projects.json`:

```json
{"projects":[{"id":"backend","name":"Backend"}]}
```

`projects/<id>.json`: project metadata plus optional `statistics`, `metrics`, `progress`, `totals`, `counts`, `agent_estimate_hours` (`low`, `likely`, `high`; `codex_estimate_hours` is accepted as its original name) and `actual_machine_hours`. Aggregate stats are displayed as **reported**, never substituted for evidence-based issue metrics. `generated_at` or `updated_at` supplies provenance time.

`epics/<key>/scope.json`:

```json
{
  "epic_key":"EXAMPLE-1",
  "title":"An imported Epic",
  "enumeration_complete":true,
  "issue_keys":["EXAMPLE-2"],
  "external_dependencies":["OUTSIDE-1"],
  "latest_sync_at":"2026-11-15T00:00:00Z"
}
```

`epics/<key>/issues.json` accepts an array, `{"issues": [...]}`, or an object keyed by issue key. An issue:

```json
{
  "key":"EXAMPLE-2", "title":"Work item", "projects":["backend"],
  "source_status":"In Progress", "local_status":"in_progress",
  "task_ids":["task-uuid"], "depends_on":["OUTSIDE-1"],
  "verification":{"outcome":"passed"}, "evidence":["local/artifact/path"],
  "agent_estimate_hours":{"low":1,"likely":2,"high":4},
  "actual_machine_hours":1.3,
  "estimate":{"value":8,"unit":"hours"}
}
```

`estimate` is the Jira original estimate. Each task card shows it for the task's linked issues, next to the hours the agents worked on the issue so far; the issue cards, the issue table, the issue detail panel and the dependency map nodes show the same two numbers. Those hours come from `jira-sync/worked-so-far.json`, which the orchestrator writes with `jira_sync.py worked` on its own machine; the dashboard cannot read session transcripts, so the card also shows when they were measured. A parent issue has no worked hours of its own.

Aliases: `issue_key`, `summary`, `affected_projects`, `local_task_ids`, `jira_status`, `verification_outcome`, `acceptance_criterion_evidence` / `acceptance_criteria_evidence`. `dependencies` is accepted as a string array equivalent to `depends_on`. Scope aliases: `key`, `scope_complete`, `in_scope_issue_keys`. External dependencies can also be objects with a `key`; these are Epic-level relationships, not automatically in-scope issues.

Issue hierarchy uses `parent_key` or `parent: {"key":"STORY-1"}`. Keep the immediate story parent for subtasks. Jira `issuelinks` or normalized `jira_links` preserve **Blocks** direction: an inward issue is a prerequisite of the current issue; an outward issue is its dependent. Normalized links use `{"type":"Blocks","inward":"OTHER-1"}` or `{"type":"Blocks","outward":"OTHER-1"}`. Split and relates links never become prerequisites. Epic external objects may include `relationship: {"name":"Blocks"}` and `direction: "inward" | "outward"`; missing direction is contextual, not an inferred blocking arrow.

Optional `epics/<key>/dependencies.json` records explicitly reviewed delivery milestones without editing worker-owned tasks:

```json
{
  "nodes": [{
    "id":"milestone:approved-release", "kind":"milestone", "label":"Approved library release",
    "projects":["producer"], "teams":["BE"], "status":"unknown",
    "verification":"not_verified", "evidence":["reports/release-readiness.json"]
  }],
  "edges": [{
    "from":"milestone:approved-release", "to":"task:consumer-task-id", "kind":"requires",
    "condition":"Use an approved immutable release with compatibility evidence.",
    "evidence":["decisions/release-gate.json"]
  }]
}
```

Graph IDs are prefixed `project:`, `epic:`, `issue:`, `task:` or `milestone:`. Extra node declarations are milestones and cannot overwrite native records. Milestone completion requires `status: "verified"`, `verification: "passed"` and nonempty evidence; an evidence reference describing a blocker alone is insufficient. The dashboard displays recorded assertions; it does not open evidence or verify artifacts itself. Task completion still requires Done plus evidence; issue completion still requires passing local acceptance verification. Jira Closed never proves local completion. Blocker prose and task titles do not automatically create dependencies.

Local tasks display a distinct short local ID, their own title and a separate Epic scope or linked Jira association. Jira records retain their actual key and issue type. The default compact map folds local work into expandable Current local work, Local work history and Other local work / audit panels. Current work requires a recorded active owning Jira status; History requires verified local completion evidence and exposes full IDs, sources and results. Unverified prerequisite context remains visible in the compact map. Show all local work restores all selected local nodes. Completing a local task does not establish completion of its Jira owner.

Project, Epic and Team filters combine in both views. Prerequisite ancestors and immediate downstream neighbors outside the selection appear as labeled context and do not affect totals. Full conditions, completion status, sources and evidence remain readable below the graph. A production build configuration is not evidence that a feature has been deployed to production.

`tasks/<project>/<uuid>.json`: `id`, `project` (defaults to directory name), `title`, `status`, optional `issue_key`, `epic_key`, `depends_on` task IDs, `evidence`, `updated_at`. Task `done` plus nonempty evidence marks evidenced completion. A task alone does not establish an issue's acceptance criteria.

`epics/<key>/progress.json`: preserved reported fields `metrics`, `statistics`, `progress`, `totals`, `counts`, `percent`, status counts, scope completeness, `estimate`, `machine_hours`. Nested project aggregates may use `per_project`, `project_metrics`, or `projects` as an object keyed by project ID, or a list of objects with `project` / `id`. Project snapshots support the same fields. No estimate is inferred from story points or ordinary Jira hours.

### Team classification and filtering

Projects, issues and tasks may declare `"team":"FE"` or `"teams":["BE","FE"]`. Accepted aliases include backend/back-end, frontend/front-end and quality assurance (case-insensitive). Explicit metadata overrides inferred classification; an explicit empty list remains unassigned. Duplicate issue snapshots combine team membership.

Without metadata, titles mentioning test, tests, testing, QA or quality assurance belong to **QA**, including development test tasks. Otherwise prefixes UI/FE/Frontend/Front-end belong to **FE** and DEV/BE/Backend/Back-end to **BE**. Testing takes precedence over development prefixes. Tasks with no title classification inherit their linked issue team. Remaining records use project ownership: `frontend` is FE; `geowealth`, `admin-center`, `identity-service` and `user-context` are BE. Other projects remain unassigned unless configured with explicit team metadata. This is a display inference, not an update to source snapshots or Jira.

Team selection filters issues, tasks, related Epics, project cards, graph and modeled estimates together. QA work remains visible within a BE-owned project. Cross-team issues count once in the selected total; project estimate allocation retains its original membership split. Dependencies outside the selection remain labeled boundary nodes. Whole-project reported estimates/actuals on project cards are hidden under a team filter. Aggregate snapshots are also hidden unless a snapshot explicitly declares matching team metadata. Data-quality warnings cover all sources, and the hypothetical calculator remains an independent all-team scenario.

### Verification and scope

An issue is verified only if:

1. `local_status` is `verified_done`;
2. verification is `passed`, `pass` or `verified` and evidence is nonempty;
3. every linked required local task exists, is `done`, and has evidence.

Otherwise `verified_done` is displayed as awaiting verification. An issue with `local_status` `deferred` stays in scope and in the completion denominator, but its process stage is `deferred` instead of being derived from linked tasks, and it is not listed as upcoming work. Evidence references are treated as recorded assertions, not opened or executed by the dashboard; the orchestrator owns validating the underlying artifacts. Contradictory duplicate statuses, estimates or measurements produce a warning and suppress verified completion/explicit estimates/measurements until reconciled. Duplicate issues merge memberships and dependencies and count only once overall.

Progress = verified unique issues / known unique issues, only when scope enumeration is explicitly complete. Missing declared issue records remain visible as placeholders and invalidate scope completeness. Zero issues has no percentage. Overall completeness requires all imported Epics complete; unknown or unimported future scope is not invented. Reported aggregate figures are never merged into this denominator. Project memberships may overlap; do not add project issue counts into the portfolio total.

### Local execution and issue reconciliation

The Tasks page shows **Epic work & issue links** above **Issues & verification**. The summary and task execution counts follow Team/Project/Epic filters. Expand an issue's **Verification & linked work** for required task records, issue-wide verification reason, missing records, timestamp and source path. Recorded issue status and filtered task execution are distinct: activity can exist while the recorded status remains Pending. Verification always evaluates all required tasks, including those outside the current filter.

Links are resolved in both directions before checking completion: issue `task_ids` and `local_task_ids` are combined with task `issue_key` and optional `issue_keys` (a string array for work spanning several issues). Duplicate issue snapshots contribute all their links. A task is counted once per issue and once in the local task summary. Reverse-only unfinished tasks prevent verified completion. Forward-only issue links are also displayed on the task.

Writer contract: set `epic_key` to the Epic; use `issue_key` or `issue_keys` only for the actual issues whose requirements the task contributes to. Record concrete task IDs on those issues when known. Epic preparation without a specific issue allocation must remain Epic-level; the dashboard recognizes legacy `issue_key` equal to a known Epic as Epic-level work, unless that key is also an imported issue. Epic work is never assigned to every child. Unknown keys appear as unresolved mappings and source warnings. Tasks without mappings remain visible separately. The dashboard does not infer mappings from titles/project membership or modify mailbox files.

A completed analysis/design/setup task proves only its stated scope. Update issue `local_status`, criterion evidence and verification only after the orchestrator assesses the work; task completion alone never promotes an issue to Verified done. Source warnings expose missing referenced tasks and unknown keys, while issue rows explicitly show zero task links. Refresh reads these current local records; it does not fetch Jira or accept verification.

### Agent estimates

The editable model uses **low / likely / high hours per issue × review multiplier** (defaults 0.5 / 1.5 / 4 × 1.25). These are illustrative assumptions, not benchmark measurements or calibrated forecasts. Explicit `agent_estimate_hours` (or its original name `codex_estimate_hours`) override the model for an issue. Shared issue estimates are allocated equally across affected projects; unmapped issues remain in the overall total. Low/likely/high are scenario bounds, not statistical confidence intervals.

Only known enumerated scope is modeled. Incomplete scope is labeled a known subset; task-only and aggregate-only projects have no inferred whole-project estimate. Explicit reported project estimates are displayed separately and are not added to issue estimates. The hypothetical issue-count calculator works before scope import and is clearly excluded from live statistics. Remaining estimates omit verified issues. Blocked/in-progress issues retain full modeled effort; there is no invented fractional completion.

`actual_machine_hours` must be explicitly recorded measurements. Unknown values remain unknown; a known subset sum includes a measurement coverage count and is never described as total actual time. Machine-hours sum agent execution time, so multiple parallel agents can exceed elapsed wall-clock hours. No token-to-time, message timestamp, or Jira-hour conversion is used. The Jira worklog the orchestrator writes when an issue closes (user instruction, 2026-11-22) is computed separately by `jira_sync.py` from the agents' session transcripts between its applied In Progress and Closed updates; the dashboard neither reads nor writes it.

## API

- `GET /healthz` → `{"status":"ok"}` (process health, not a claim that mailbox data is valid).
- `GET /api/dashboard?mode=live` → `generated_at`, `mode`, `warnings`, `projects`, `epics`, `issues`, `tasks`, `dependencies`, `task_dependencies`, `external_dependencies`, `graph`, `reported_statistics`, `totals`, `estimate_method`. `graph.nodes` holds typed identities, filter metadata, recorded status, completion `ready` boolean, sources and `unmet_prerequisites`. Here `ready` means verified completion evidence, **not** readiness to start. `graph.edges` separates `membership` from `requires` and retains direction, conditions and evidence.
- Optional numeric query fields: `low`, `likely`, `high`, `review_multiplier`. Require `0 ≤ low ≤ likely ≤ high ≤ 1000` and `1 ≤ review_multiplier ≤ 1000`.
- `mode=demo` selects the isolated demo fixture. Team/Project/Epic filtering is browser-local; API returns the full snapshot.
- Errors: `{"error":{"code":"...","message":"..."}}`. Mutations return 405. Static assets are allowlisted. No arbitrary file-serving endpoint.

## Validation

The mailbox orchestrator's optional local CLI `dispatch_gate.py` reuses this graph projection for dependency eligibility and immutable execution receipts. It is not an HTTP mutation endpoint and is not invoked by the read-only web server. Its wrapper and mandatory worker protocol live in the containing mailbox workspace; no mailbox snapshots are packaged into the application repository. Run its tests with the normal test/coverage workflow.

```sh
make setup
make doctor
make check
make coverage
make up
make health
```

Tests use temporary directories and synthetic data; they never mutate live mailbox state. Workflow tests cover dependency/configuration failures, environment precedence, configuration preservation, setup repeatability and demo isolation. Browser verification covers filters, model edits, cycles, unresolved nodes, narrow viewport, source warnings and safe text rendering. Local development evidence is excluded from the public repository.

## License

[MIT](LICENSE) — copyright 2026 Petar Nenov.


### Implementation process orientation

The Overview adds process-stage counts, Now / Blocked / Next triage, a verification queue and changes since the previous comparable browser snapshot. Jira keys open a shared detail panel with the recorded baseline, remaining work, explicit owner, original acceptance criteria, per-criterion evidence and revision trace. Missing ownership, criterion coverage and revision evidence remain unknown. The source Jira status stays independent from the local process stage and blockers.

Process stages are `uninvestigated`, `investigating`, `ready`, `implementation`, `review`, `verification`, `complete` and `unknown`. A partial baseline means an initial assessment exists and investigation is incomplete; a reported complete baseline is a verification candidate. Only the existing strict issue verification permits Complete. Ready and Next are recorded stages/suggestions, not permission to execute: dependency, ownership, loop and quota checks still apply.

Search combines with Project / Epic / Team. The URL retains filters, search, view, graph mode and focused issue; unavailable values must be explicitly reported. Graph focus uses canonical relationships and keeps external context separate from scoped totals. Freshness labels distinguish source Jira updates, worker reports (or explicitly labeled linked-task update proxies), evidence times and dashboard read time; missing, invalid and future timestamps do not imply freshness or worker availability.

Changes are browser-local comparisons, separated by live/demo mode and comparable filter/search scope. They report observed completion, stage, blocker and scope changes with previous metadata for removals. The first visit has no comparison baseline. Storage failures and incompatible data are explicit; opening details or changing graph presentation does not consume a comparison. The server and mailbox stay read-only.


Issue records may provide an explicit `delivery` object. Supported fields include `stage`, `existing_work`, `remainder`, `owner`, `next_action`, `criteria` (`text`, `status`, `evidence`), `verification` (`verifier`, `evidence`, `reason`), `trace` and `freshness`. Trace is keyed by `written`, `reviewed`, `integrated` and `tested_integrated`; each entry supplies `revision`, `evidence` and optional `at`. Integrated verification requires a recorded integrated revision and a matching tested revision, both with evidence. A trace claim never changes issue verification or proves deployment. Structured criterion results require their own evidence to be displayed as passed; original acceptance text remains available when structured coverage is absent.

The projection also preserves existing `baseline_status`, `baseline_assessment`, `acceptance_criteria` and explicit ownership/verification timestamps. Source metadata conflicts remain visible instead of selecting the most advanced claim. Producers should record actual outcomes and artifact references in their owned coordination records; the dashboard does not invent or write those records.

Process-data conflicts are returned in `delivery_warnings`, separately from authoritative graph warnings; they do not grant or remove dispatch eligibility. Changes to the display helper still invalidate old dispatch receipts through the code fingerprint.


### Ownership roles

Jira assignee, implementation owner and recorded execution owner are separate roles. An explicitly fetched `jira_assignee: null` means **Unassigned in Jira**; an absent assignee field means **Not loaded**, while malformed or contradictory ownership metadata means **Conflicting records**. The assignee retrieval timestamp and source describe this field snapshot independently from the issue status/acceptance snapshot. Jira ownership is never inferred from an Epic parent, a local agent or affected projects.

`implementation_owner` identifies the explicitly recorded implementation responsibility. Execution ownership comes from linked local tasks' `owner` or `owner_session_id`, with task/project/source context. Multiple recorded workers remain inspectable; a recorded session ID is not proof that the worker is currently active. Missing local responsibility is **Not recorded**, rather than a claim that nobody owns the Jira issue. Legacy local `owner` records remain local ownership and never become a Jira assignee.

Local enrichment may fetch only ownership fields read-only from Jira and preserve the imported issue scope, statuses, parents and acceptance criteria. Such a partial refresh does not make the full Jira snapshot newer and never changes Jira assignment.

Issue detail prioritizes the process summary, next action and compact ownership cards. Recorded sources, observations and full worker identities remain available in expandable audit sections. Existing and remaining work are visually separated; presentation does not change recorded stages, assignments or completion evidence.

### Acceptance criteria presentation

Issue details display acceptance criteria as numbered cards with explicit recorded outcomes and expandable evidence references. The coverage counter counts criteria with recorded evidence, independently of their outcomes; it is not a completion percentage. Green outcome styling requires a recorded passing outcome and evidence. Failed or blocked outcomes receive warning styling; other outcomes remain neutral.

When structured criteria are unavailable, the original text is displayed as source scenarios split at numbered headings (including `1:`, `1.` and `1)`), with blank-line or bullet boundaries as a fallback. These excerpts are labeled **Not assessed** and do not establish criterion coverage. When structured criteria exist, the original acceptance text remains available in an expandable section. Missing criteria and missing evidence are explicit. The cards support light/dark themes and narrow screens, and preserve the dashboard's read-only verification semantics.

Scenario cards distinguish the title and Given / When / Then / And / But steps with compact labeled rows. Source preambles and other text remain visible; textual splitting is presentation only and does not create structured verification records.
