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

## Install and run team

From the root of a fresh checkout, run:

```sh
make setup
```

Open a new terminal and type `team` from any directory. To verify the installation, run `make doctor` from the checkout. You can also run it before setup to see what is missing.

| Platform | Where to run `make setup` and `make doctor` | Where to run `team` |
| --- | --- | --- |
| macOS | Terminal | A new terminal |
| Linux (apt or dnf) | Your shell | A new terminal |
| Windows | A WSL 2 Linux shell, in the checkout | PowerShell, Command Prompt, or the installing WSL distribution |

On Windows, clone the repository **inside the WSL distribution** (for example, under `~/src`). Keep Windows interoperability enabled. If you use Docker Desktop, enable WSL integration for that distribution. On a clean apt- or dnf-based WSL distribution, setup can install Docker Engine directly using systemd. Setup registers the Windows command for your user and binds it to the Linux account and distribution that ran setup.

For a standalone checkout, setup creates a private `mailbox-data/` directory with an empty project registry. It installs the launcher and missing runtime dependencies, including Python 3.8+ with curses and tmux, and builds the dashboard. Install and sign in to the harness CLIs you plan to use in the same environment as the mailbox. [Run a team](#run-a-team)

## System dependencies

On **macOS**, setup uses Homebrew to install missing prerequisites and starts Docker Desktop when needed. On **Linux** with apt or dnf, setup installs missing prerequisites and can install Docker Engine and the Compose plugin. On all three platforms, doctor checks the launcher, its shell PATH, the team runtime, Docker and whether the team menu can read the configured project registry. On Windows it also validates the PowerShell/Command Prompt launcher and its registered user PATH.

If Make is missing, run `sh scripts/install-system.sh` from the component directory. On macOS, make the Homebrew version available in the current shell before continuing.

```sh
export PATH="/opt/homebrew/opt/make/libexec/gnubin:/usr/local/opt/make/libexec/gnubin:$PATH"
```

Then run `make setup`.

System installation may request administrator authentication. Docker Desktop may require you to finish its first-run prompts; setup waits for readiness for up to 120 seconds and reports an actionable error if it cannot proceed. On Linux, an existing engine must be accessible to your account; setup does not change socket permissions or grant group access. The team menu uses local Python. Dashboard development tests and coverage run inside containers. `make doctor` only checks and never invokes the installer.

From an existing mailbox workspace, install the component if `dashboard/` is absent:

```sh
git clone https://github.com/petarnenov/harness-mailbox.git dashboard
cd dashboard
make setup
```

Open a new terminal after installation. Use `make doctor` to verify setup, `team` to open the menu, or `make up` to start the dashboard independently.

If the component is already present, run the same Make commands there. Setup detects the mailbox's `projects.json` in the checkout or its parent, creates `.env` only when absent, and installs a forwarding `Makefile` in the parent workspace if it has none. In a mailbox with this forwarding Makefile, the same Make targets work from the mailbox root. Existing `.env` and root `Makefile` files are preserved.

For a mailbox at another location, provide its absolute path:

```sh
MAILBOX_ROOT=/absolute/path/to/mailbox make setup
MAILBOX_ROOT=/absolute/path/to/mailbox make up
```

An explicitly supplied `MAILBOX_ROOT` overrides `.env` for that invocation. Setup saves the selected path when creating a new `.env`; it preserves any existing `.env`, so keep supplying an override or edit that file if you intend to change its saved location. Spaces, dollar signs and hashes in paths are supported; quote paths when setting environment variables.

In a dedicated data directory such as the standard `mailbox-data/`, the dashboard runs with the non-root numeric user and group of the directory owner. Start workers with that same host account. Their private directories (`0700`) and atomically replaced records (`0600`) are immediately readable by the dashboard. Keep credentials outside this data-only mount for its entire lifetime. A standalone installation can link `.env.local` to the external `../.env.local` file. Mailboxes containing the dashboard checkout, a Git checkout at the root, or a local `.env` or `.env.local` file retain the image's unprivileged reader (`65534:65534`), as do roots owned by user or group zero. In those layouts only files readable by that account are visible. These numeric permissions assume Docker maps the bind mount's UID and GID directly.

Open **http://localhost:17171**. The service binds only to `127.0.0.1`, runs as an unprivileged user with a read-only root filesystem, and mounts mailbox data at `/data:ro`.

| Command | Purpose |
| --- | --- |
| `make` / `make help` | List commands |
| `make setup` | Install team, prepare the mailbox and build container dependencies |
| `make doctor` | Check team, PATH, runtime dependencies, Docker and mailbox configuration without changes |
| `make doctor-deps` | Check the Docker prerequisites without requiring mailbox configuration |
| `make up` / `make start` | Build current source, start, and wait for a healthy dashboard |
| `make down` / `make stop` | Stop the mailbox dashboard |
| `make restart` | Rebuild and recreate the dashboard, then wait for health |
| `make build` | Build the dashboard image |
| `make status` | Show container status |
| `make logs` | Follow logs; Ctrl-C detaches |
| `make health` | Check the running dashboard's HTTP health endpoint; a failed check prints `Dashboard health check failed.` |
| `make demo` | Start with synthetic data for this invocation; preserve `.env` |
| `make test` | Build the tools image and run tests in an isolated container |
| `make lint` | Check JavaScript and shell syntax |
| `make check` | Run tests and syntax checks |
| `make coverage` | Run tests with coverage; save reports under `.coverage-output/` |

`make demo` uses the same port and replaces the running dashboard's data mount with the shipped synthetic fixture. Select **Explore demo** for its explicitly labeled demo view. Run `make up` to restore the configured live mount. It never writes or copies demo data into the mailbox.

Setup and initial image builds/downloads need internet access; Docker checks do not pull images. Tests and syntax checks run with networking disabled and the component source mounted read-only. Coverage writes only to the separate `.coverage-output/` directory, which is excluded from Git and Docker build context. Runtime project records, `.env`, session records and local evidence are never part of published source.

Refresh reads current files; no background watcher or Jira synchronization is installed. A failed refresh keeps the last snapshot and shows `Could not refresh: <reason>.` with a fixed reason: the server could not be reached, its response could not be read, the server's own error message, or the snapshot could not be displayed. `make down` stops the dashboard without changing mailbox data. Port conflicts are reported; unrelated services are not stopped.

Dark theme is the default on a fresh visit. The header theme toggle offers light mode and remembers an explicit choice locally; a blocking head script selects it before the stylesheet paints.

The sticky header contains six page tabs: **Overview**, **Dependencies**, **Tasks**, **Estimates**, **Sources**, and **Factory**. Only the active page is displayed. The shared **Team** filter offers **All teams**, **BE**, **FE**, **QA**, and **Unassigned** on every page. It combines with Project and Epic and remains selected across tabs and data refreshes. URLs such as `/#dependencies` are shareable local deep links; browser back/forward restores the selected page. Arrow keys, Home and End navigate tabs. On narrow screens only the tab strip scrolls horizontally.

## What the dashboard shows

- Every registry project, plus unregistered projects discovered in project snapshots, task directories, issue project mappings, and aggregate per-project statistics.
- All available Epic/issue snapshots, local tasks even without Jira mapping, and aggregate-only reported statistics with provenance.
- Separate **Hierarchy** (Project → Epic → issue → subtask → local task) and **Execution prerequisites** views. Execution arrows run prerequisite → dependent across multiple levels. Cycles, unresolved dependencies and nodes outside the selected filter stay visible. Long graphs scroll within the map. Hover over a blocked card to see the recorded reason, the prerequisites it is waiting for and their recorded owners. The tooltip also indicates when a reason or blocking owner is unknown.
- Colored completion markers distinguish **Verified complete** (green with a check), work in progress, blocked work and unverified completion. Unmet prerequisites are shown separately. A completed prerequisite does not complete the dependent; no recorded prerequisites does not prove readiness to start.
- Unique verified issue progress, separate Jira/local statuses, project allocations, total modeled and remaining effort, explicit estimates and known measured time.
- The issue detail has a **Why · recorded context** section: the decisions recorded for the issue and its linked tasks with their rationale, requests with no recorded answer, the latest evidence, and the direct and inherited unmet prerequisites with their recorded owners. It is a summary of existing records; eligibility to start is decided only by the dependency gate.
- The **Factory** page shows the recorded delivery of the tasks as a factory floor seen from above. There is one conveyor lane per registered worker session and one box per task. The page opens on the current state: every box stands at the station its recorded events lead to, a task whose delivery is recorded complete is not on the floor, and nothing from the past is replayed. After **Refresh data** only the events recorded since the previous load are animated. The **Show** list replays one task, named by its title and the first eight characters of its id, with the most recently active task first. The floor then shows only that task's lane and box, and the task's recorded events play from the first one: the box enters with its first recorded delivery stage, rides through the stations `investigating`, `implementation`, `review` and `verification`, goes back from a checkpoint when a refusal is recorded (a rejected G1 or G2, a returned report), and leaves by car when the delivery is recorded complete. A finished replay stays on its last frame until **Current state** is chosen in the list, the filters leave its task out, the data no longer contains it or **Explore demo** is switched. **Pause** works only in a replay and is disabled in the current state; the speed selector works in both. The shared Team, Project and Task filters and **Find work** limit the boxes and the replay list to the tasks in scope, and while one of them is set only the lanes with such a task are drawn; **Hide completed tasks** does not apply to this page. In the current state a robot works on a box only while its worker's last recorded status is `working`. Pointing at a box shows the task's title, project and stage; pointing at a robot shows the worker session and its last recorded status. The page is computed only from existing records and changes nothing.
- Empty scope remains **unavailable**, not 0% or 100%. No live data is invented. The checkbox **Explore demo** switches to isolated fixture files shipped in `demo/`; a prominent banner distinguishes them. It never writes or copies demo data to the mailbox.

## Agent compatibility

The dashboard is independent of the agent host. Codex, Claude Code, GitHub Copilot or another local tool can produce the same JSON snapshots using the contract below; the application only reads files. The workspace skills define discovery and message delivery for each host. The displayed agent effort model is a planning convention, not a connection to an agent or billing API.

## Run a team

1. Open a terminal and type `team`. Press `A` to add a local project. In the folder browser, use Up/Down to highlight a directory, Right or Enter to open it, and Left to go to its parent. Press Space to select the highlighted folder, or choose **Select this folder** for the directory shown at the top. Check the path and confirm or edit its project ID. Press `A` again to add another project. Esc goes back.
2. On **Select projects**, use Space to select existing projects. Newly added projects are already selected. Use `+` or `-` on a project to adjust its worker count, then press Enter. Each team has one orchestrator for all selected projects and one tmux window per worker. A new project can join the team immediately. Before accepting an execution task, its worker must register and identify the project’s required loop skill.
3. Choose the orchestrator’s harness, model and reasoning effort, then configure the workers. Each role can use GitHub Copilot, Claude Code or Codex. All workers use the worker configuration. Select **Automatic selection** to use the harness default, press `R` to refresh its model catalog, or enter a model ID manually. The effort picker appears when the catalog lists reasoning levels for the model. Claude model labels show the CLI’s current display name alongside its launch ID.
4. On **Review team**, select **Start team**. Give the objective in the orchestrator window. The default tmux status bar shows the chosen orchestrator in brackets, for example `[Codex]`. For a first check, enter “Ask each selected worker to report its current branch and HEAD commit, then summarize the confirmed replies.” With the default tmux bindings, press `Ctrl-b` then `w` to visit worker windows for first-run trust or sign-in prompts.

During an active objective, a status question gets a progress update and the orchestrator continues handling pending decisions. Ask explicitly for a pause, stop or status-only report to suspend that work. While awaiting a worker event, the orchestrator checks its inbox at least once every 60 seconds unless its own session can wake on that event. Registration alone leaves it idle after inventory.

The menu opens in dark mode on first use and remembers your theme. Press `T` to switch themes or Ctrl-T while editing an ID. New participants inherit the launching shell’s provider configuration directories. Each CLI handles its own trust prompts and permissions.

### Manage projects and sessions

- **Add or remove a project.** Adding the same working directory reuses its ID. Selecting the containing Git checkout also reuses the registration when it maps unambiguously to one project. The menu groups old duplicate registrations. Press `D` or Delete on a project to review the IDs to remove. Removal also works after the project's folder was deleted or replaced; a removed ID stays bound to its original folder, so only that folder can be registered under it again. Stop its workers first. Confirmation removes the registration and preserves the project files, messages and task history. A renamed directory can reclaim its ID when the filesystem confirms its identity. If ownership cannot be verified or the directory belongs to a different project, choose a new ID.
- **Reconnect or stop workers.** Run `team` in a second terminal, including when the team uses a named tmux server. Select the orchestrator to reconnect, an individual worker to stop it, or **Stop entire team**. Review the affected sessions before confirming. **Stop entire team** also closes the windows of mailbox agents that were added to the team's tmux session, such as workers the orchestrator opened, and ends the processes those agents and the team's panes started, with a graceful stop first and a forced stop after a short wait (processes that left their pane's process tree are found only on Linux, where `/proc` exposes their environment); if a process cannot be stopped, the menu says so and refreshes the team; if the team's processes cannot be checked, the menu asks you to check for remaining agent processes. Other tmux windows stay open. Agents started before this version carry no team marker, so restart the team once to make this apply to them. Workers sharing a project have separate mailbox endpoints. Reconnection checks the original coordinator so a replacement tmux session cannot receive the connection. Older teams without recorded pane ownership offer reconnection only. A past mailbox registration can remain on disk after the process exits. Confirm that the endpoint is reachable before assigning more work.
- **Leave the team running.** Press `Q` in the management menu or `Ctrl-b` then `d` to detach from tmux. Projects added while the team is running are available for its next launch.
- **View the dashboard.** Opening `team` starts or resumes the Docker Compose dashboard for the installed command’s mailbox at **http://localhost:17171**. Reopening the menu reuses the running container when its reader identity matches the selected mailbox. An older container with a different identity is recreated once, briefly interrupting dashboard requests. If Docker is unavailable or an existing container belongs to another checkout or mailbox, the menu displays a warning and team management remains available.

Setup refreshes the launchers and Copilot mailbox extension. It supplies missing agent profiles and mailbox skills and preserves existing workspace instructions and profiles. A Copilot worker started by the launcher offers its own session for registration. The orchestrator confirms it through a correlated request and reply. Native Codex and Claude Code sessions use the mailbox and the session tools available to their host. The host adapter reference covers mixed teams, manual sessions and delivery verification. [Host adapters in the source checkout](workspace/.agents/skills/mailbox-orchestrator/references/host-adapters.md#starting-a-team)

## Delivery package

Changes to this repository go through the `da.loop` delivery skill: `/da.loop <goal>` in Claude Code and Copilot, `$da.loop <goal>` in Codex. `da.commit`, `da.merge <branch>` and `da.sync <project path>` cover commit triage, merges and replication; `da.jira.comment`, `da.jira.estimate.hh` and `da.jira.retro` need an explicit ticket or board and keep their write-confirmation gates. The mailbox workspace's Jira policy still applies to work coordinated through the mailbox.

Canonical skills live in `.claude/skills/da*` and agents in `.claude/agents/da-*.md`; `.agents/skills/da*` holds one relative symlink per skill for Codex. This is a deliberate exception to the workspace convention that `.agents/skills/` is the single source: the package's sync and renderer scripts require the `.claude/` layout. After editing a canonical agent, run `node .claude/skills/da.loop/scripts/render-agents.mjs` to update the Codex and Copilot adapters. The rulebooks are `.claude/skills/da-python-conventions/SKILL.md` and `.claude/skills/da-python-test-conventions/SKILL.md`; commands are in `.claude/skills/da.loop/references/stack.md`. The Codex G1 hook in `.codex/hooks.json` needs project trust through `/hooks`; review `.claude/skills/da.loop/scripts/g1-spec-review-hook.mjs` first. Run history under `.claude/goal-runs/` stays local.

## Snapshot contract

The backend reads only these JSON paths, with a 4 MB per-file bound. Paths outside the data root and symlink files are rejected. Malformed records produce data-quality warnings; valid sources continue to display. A read warning is `<path relative to the data root>: <reason>` with one of the fixed reasons `invalid JSON`, `unreadable file`, `symlink/outside data root`, `exceeds 4 MB limit` or `Non-finite JSON number: <literal>`; a number literal that overflows, such as `1e400` or an integer too large for a floating-point value, counts as non-finite. A `scope.json` that was read and is not a JSON object is reported as `epics/<folder>/scope.json: scope is not a JSON object`; one that could not be read gets only its read warning. In both cases that Epic's scope stays incomplete. Unknown fields are not used to infer completion. Writers should publish snapshots atomically as required by the mailbox protocol.

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

Without metadata, titles mentioning test, tests, testing, QA or quality assurance belong to **QA**, including development test tasks. Otherwise prefixes UI/FE/Frontend/Front-end belong to **FE** and DEV/BE/Backend/Back-end to **BE**. Testing takes precedence over development prefixes. Tasks with no title classification inherit their linked issue team. Remaining records use the built-in project ownership mapping. Projects outside that mapping remain unassigned unless configured with explicit team metadata. This is a display inference, not an update to source snapshots or Jira.

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

Writer contract: set `epic_key` to the Epic; use `issue_key` or `issue_keys` only for the actual issues whose requirements the task contributes to. Record concrete task IDs on those issues when known. Epic preparation without a specific issue allocation must remain Epic-level; the dashboard recognizes legacy `issue_key` equal to a known Epic as Epic-level work, unless that key is also an imported issue. Epic work is never assigned to every child. Unknown keys appear as unresolved mappings and source warnings. Tasks without mappings remain visible separately. A task `issue_key` that is neither a string nor a list, or an `epic_key` that is neither a string nor null, is ignored with the warning `<task source>: invalid issue_key ignored` or `<task source>: invalid epic_key ignored` and displayed as empty; a task `status` that is not a string is displayed as `unknown` with `<task source>: invalid task status ignored`. When several task records share one task ID, the warning `<task id>: duplicate task identity in <sources>; completion evidence ignored` is raised and none of those records counts as completion evidence. The dashboard does not infer mappings from titles/project membership or modify mailbox files.

A completed analysis/design/setup task proves only its stated scope. Update issue `local_status`, criterion evidence and verification only after the orchestrator assesses the work; task completion alone never promotes an issue to Verified done. Source warnings expose missing referenced tasks and unknown keys, while issue rows explicitly show zero task links. Refresh reads these current local records; it does not fetch Jira or accept verification.

### Agent estimates

The editable model uses **low / likely / high hours per issue × review multiplier** (defaults 0.5 / 1.5 / 4 × 1.25). These are illustrative assumptions, not benchmark measurements or calibrated forecasts. Explicit `agent_estimate_hours` (or its original name `codex_estimate_hours`) override the model for an issue. Shared issue estimates are allocated equally across affected projects; unmapped issues remain in the overall total. Low/likely/high are scenario bounds, not statistical confidence intervals.

Only known enumerated scope is modeled. Incomplete scope is labeled a known subset; task-only and aggregate-only projects have no inferred whole-project estimate. Explicit reported project estimates are displayed separately and are not added to issue estimates. The hypothetical issue-count calculator works before scope import and is clearly excluded from live statistics. Remaining estimates omit verified issues. Blocked/in-progress issues retain full modeled effort; there is no invented fractional completion.

`actual_machine_hours` must be explicitly recorded measurements. Unknown values remain unknown; a known subset sum includes a measurement coverage count and is never described as total actual time. Machine-hours sum agent execution time, so multiple parallel agents can exceed elapsed wall-clock hours. No token-to-time, message timestamp, or Jira-hour conversion is used. The Jira worklog the orchestrator writes when an issue closes (user instruction, 2026-11-22) is computed separately by `jira_sync.py` from the agents' session transcripts between its applied In Progress and Closed updates; the dashboard neither reads nor writes it.

The context route additionally reads `decisions/**/*.json`, `inbox/<route>/*.json`, `outbox/<route>/*.json`, `archive/<route>/*.json`, `reports/**/*.json`, `dispatch-gates/<task>/*.json` and `sessions/*.json` under the same guards. These directories are read only for `/api/context`; a symlinked directory is not followed. Nothing is written, and message bodies are never returned.

The factory route reads `sessions/*.json`, `deliveries/<task-id>.json`, `decisions/<task-id>/*.json` and the `status_report` messages in `inbox/<route>/*.json`, `outbox/<route>/*.json` and `archive/<route>/*.json` under the same guards. It uses a session record's `role`, `session_id`, `project`, `session_name`, `status` and `last_seen_at`, a delivery record's `history`, a decision's type, verdict and `decided_at`, and a status report's `from_session_id`, `status` and `created_at`. Nothing is written.

## API

- `GET /healthz` → `{"status":"ok"}` (process health, not a claim that mailbox data is valid).
- `GET /api/dashboard?mode=live` → `generated_at`, `mode`, `warnings`, `projects`, `epics`, `issues`, `tasks`, `dependencies`, `task_dependencies`, `external_dependencies`, `graph`, `reported_statistics`, `totals`, `estimate_method`. `graph.nodes` holds typed identities, filter metadata, recorded status, completion `ready` boolean, sources and `unmet_prerequisites`. Here `ready` means verified completion evidence, **not** readiness to start. `graph.edges` separates `membership` from `requires` and retains direction, conditions and evidence.
- `GET /api/context?node=task:<id>|issue:<key>&mode=live` → the recorded context of one task or issue as a bounded pack: `generated_at`, `mode`, `node`, `ancestors`, `unmet_prerequisites`, `decisions`, `open_requests`, `evidence`, `omitted` and `warnings`. It layers decision, message, evidence (report, gate receipt, commit) and session records on the dashboard graph through the links the records already carry (`task_id`, `issue_key`, `in_reply_to`, `request_id`, `owner_session_id`, `g1_decision`, `g2_decision`, `supersedes`, `supersedes_report`, commit SHAs and record paths in a task's `evidence`). A reference that names no readable record becomes a warning; nothing is inferred. An issue pack also covers the tasks linked to the issue. `unmet_prerequisites` holds the node's own prerequisites and those inherited from its membership ancestors (`inherited_from`), exactly as the graph records them, with the recorded owners; it does **not** list the prerequisites of prerequisites, which the dependency gate also checks, so only `dispatch_task.py check` decides eligibility. `open_requests` are messages whose `type` contains `request` and that have no recorded reply, no decision naming them and, for an `execution_request`, no allowed `validate` receipt; this reflects recorded answers only. Each list is limited (ancestors 20, unmet prerequisites 20, decisions 10, open requests 10, evidence 10, warnings 50; rationale 600 characters) and `omitted` counts what was left out. A missing, repeated or malformed `node` returns `400 invalid_request`, an id absent from the graph `404 not_found`, and a pack that cannot be produced `500 internal_error` with `Context data could not be read`.
- `GET /api/factory?mode=live` → the factory animation as `generated_at`, `mode`, `warnings`, `stations`, `lanes`, `boxes` and `events`. `stations` are the four delivery stages `investigating`, `implementation`, `review` and `verification`. A lane is a session record with `role` `worker`, a `session_id` and a `project`; its `last_status` is the newest `status_report` of that session, or `null`. A box is a task owned by a lane's session that has at least one event; a task whose record has the status `done` or `cancelled` and whose delivery history never reached `complete` gets no box and no events. Events are ordered by recorded time: `enter` (first station stage in the delivery history), `move` (a later station), `refuse` (a decision with the verdict `changes_requested`, `returned`, `rejected`, `blocked` or `evidence_accepted_criteria_failed` while the box is in the factory, with `gate` `g1`, `g2` or `report`) and `exit` (the first `complete` after the box entered). Each event has a stable `id`. Every `at` is the recorded time in UTC. A history entry or refusal with a malformed time is left out and reported once per record as `<path>: malformed factory event`; a `history` that is not a list is reported the same way. A task id found in more than one task record gets no box, and each of its records owned by a lane session is reported as `<path>: duplicate task identity`. A projection that cannot be produced returns `500 internal_error` with `Factory data could not be read`.
- Optional numeric query fields: `low`, `likely`, `high`, `review_multiplier`. Require `0 ≤ low ≤ likely ≤ high ≤ 1000` and `1 ≤ review_multiplier ≤ 1000`.
- `mode=demo` selects the isolated demo fixture. Team/Project/Epic filtering is browser-local; API returns the full snapshot.
- Errors: `{"error":{"code":"...","message":"..."}}` with fixed, code-written messages; exception text is written only to the server log. An invalid model value or mode returns `400 invalid_request`; a snapshot that cannot be produced returns `500 internal_error` with `Dashboard data could not be read`. Mutations return `405 read_only`. Requests the HTTP layer itself rejects (unsupported methods, malformed or over-long requests) carry the same body shape and security headers with `unsupported_method`, `bad_request` or `internal_error`; a `HEAD` reply has no body. Static assets are allowlisted. No arbitrary file-serving endpoint.

## Validation

The mailbox orchestrator's optional local CLI `dispatch_gate.py` reuses this graph projection for dependency eligibility and immutable execution receipts. It is not an HTTP mutation endpoint and is not invoked by the read-only web server. Its wrapper and mandatory worker protocol live in the containing mailbox workspace; no mailbox snapshots are packaged into the application repository. Run its tests with the normal test/coverage workflow. Its denial reasons are fixed texts: a record that cannot be parsed or read is reported as `Invalid JSON: <relative path>` or `Unreadable file: <relative path>`, a `scope.json` that is not a JSON object as `Malformed scope record: <relative path>`, any other unexpected record shape as `Mailbox record structure is malformed`, and a malformed UUID is denied with the same reason as a non-canonical one.

The workspace script `context_pack.py [--root <mailbox>] <node>` prints the same context pack for the orchestrator and for workers; it writes nothing and authorizes nothing. `receive.py` and `status_round.py collect` attach that pack themselves: every result carries `context` with `available`, `packs` keyed by `task:<id>` for the first 10 distinct tasks the run points at and `omitted` for the rest, and each request or `decisions_needed` entry whose task has a pack names it in `context_node`. When the context cannot be built they report `"available": false` and otherwise behave as before. `context_pack.py` and `dispatch_task.py` refuse to run without `--root` when no `projects.json` is found above them, so a copy inside an application checkout never treats `workspace/` as a mailbox; both report a failure with one fixed line on standard error.

The workspace scripts `new_task.py`, `reconcile_sessions.py`, `jira_sync.py`, `record_issue.py`, `aio_sync.py`, `ensure_structure.py`, `status_round.py` and `status_round_runner.py` end an anticipated failure with one fixed line and a non-zero exit status, without a traceback, library or service response text, or absolute paths. They publish records through a uniquely named temporary file that is synced before it is linked or renamed into place and never replace an existing record. `jira_sync.py` and `record_issue.py` read every documented `issues.json` format, write it back in the format they found and serialize updates of one Epic with a lock on its folder; `--epic` must be a single folder name under `epics/`, and `epics/` itself must not be a symbolic link. `jira_sync.py done` needs the canonical UUID of a pending request and refuses one that is already completed; it writes the applied status or estimate to the snapshots before it completes the request, so a failed snapshot update leaves the request pending. `estimate` needs a finite number of hours. A worklog for an issue whose start was applied is refused while another issue of one of its projects has an overlapping closed stay whose start was skipped and whose worklog is not queued yet, because that worklog is the only record of the neighbour's real In Progress time; queue the neighbour's worklog first. Two issues whose starts were both skipped do not block each other: until its worklog is queued, each counts for the other from the time its start was skipped, which can overstate the shared hours. A neighbour that is still In Progress after a skipped start does not block a worklog either and counts from the time its start was skipped, so when its real start was earlier the two worklogs together can exceed the working time. While `epics/` is a symbolic link, every `jira_sync.py` command that reads or writes the snapshots is refused. `reconcile_sessions.py retire` archives the messages addressed to either ID of the retired session. A takeover in `reconcile_sessions.py` keeps an earlier archived orphan record: the archive file gets a unique name when `archive/orphans-resolved/<task>.json` is taken, and the succession entry records it as `orphan_archive`. `aio_sync.py` accepts only an issue key such as `GEO-123` and a numeric `--sprint`, and uploads evidence, coverage and JUnit files only when each is a regular file, not a symbolic link, not named `.env*` in any letter case, and inside the mailbox root, a registered project's repository or a linked Git worktree of one (the worktree's `.git` file and the repository's `worktrees/<name>/gitdir` file must point at each other); its ledger records the revision of each run and the cycle it created, so another revision is recorded as a new run and a later rerun reuses the recorded cycle. `status_round.py request` publishes one non-executable `status_request` per registered worker and 15-minute UTC slot under a UUID derived from the slot, project and endpoint, so a repeated run in the same slot publishes nothing, and it archives each worker's older unanswered `status_request` (`superseded`, or `supersede_conflicts` when the archive name is taken; it needs `inbox/orchestrator/` to tell answered requests apart and otherwise ends with its mailbox line and archives nothing); `status_round.py collect` only reads the replies and lists missing workers, blocked workers' context and question, and incomplete blocked reports.

The workspace script `status_round_runner.py [--root <mailbox>] start --endpoint <orchestrator endpoint>` runs the orchestrator's 15-minute status round without an agent. It starts one detached runner process for the mailbox root, which runs `status_round.py request` for the current slot at once and then at every 15-minute UTC boundary, together with `collect` for the previous slot. `start` is idempotent: while a runner for the same endpoint runs, it starts no second one and changes no file; for another endpoint it is refused. `stop` ends the runner and prints `stopped`, or `not_running` when none runs. `status` only reads and prints `running`, `pid`, `endpoint`, `started_at`, `current_slot`, `last_slot`, `current_slot_recorded`, `last_tick_path`, `last_tick` and `last_tick_problem`, with paths relative to the mailbox root. The runner holds `.status-round-runner.lock` in the mailbox root for its whole life; that file is also its state (pid, endpoint, start time). The runner is bound to the orchestrator: `start` is refused while no orchestrator started by the team launcher holds `.copilot-mailbox-orchestrator.lock`, and the runner ends by itself at the first slot boundary where no process holds it. Each tick publishes one immutable record `status-rounds/<YYYYMMDDTHHMM00Z>.json` with `id`, `type` (`status_round_tick`), `slot`, `recorded_at`, `orchestrator_endpoint`, `request` (the result of `request`: `published`, `existing`, `skipped`, `superseded`, `supersede_conflicts`), `previous_slot`, `not_requested` (the workers that had no request in the previous slot; `collect` still lists them under `missing`), `collect` (the unfiltered result of `collect` with its context: `workers`, `missing`, `decisions_needed`, `incomplete`, `context`), `collect_omitted` (set, with `collect` null, when no worker was asked in the previous slot) and `errors` (each failed step with its fixed line; the field of that step is then null). A tick repeated in one slot touches no file. A tick wakes no session: the orchestrator reads the newest record. `current_slot_recorded` is false from a slot boundary until the runner has ticked (at most about 30 seconds plus the tick) and stays false when the record could not be published. Slots missed while the machine sleeps are not replayed, the runner is not restarted after a reboot or logout, and a running runner keeps the code it was started with, so run `stop` and `start` after `make setup` refreshed the scripts. Invalid arguments end with the fixed line and exit status 2.

```sh
make setup
make doctor
make check
make coverage
make up
make health
```

Tests use temporary directories and synthetic data; they never mutate live mailbox state. Workflow tests cover dependency/configuration failures, environment precedence, configuration preservation, setup repeatability and demo isolation. Browser verification covers filters, model edits, cycles, unresolved nodes, narrow viewport, source warnings and safe text rendering. Local development evidence is excluded from the public repository.

The Team setup workflow runs installation and diagnosis regressions on macOS and Ubuntu. Its Windows job exercises the PowerShell command registration and diagnostics on a Windows runner. Interactive sessions inside WSL 2 require a separate end-to-end check. [Team setup workflow](.github/workflows/team-setup.yml)

## License

[MIT](LICENSE) — copyright 2026 Petar Nenov.

### Third-party libraries

The Factory page uses two libraries that are vendored unchanged in `static/vendor/` and served from the dashboard itself; the page loads nothing from the network. `static/vendor/VENDOR.md` records the version, source, checksum and licence of every file.

- PixiJS 8.23.0 and its `unsafe-eval` module, under the MIT licence (`static/vendor/LICENSE-pixi.txt`).
- GSAP 3.15.1, under the GSAP standard licence (<https://gsap.com/standard-license>). GSAP is **not** MIT-licensed and is not covered by this repository's licence; its file keeps its original licence header.


### Implementation process orientation

The Overview adds process-stage counts, Now / Blocked / Next triage, a verification queue and changes since the previous comparable browser snapshot. Jira keys open a shared detail panel with the recorded baseline, remaining work, explicit owner, original acceptance criteria, per-criterion evidence and revision trace. Missing ownership, criterion coverage and revision evidence remain unknown. The source Jira status stays independent from the local process stage and blockers.

Process stages are `uninvestigated`, `investigating`, `ready`, `implementation`, `review`, `verification`, `complete` and `unknown`. A partial baseline means an initial assessment exists and investigation is incomplete; a reported complete baseline is a verification candidate. Only the existing strict issue verification permits Complete. Ready and Next are recorded stages/suggestions, not permission to execute: dependency, ownership, loop and quota checks still apply.

Search combines with Project / Epic / Team. The URL retains filters, search, view, graph mode and focused issue; unavailable values must be explicitly reported. Graph focus uses canonical relationships and keeps external context separate from scoped totals. Freshness labels distinguish source Jira updates, worker reports (or explicitly labeled linked-task update proxies), evidence times and dashboard read time; missing, invalid and future timestamps do not imply freshness or worker availability.

Changes are browser-local comparisons, separated by live/demo mode and comparable filter/search scope. They report observed completion, stage, blocker and scope changes with previous metadata for removals. The first visit has no comparison baseline. **Hide completed** changes only what is displayed: project cards, membership and the comparison snapshot are computed from the selected scope before completed items are hidden. Comparison baselines stored by an earlier version are discarded once. Storage failures and incompatible data are explicit; opening details or changing graph presentation does not consume a comparison. The server and mailbox stay read-only.


Issue records may provide an explicit `delivery` object. Supported fields include `stage`, `existing_work`, `remainder`, `owner`, `next_action`, `criteria` (`text`, `status`, `evidence`), `verification` (`verifier`, `evidence`, `reason`), `trace` and `freshness`. Trace is keyed by `written`, `reviewed`, `integrated` and `tested_integrated`; each entry supplies `revision`, `evidence` and optional `at`. Integrated verification requires a recorded integrated revision and a matching tested revision, both with evidence. A trace claim never changes issue verification or proves deployment. Structured criterion results require their own evidence to be displayed as passed; original acceptance text remains available when structured coverage is absent.

The projection also preserves existing `baseline_status`, `baseline_assessment`, `acceptance_criteria` and explicit ownership/verification timestamps. Source metadata conflicts remain visible instead of selecting the most advanced claim. Producers should record actual outcomes and artifact references in their owned coordination records; the dashboard does not invent or write those records.

Process-data conflicts, and read failures of `deliveries/<task>.json` or `jira-sync/worked-so-far.json`, are returned in `delivery_warnings`, separately from authoritative graph warnings; they do not grant or remove dispatch eligibility. Changes to the display helper still invalidate old dispatch receipts through the code fingerprint. A total whose individually valid values sum beyond the floating-point range is returned as `null`: `estimate`, `remaining_estimate` and `actual_machine_hours` of `totals` and of each Epic and project `progress`, and a project's `allocated_estimate`. A low/likely/high range is `null` as a whole when any of its three sums overflows, and each field is decided on its own. `delivery_warnings` then carries `Estimate or measured-hour totals exceed the supported number range; the affected totals are unavailable` once per response.


### Ownership roles

Jira assignee, implementation owner and recorded execution owner are separate roles. An explicitly fetched `jira_assignee: null` means **Unassigned in Jira**; an absent assignee field means **Not loaded**, while malformed or contradictory ownership metadata means **Conflicting records**. The assignee retrieval timestamp and source describe this field snapshot independently from the issue status/acceptance snapshot. Jira ownership is never inferred from an Epic parent, a local agent or affected projects.

`implementation_owner` identifies the explicitly recorded implementation responsibility. Execution ownership comes from linked local tasks' `owner` or `owner_session_id`, with task/project/source context. Multiple recorded workers remain inspectable; a recorded session ID is not proof that the worker is currently active. Missing local responsibility is **Not recorded**, rather than a claim that nobody owns the Jira issue. Legacy local `owner` records remain local ownership and never become a Jira assignee.

Local enrichment may fetch only ownership fields read-only from Jira and preserve the imported issue scope, statuses, parents and acceptance criteria. Such a partial refresh does not make the full Jira snapshot newer and never changes Jira assignment.

Issue detail prioritizes the process summary, next action and compact ownership cards. Recorded sources, observations and full worker identities remain available in expandable audit sections. Existing and remaining work are visually separated; presentation does not change recorded stages, assignments or completion evidence.

### Acceptance criteria presentation

Issue details display acceptance criteria as numbered cards with explicit recorded outcomes and expandable evidence references. The coverage counter counts criteria with recorded evidence, independently of their outcomes; it is not a completion percentage. Green outcome styling requires a recorded passing outcome and evidence. Failed or blocked outcomes receive warning styling; other outcomes remain neutral.

When structured criteria are unavailable, the original text is displayed as source scenarios split at numbered headings (including `1:`, `1.` and `1)`), with blank-line or bullet boundaries as a fallback. These excerpts are labeled **Not assessed** and do not establish criterion coverage. When structured criteria exist, the original acceptance text remains available in an expandable section. Missing criteria and missing evidence are explicit. The cards support light/dark themes and narrow screens, and preserve the dashboard's read-only verification semantics.

Scenario cards distinguish the title and Given / When / Then / And / But steps with compact labeled rows. Source preambles and other text remain visible; textual splitting is presentation only and does not create structured verification records.
