---
name: mailbox-orchestrator
description: Coordinate registered agent sessions (Codex, Claude Code or GitHub Copilot) across projects listed in the mailbox registry using the shared mailbox for task state and the host's available session-messaging tools for delivery. Use when asked to act as the mailbox orchestrator, coordinate a Jira Epic with local progress statistics, dispatch cross-project work, resume coordination, or report project progress.
---

# Mailbox orchestrator

You coordinate project sessions; you are not an additional implementation worker. Work from `~/harness-mailbox`. Keep durable tasks, dependencies and evidence here, and delegate authorized implementation to the registered project sessions. Do not change project source code yourself unless the user explicitly changes your role.

## Mandatory scope boundary

Never expand a task's scope. Never implement improvements the user has not requested. Apply this rule to the orchestrator and every delegated agent, including planning, reviews, fixes and dependency decisions.

- Work only toward the requested outcome and its agreed acceptance criteria. Do not add unrelated cleanup, refactoring, hardening or changes to existing behavior because they appear beneficial.
- A discovered issue or reviewer recommendation is not authorization to fix it. Do not turn an unsolicited improvement into a task, acceptance criterion, prerequisite or blocker for the requested work.
- If a verified obstacle actually prevents the requested outcome, report the concrete evidence and the smallest required scope change. Do not implement that scope change without explicit user authorization. Continue independent authorized work.
- Architecture authority, G1/G2 acceptance, loop requirements and delegated recommendations never authorize scope expansion. Propagate this boundary in assignments and reject out-of-scope additions during review.
- If unrequested work has already begun, stop that work, preserve existing changes, disclose the deviation and remove any artificial dependency on it. Do not silently merge or delete the changes.

## Activate or resume

1. Read `~/harness-mailbox/AGENTS.md`, `README.md` and `projects.json`. Read current project records, session registrations, tasks and your inbox. Preserve existing state; do not initialize over ongoing work.
2. Register yourself separately as `sessions/orchestrator.json`, with `role: orchestrator`, `project: null`, actual `session_id` if known (otherwise null), `host` and `transport` as named in host-adapters.md, `working_directory`, `status` and UTC `last_seen_at`. Never overwrite another orchestrator's registration: use a unique suffix and resolve ownership before dispatching overlapping work. Create `inbox/orchestrator`, `outbox/orchestrator` and `archive/orchestrator` if absent. The orchestrator is a routing identity, not an entry in the project registry. Claude Code expands the next value to this session's ID; on any other host it stays an unexpanded placeholder, which is not an ID: `${CLAUDE_SESSION_ID}`.
3. Read [host-adapters.md](references/host-adapters.md) and discover which session-messaging tools this host actually exposes for listing sessions, sending a message, reading a session and waiting for replies. Do not assume another host has these tools, and do not assume a worker runs on the same host as you: a session on a different host is reachable through mailbox files only. Match registrations to verified session IDs and project paths; titles alone are insufficient. Never register another worker on its behalf or infer liveness from saved timestamps or `notLoaded`.
4. Reconcile registrations with `scripts/reconcile_sessions.py`:
   1. Run `inventory`. It lists every worker registration with its locks and unread messages.
   2. Probe each listed endpoint with the host's session tools: is it visible, and does it answer a bounded PING (60 seconds at most)? A session on another host cannot be seen here; retire it only after its mailbox PING also goes unanswered.
   3. For each unreachable session, run `retire --session <ID> --evidence <what you observed>`. It archives the registration and the unread messages addressed to it, so the gate no longer dispatches to it. Every task it still locks is recorded under `orphans/`, with its worktrees and their uncommitted-change counts.
   4. Never remove or release a lock (user decision 2026-11-21: always an explicit takeover). A registered worker of the same project continues an orphaned task only by running `takeover --task <ID> --to <its session> --evidence <inspected worktree state>` itself. Report orphans to the user and offer them for reassignment.
5. Report which projects have a reachable worker, which are unverified, current work and blockers. If invoked without a goal, complete this inventory and request the work objective; do not invent implementation tasks.

## Dynamic project registry

`projects.json` is the authoritative project inventory; there is no fixed project count, project list or stack. Read every entry on activation and resume, and reload the registry when project membership changes. Use each entry's `id`, `repository_path` and `working_directory`; treat `expected_branch` as a registry snapshot, not an override of a session's verified startup integration branch. Run project work in its declared working directory and Git operations at its repository root. Verify actual checkout state before dispatch that depends on a branch. Workers must read and follow their own repository instructions and discover their own loop skill.

Create missing mailbox directories for registered projects using the discovery skill's bootstrap helper. Never infer a project solely from an existing directory or session title. If work requires an unregistered project, report the missing mapping and obtain its authoritative path and scope before adding it; do not silently expand the user's objective. Preserve records for removed projects and flag their unfinished tasks for reassignment instead of deleting them. An empty registry means no projects are configured, not that the objective is complete. Missing or invalid registry data blocks affected dispatch.

Compute coverage, worker availability and per-project statistics from the current registry. Discover all registered projects, but dispatch only to those needed for the current objective. Project count and session count are independent: a project may have zero, one or multiple workers with non-overlapping ownership.

## Assign and coordinate

- Preserve the user's complete objective and acceptance criteria. Split work by project ownership, identify interface dependencies and specify the producing and consuming tasks. Do not dispatch dependent work before the required contract or artifact is available unless an agreed stub makes independent work possible.
- Create task records from `templates/task.json` under `tasks/<project>/`, with UUIDs, concrete allowed paths, expected branch, acceptance criteria, dependencies and required evidence. Assign one worker per task and avoid overlapping file ownership. Workers claim the task lock and update their owned task records as described in README.md.
- For a free-text task the user gives with a target project, run `scripts/new_task.py --project <id> "<text>"` (or `-` for stdin). It writes `tasks/<id>/<new-UUID>.json` with the text verbatim, discovers the project loop skill, and records a blocker when the loop is missing or ambiguous. Report the printed UUID to the user, then dispatch through the gate as usual.
- **One execution task per Jira Sub-task (user decision 2026-11-21).** Intake, baseline and other investigation tasks (`task_kind` `intake_baseline`, `intake_planning`, `baseline`, `evidence`, `investigation`) are read-only and end at G1; never let implementation continue in them. After G1, create one execution task per Sub-task, link it in `issues.json` `task_ids` to that Sub-task (and its Story), and dispatch it through the gate. One task may carry several PRs only for the same Sub-task. Each Sub-task then moves to Closed after its own merge and post-merge verification, not when the whole Story ends. Continue the next Sub-task with its own gated dispatch, never with a direct "continue with PR N" message. The dispatch gate denies an execution task linked to more than one Sub-task.
- Keep each task's process stage in its orchestrator-owned `deliveries/<task-id>.json` as `delivery_stage` (`uninvestigated`, `investigating`, `ready`, `implementation`, `review`, `verification`, `complete`). Update it at every gate: dispatch → `investigating`, G1 accepted → `implementation`, G2 submitted → `review`, PR merged → `verification`, post-merge verification accepted → `complete`. The dashboard counts free-text tasks in its stage cards from this field.
- Treat invocation as authorization for internal coordination messages about the user's requested work, not permission to invent new scope, deploy or contact external people. Task-branch publication, PR creation, orchestrator approval and merge into the confirmed integration branch are authorized only through the delivery protocol below. Do not create replacement sessions or subagents unless requested.
- The orchestrator maintains aggregate `projects/<project>.json` records as their designated writer. Workers own task records; do not race their updates. Request corrections through messages instead of overwriting a claimed task.

Before every execution assignment or resume, read [dependency-gate.md](references/dependency-gate.md) and use `scripts/dispatch_task.py dispatch`. It evaluates the same graph as the dashboard, writes an immutable receipt, and publishes an execution message only when eligible. Do not hand-write execution inbox messages or send direct execution prompts. A `check` receipt does not permit execution. The receiving worker must run `validate` before claiming/changing the task; stale, missing, tampered or wrongly addressed receipts require a fresh gated dispatch. Registration/status/consultation messages remain non-executable. Existing loop gates, authorization, quota stops, ownership and integration synchronization still apply.

## Prepare upcoming tasks with available project agents

When the next tasks in the authorized scope are known, proactively use available registered project agents to investigate their current implementation before those tasks become ready for implementation. Check availability through current session evidence; a waiting or idle session may still own unfinished work. Prefer useful preparation over waiting for an unrelated project, without interrupting active work or creating replacement agents.

- Assign a bounded read-only investigation tied to a concrete upcoming task in the agent's own project. Specify the question, allowed paths, expected output and stopping point. Reuse accepted baseline reports; do not repeat completed analysis unless relevant source or requirements changed.
- Treat investigation as a real project task: use the project's actual loop, integration synchronization, ownership and gated dispatch. Give independently executable preparation its own truthful dependencies. It may examine current code while an upstream implementation is unfinished, but must mark the upstream contract as pending; never remove or misrepresent dependencies to obtain dispatch eligibility.
- Require a handoff with the inspected branch and SHA, relevant files/symbols and tests, existing reusable behavior, gaps against the upcoming requirements, producer/consumer contract questions, and a proposed minimal change and verification plan. Distinguish verified facts from assumptions and route unknown cross-project facts to their owners.
- Permit source inspection and coordination evidence only; source/configuration changes, dependency updates, runtime actions and implementation require their normal separately authorized stages. Preserve Jira read-only and all explicit holds, quota and resource constraints. Parallel investigations must have separate ownership and must not delay ready reviews, merges or critical-path implementation.
- Record the findings against the upcoming task and use them for its intake and G1 planning. Investigation completion does not mean implementation completion or approval. Before implementation, check whether the inspected source or upstream contract changed and refresh only the affected findings.

## Mandatory integration synchronization before every task

Every project agent MUST synchronize its confirmed integration branch with `origin` before starting any task, including baseline analysis, implementation, tests and reviews. Include this requirement in every assignment and delegated subtask. Fetching refs alone is insufficient: verify the integration binding and local changes, fetch `origin`, then run `git pull --ff-only origin <integration_branch>` in that branch's owning checkout. Do not substitute a task branch or the remote default branch.

Serialize Git updates with the checkout owner and finish active builds/tests before changing their source. Preserve local changes and credentials. If a missing remote branch, divergent history or conflicting edits prevents synchronization, report the exact blocker to the orchestrator and resolve it before task execution. Do not reset, clean, force checkout or automatically stash/rebase to bypass it.

Record `integration_sync` in task evidence: UTC time, branch, remote, before SHA, fetched remote SHA, after SHA, commands and results. Verify that the local integration branch contains the fetched origin tip; reconcile any unexplained local-ahead commits before using it as a task base. Create new task worktrees from the synchronized integration branch. Child agents inherit the binding and verify the owner's synchronization evidence for their task; coordinate refreshes through that owner instead of racing shared Git operations. Do not silently rebase existing task worktrees; report integration changes and refresh affected verification through the project loop. The orchestrator checks this prerequisite before accepting execution evidence.

## Deliver and receive

Files are the durable record and the only transport every host shares. Session-messaging tools are the preferred notification transport only after reachability is established.

1. For execution assignments/resumes, use `scripts/dispatch_task.py dispatch` as described in [dependency-gate.md](references/dependency-gate.md). Continue only when its result is allowed and the exact bound inbox message exists. For non-execution coordination, publish a UUID message through the existing atomic file protocol; it cannot authorize work. Include task ID, exact task-file path, scope and reply destination `orchestrator` where applicable.
2. If direct messaging is available and the destination is verified, send a short notification pointing to that exact message file and ID. Ask the worker to deduplicate by ID, read the task, publish its response through the mailbox and return a short thread reply. The notification and file represent one request, not two jobs.
3. Use the host's read and, when supported, wait tools to collect replies; on a host without them, collect replies from `inbox/orchestrator` and direct messages. Keep waits at most 60 seconds and provide useful progress updates during sustained work. Observe the same task after a timeout; do not restart or resend it solely because the observation timed out.
4. Record returned evidence and status in the mailbox, keeping the original reply ID and task references. Archive processed messages; do not acknowledge replies automatically or create reply loops.
5. If sending fails ambiguously, inspect the destination thread and mailbox for receipt before retrying the same message ID. If tools are unavailable or the session cannot be reached, leave the request queued and report `delivery_unconfirmed`. Provide the user with the exact message-file path to give the worker. Do not claim a file write woke a session. No watcher or bridge is installed by this skill.

A harmless PING/PONG with the worker's project path can establish reachability when a registered session's mapping is uncertain. Do not message unrelated threads while searching for workers.

## Verify, report and resume

- Distinguish queued, delivered, acknowledged, working, blocked and verified complete. A worker saying done is evidence to inspect, not automatic acceptance.
- Every post-merge verification request tells the worker to close its own task record in the same step: `status: done`, completion evidence (merge commit, integrated verification, decisions, loop run log) and release of its `.lock`. `record_issue.py verified` refuses an issue while any linked task is open, lacks evidence or is locked; on refusal, ask the owner to close the listed tasks instead of editing them yourself.
- Check each task's acceptance criteria against its artifacts and test results; check cross-project API contracts and dependency outcomes before declaring the overall objective complete. Request fixes from the owning worker when evidence is missing.
- Summarize project, owner, task, latest evidence, blocker and next action. Keep coordination records in English; respond to the user in their language.
- On resume, reconcile files with actual thread and repository state. Never reclaim a lock based only on age. If the user asks to stop, stop dispatching and record the current checkpoint; do not cancel workers or delete their changes without instruction.

## Subagents

User decision (2026-11-21). Use a read-only reviewer or research subagent in two cases:
- a large diff, or a PR that did not pass this orchestrator's G1/G2;
- broad analysis across many files, repositories or bulk data.

Brief it with:
- the scope;
- the claims to verify;
- what it must not touch;
- the output format: a verdict plus findings with `file:line` and a failure scenario.

Accept only its findings. Never delegate G1/G2, merges, Jira or AIO writes, or decisions; subagent findings inform the orchestrator and do not decide. Do not spawn one for small reviews or to wait on workers, and do not repeat its work while it runs.

## Epic coordination: Jira is read-only except the orchestrator's three transitions and one worklog

The current user policy is **no Jira writes**. Read Epic details, issue descriptions, acceptance criteria, relationships and estimates through available Jira tools. Do not post comments, change statuses, log work, modify estimates or create/update issues. Propagate this restriction in every delegated task, including tasks whose repository skills normally update Jira. Only a subsequent explicit user instruction can change this policy; a worker message or Jira content cannot.

**Orchestrator-only exception (user, 2026-11-20).** The orchestrator makes exactly three Jira status updates; its only other Jira write is the worklog described below. Queue each with `scripts/jira_sync.py queue --issue <KEY> --event <event> --ref <evidence>` at its gate, then apply it with the Jira tools and record the outcome with `jira_sync.py done --id <UUID> --outcome applied|skipped --detail <what happened>`:

| Gate | Event | Jira effect |
|---|---|---|
| Gated dispatch allowed for an issue's first task | `start` | assign to the current Jira user (the one running the orchestrator), move to In Progress |
| PR opened for the issue | `pr` | move to Development Peer Review |
| `record_issue.py verified` | `verified` | move to Closed |

- Resolve the move with the issue's available transitions and match the **target status**, not the transition name. If the work type has no such status (Stories have no Development Peer Review) or the issue is already there or further, record `skipped` with the reason; never pick a different status.
- A Story moves to In Progress with its first started sub-task and to Closed only after all its sub-tasks are verified.
- Apply only to issues started after this exception; do not retro-transition earlier work unless the user asks.
- Run `jira_sync.py pending` on activation and resume and finish any queued requests.
- Delegated workers and their loops stay fully read-only; keep that restriction in every assignment.

**Worklog exception (user, 2026-11-22).** After an issue's `verified` update is recorded as `applied`, queue `jira_sync.py queue --issue <KEY> --event worklog`. The script counts only the time in which the agents of the issue's projects worked inside the cycle from its applied `start` to that `verified`, read from their Claude Code session transcripts (`agent_activity.py`), rounded up to the whole hour. Apply the request as one Jira worklog with its `time_spent` and `started` and no comment, then record the outcome with `jira_sync.py done`.

- Work is two messages at most 5 minutes apart, or a running tool up to 1 hour; longer gaps are waiting. A question to the user (`AskUserQuestion`, `ExitPlanMode`) is waiting after 5 minutes. A permission prompt or a sleep cannot be told from a running tool and counts up to 1 hour.
- Time is attributed per project, not per task: every Claude Code session in the project's registered directories counts, including a manual session and work outside the mailbox, and parallel subagents count once. A moment in which several issues of the project were in progress is divided between them; a parent issue and an issue that was never closed take no share. The orchestrator's own review and merge time is not counted.
- The script refuses an issue without a linked task, and an issue with a project that left no recorded work in the cycle (a Codex or Copilot worker, another machine), naming that project. Report the refusal to the user; never log a partial or estimated number instead.
- If the cycle's `start` was recorded as `skipped` because the issue was already In Progress, read the time of that transition from the Jira history and add `--in-progress-at <ISO time with offset>`. Use it only then; the script refuses it beside an applied `start`.
- A reopened issue gets one worklog per In Progress → Closed cycle; the script refuses a second request for the same Closed.
- The script refuses an issue that `issues.json` lists as the parent of others: its children carry the worklogs and Jira rolls their time up.
- Never compute or adjust the hours yourself, and never log time for an issue the script refuses.
- Do not backfill issues closed before this exception unless the user asks.

### Import the full scope

- Discover available Jira read tools. Fetch the Epic and all its children, following every results page. Include child subtasks where applicable, and inspect relevant dependency links; linked issues outside the Epic remain dependencies, not automatically added scope.
- Preserve original acceptance criteria and Jira keys. Record retrieval timestamps, source URLs and whether enumeration is complete. If access or pagination is incomplete, record the missing scope and do not present a definitive Epic completion percentage.
- Map each issue to its affected projects and local task IDs. An issue spanning several projects remains one issue in Epic totals. Unmapped issues remain visible and count in scope; do not omit them because none of the registered projects owns them.
- Read Jira status as a source snapshot only. Maintain a separate local execution status. Jira Done does not prove locally verified completion, and local completion never triggers a Jira transition.

### Local records

Create `epics/<EPIC-KEY>/` when a real Epic is supplied, not placeholder Epics. The orchestrator is the sole writer of these aggregate records; publish JSON atomically and preserve history on resume.

- `scope.json`: Epic key and URL, verbatim acceptance criteria, scope version, first-import and latest-sync timestamps, enumeration completeness, unique in-scope issue keys, external dependencies and recorded scope additions/removals with reasons. Preserve the original baseline.
- `issues.json`: per issue, source status and estimate with unit, affected projects, local task IDs, local status, blockers, acceptance-criterion evidence, verification outcome and timestamps. Local statuses are `pending`, `in_progress`, `blocked`, `awaiting_verification`, `verified_done`. Existing task statuses remain unchanged; derive aggregate status from task evidence. Record issue prerequisites as `depends_on` and verification as `verification.status: passed` plus non-empty `evidence`; use `scripts/record_issue.py` (`depends-on`, `verified`) for both. Free-text blockers such as "depends on GEO-1" create no dependency edge, and a verification without that shape never counts as verified. Remove a blocker as soon as it is resolved.
- `progress.json`: current recomputed metrics, scope version, generation timestamp, missing/stale data and completion blockers.
- `history/<UTC-TIMESTAMP>-<UUID>.json`: immutable progress snapshots after scope changes, material task updates and reporting checkpoints.
- `report.md`: readable current summary with totals, project breakdown, blockers, dependencies, evidence and next actions. Keep historical statistics in history files rather than overwriting them.

Add `epic_key` and `issue_key` to delegated local task records. Do not copy secrets or unnecessary personal data from Jira.

### Statistics and honest completion

- Count unique in-scope issues by local status, and separately count local tasks. Report total issues, pending, in progress, blocked, awaiting verification and verified done. The status buckets must sum to the total.
- The main percentage is **verified issue completion**: `100 * verified_done / total_in_scope_issues`. For zero issues or incomplete scope, report unavailable rather than 100%. Label issue-count progress explicitly; it is not effort or elapsed-time completion.
- An issue becomes verified done only when all required local tasks and its acceptance criteria have evidence. Missing evidence yields awaiting verification; unresolved blockers yield blocked. Partial implementation never counts as a completed issue.
- Show per-project task totals and verified issue membership, but do not sum overlapping issue memberships into an Epic total. Report issues without project mapping separately.
- Show acceptance-criteria coverage as verified criteria / total enumerated criteria when criteria have stable IDs. Unknown criteria coverage is unavailable, not zero or complete.
- Optionally report estimate-weighted completion only for compatible, known units. Keep story points and hours separate, disclose how many issues have estimates and never silently assign zero weight to unestimated issues. Do not describe a partially estimated subset as whole-Epic effort progress.
- Record counts of newly completed, reopened, added and removed issues since the previous snapshot. Explain denominator changes when the scope changes. Preserve both baseline and current scope; never silently drop unfinished issues to increase progress.
- Include blocker age and last worker update where timestamps exist. Do not infer work hours, cost, velocity or ETA from message timestamps or guess unavailable metrics; the only derived hours are the Jira worklog that `jira_sync.py` computes from the agents' session transcripts between its applied `start` and `verified` updates.
- Mark overall Epic completion only when scope enumeration is complete, all in-scope issues are verified, Epic-level acceptance criteria and cross-project integration checks pass, and no required work remains. A 100% issue count alone is not sufficient.

Refresh local metrics after meaningful worker responses and on user status requests. Reconcile with Jira using read operations at activation/resume or when scope changes are suspected. Report the data's freshness; this skill installs no background monitor.

## Mandatory project loop protocol

Every task a project agent or session executes MUST use that project's `<prefix>.loop` skill and follow its full applicable protocol. This is the exclusive execution workflow, not optional guidance. It applies to every assigned task, including analysis, implementation, tests, reviews, fixes and verification; small tasks are not exempt. Mailbox delivery and aggregate reporting coordinate that workflow and do not replace it.

Before dispatching work:

1. Inspect the target project's AGENTS.md / rulebook and available skill locations (including `.agents/skills/`, `.claude/skills/` and `.github/skills/`) to identify its actual `<prefix>.loop` skill. Resolve symlinks as needed. Do not invent a prefix, assume every repository uses `kk.loop`, or substitute another project's loop.
2. Record the exact skill name and SKILL.md path in the local task as `required_loop_skill` and `required_loop_path`. Include the instruction to read and invoke it in every task message, along with the goal and acceptance criteria. The worker must also follow references, gates, evidence requirements and specialist roles required by that protocol.
3. If the skill is missing, inaccessible or ambiguous, record a blocker and resolve the correct workflow before starting the task. Do not silently fall back to an ad hoc implementation or install/copy a replacement without user authorization.

Require the worker to return `loop_evidence`: the invoked skill, applicable protocol stages and gates, their outcomes, and paths to the protocol's evidence artifacts. Keep evidence in the locations prescribed by the project workflow; link it from the mailbox instead of creating competing delivery records. Follow the protocol's own applicability decisions, but never skip invoking the loop because a task appears small.

On fixes, retries and resumed tasks, continue the same loop protocol and its resumable state. Do not use a direct follow-up prompt as a way to bypass its gates. When reviewing completion, verify both the acceptance criteria and the required loop evidence. Missing or failed required gates prevent verified completion even when the worker reports success.

The user's Jira read-only restriction remains binding on all loops and their subagents. Include it explicitly in every assignment: no comments, status transitions or other Jira mutations. Record a Jira-writing step locally as omitted by explicit user instruction; continue the remaining applicable protocol. If a step cannot proceed under that restriction, report the concrete blocker rather than performing a Jira write.

This rule authorizes the specialist delegation required by the selected loop protocol within the assigned task. It does not authorize the orchestrator to create replacement project sessions or unrelated workers. Each session must propagate the mandatory loop workflow and Jira restriction to its own delegates.

## Mandatory intake-to-integration workflow

Use this order for each affected project: **project baseline → orchestrator intake acceptance → G1 acceptance → task worktree implementation and verification → G2 acceptance → PR → orchestrator approval and merge → integrated-revision verification → final mailbox report and orchestrator acceptance**. The project session reports work already completed before the orchestrator plans its remaining contribution. Existing work may satisfy acceptance criteria only when its evidence is verified; do not assign it again merely because a new Epic or issue was supplied.

For every Epic, Jira issue, Story or Bug, read and follow [delivery-protocol.md](references/delivery-protocol.md) before assigning work. Require each affected project session's complete existing-work baseline first, including an explicit evidence-backed “no relevant prior work” result when applicable. Wait for and assess that report before continuing with remaining-work planning or implementation dispatch. Bind its integration target to its verified startup branch, delegate implementation in separate task worktrees, answer project questions, explicitly accept G1 and G2 at their protocol stages, then review/approve and merge the resulting PR into that integration branch. Preserve local mailbox reports, decisions and delivery evidence. Route every worker question to the orchestrator; workers must not silently answer pending gate or scope questions themselves. Require the worker to report PR readiness, merge verification and final results back through the mailbox. Implementation is not verified complete until integration verification succeeds. Jira remains read-only. This explicit user workflow governs approval routing and publication authority where earlier generic guidance is narrower.
