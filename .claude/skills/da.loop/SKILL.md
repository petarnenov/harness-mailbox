---
name: da.loop
description: "Drive a session goal or explicit /da.loop request in the harness-mailbox dashboard repository through task breakdown, implementation, verification, review and fixes. Preserve verbatim acceptance criteria, risk-based approval gates, resumable evidence and measured costs. Use for \"/da.loop <goal|ticket>\", a /goal session goal in this repository, or delivering a repository task end to end."
argument-hint: "<goal, ticket key or nothing to resume>"
globs: []
hooks:
  PreToolUse:
    - matcher: "AskUserQuestion"
      hooks:
        - type: command
          command: 'f="$(git -C "${CLAUDE_PROJECT_DIR:-.}" rev-parse --show-toplevel 2>/dev/null)/.claude/skills/da.loop/scripts/g1-spec-review-hook.mjs"; [ ! -f "$f" ] || ! command -v node >/dev/null || node "$f"'
    - matcher: "Agent|AskUserQuestion"
      hooks:
        - type: command
          command: 'f="$(git -C "${CLAUDE_PROJECT_DIR:-.}" rev-parse --show-toplevel 2>/dev/null)/.claude/skills/da.loop/scripts/adr-gate-hook.mjs"; [ ! -f "$f" ] || ! command -v node >/dev/null || node "$f"'
  Stop:
    - hooks:
        - type: command
          command: 'f="$(git -C "${CLAUDE_PROJECT_DIR:-.}" rev-parse --show-toplevel 2>/dev/null)/.claude/skills/da.loop/scripts/g1-spec-review-hook.mjs"; [ ! -f "$f" ] || ! command -v node >/dev/null || node "$f"'
        - type: command
          command: 'f="$(git -C "${CLAUDE_PROJECT_DIR:-.}" rev-parse --show-toplevel 2>/dev/null)/.claude/skills/da.loop/scripts/run-close-hook.mjs"; [ ! -f "$f" ] || ! command -v node >/dev/null || node "$f"'
        - type: command
          command: 'f="$(git -C "${CLAUDE_PROJECT_DIR:-.}" rev-parse --show-toplevel 2>/dev/null)/.claude/skills/da.loop/scripts/adr-gate-hook.mjs"; [ ! -f "$f" ] || ! command -v node >/dev/null || node "$f"'
---

LOOP: da.loop@2026-11-23.1

# DA Loop — harness-mailbox dashboard (Python + buildless JavaScript)

Hosts: Claude Code (`/da.loop`), Codex CLI (`$da.loop`) and GitHub Copilot CLI (`/da.loop`). The canonical files
live under `.claude/`; Codex reads the skills through the `.agents/skills/*` symlinks and the agents through
`.codex/agents/*.toml`, Copilot reads `.claude/skills` directly and the agents through `.github/agents/*.agent.md`.
The agent adapters are rendered by `scripts/render-agents.mjs` from `.claude/agents/da-*.md` — never
edit them by hand; re-render and commit both. `references/hosts.md` lists per-host tool names, hook wiring and
what each host cannot enforce.

Deliver the user's full goal through steps 1–9; step 10 applies what the run taught to the rulebooks. Mandatory task breakdown,
acceptance criteria and evidence determine completion. Follow the user's standing authorization, the global
rules and the project rulebooks `da-python-conventions` / `da-python-test-conventions`. Never commit or
write to Jira, Confluence or GitHub unless authorized. Bump `LOOP:` on edits. This skill has no automatic
browser pass; browser work requested by the user belongs in the task checklist and uses the available browser
skill, without a separate verification agent.

Stack facts (modules, layers, commands, ports, env vars, test layout) live in `references/stack.md`; read it
at step 1 and quote the relevant rows in every brief instead of re-discovering them. This repository is the
built-in dashboard component of the harness-mailbox coordination workspace (remote
`github.com/petarnenov/harness-mailbox`, default branch `main`). Its command interface is Make: run `make test`,
`make lint`, `make check` and `make coverage` from the repository root; they run in isolated Docker containers with
networking disabled. The component is a Python 3.13 standard-library read-only HTTP server, the `dispatch_gate.py`
CLI, a buildless vanilla JavaScript frontend and the tracked workspace template under `workspace/` (including the
`mailbox-orchestrator` scripts). Mailbox runtime data (`projects.json`, `tasks/`, `inbox/`, `epics/` and the other
record directories) must never enter Git. Health and syntax checks do not prove business behavior.

## Start and resume

Resolve the run directory once as `$(git rev-parse --show-toplevel)/.claude/goal-runs/` and use that absolute
path everywhere, so runs started from any subdirectory do not split history. That directory already holds earlier
local runs of this repository; on resume also inspect the legacy dir `.codex/goal-runs/` if present. Logs are
Markdown files whose first line contains the goal, `status:`, `base:` and `start:`; evidence side files are not
run logs. In this repository run logs and their evidence stay local: `.claude/goal-runs/` is gitignored and is
never committed, so a later session on this machine replays criteria, evidence and findings from it. They must
still never hold secrets, tokens or mailbox record contents. The `.g1hook` counter side files are transient.

- New goal: start a run. If it clearly replaces an old goal, record that decision; ask only when continuation
  versus replacement is ambiguous. Do not interpret a steering message as replacement.
- `/da.loop` without a goal: inspect the newest run header. Resume unfinished work; if done, report its
  goal, final checklist and log path. If stopped by the user, wait for an explicit resume request.
- Resume: read that run's criteria, decisions, tasks and current evidence. Reuse `BASE_SHA`, start time and
  baselines. Check branch and base ancestry, then reconcile status/diff against task ownership. Preserve
  prior authorizations; ask only for a genuinely missing decision.
- A pending delegation is not proof of a crash. Inspect its live agent/session handle first; poll that handle
  while live. Only if terminal or missing, record interruption, inspect its files and resume unfinished work.
  A restart does not reset attempts already supported by a result or failure.
- Reopen completed tasks only when their relevant files/dependencies changed or evidence is missing/stale.
  Re-present pending questions and continue independent authorized work.

Pre-flight, on every start and resume: run `sh <this skill's dir>/scripts/da-node.sh preflight.mjs --module
<all|backend|frontend|orchestrator|infra> --host <claude|codex|copilot|all>` (the host you are running on) and log its
first line in Evidence. The wrapper resolves a `node` even when the non-interactive shell has none on `PATH`. Anything but `PREFLIGHT=OK` is an
environment blocker for the listed agents/books/toolchain: report the lines, never delegate to a missing or
stale agent, and fix it only as authorized maintenance (the files are in this repository and go through review
like code). Pre-flight runs `node --test` on `scripts/learn.test.mjs`, `scripts/run-close-hook.test.mjs` and
`scripts/adr-gate-hook.test.mjs` using its resolved Node executable on **every host**; any regression failure blocks
preflight. One `TESTS=OK <file>` line per file records success. Pre-flight also reports every run log since
`da.loop@2026-11-20.1` that broke the closing contract (below) as `stale`: close that run before starting new work.
Pre-flight also runs `learn.mjs check`, so a lesson that was filed but never applied, an applied
rule missing from its book, or a run log without a ledger row shows up as `stale` here. A preflight that cannot
launch means `node` is missing: the G1 hook is then silently disabled, so
record that as an environment blocker rather than proceeding to G1.
After a successful pre-flight, run `sh <this skill's dir>/scripts/da-node.sh learn.mjs validate` and record
the result. Retirement failures persist `blocked@<date>` with the reason and recurrence source; subsequent
validation reports the pending count without retrying. `check` stays failed until explicit maintenance resolves
the record; do not mark a lesson reverted while its rule remains active.

Before editing, capture UTC start, branch, `git status --porcelain=v1 -uall` and `git rev-parse HEAD` as
`BASE_SHA`. A dirty tree alone is not a blocker: record pre-existing changes, preserve them and establish
an initial diff/evidence snapshot when they overlap task files. Ask only when ownership cannot be resolved.
Do not use a stash or checkout that could disturb the user's work or a running local stack; for red-at-base
checks or a different reference branch use a detached `git worktree` in the scratchpad.
Before creating a new task worktree from the integration branch, always pull it first: in that branch's
owning checkout run `git fetch origin` and `git pull --ff-only origin <integration_branch>`, confirm the local
tip contains the fetched origin tip, and create the worktree from that pulled tip. Record the before, fetched
and after SHAs. If the pull fails (divergent history, conflicting or dirty overlapping edits), stop and report
the blocker; never reset, stash, rebase or branch from a stale local tip to get past it.
Confirm the intended base branch when the task or release target depends on one, and record it in Decisions.
Never infer a base branch from a ticket key or branch name. Preserve the current checkout and its dirty work; ask only
when the intended comparison/base changes implementation safety or acceptance criteria.
When pushing is a done-criterion, check remote base visibility on the configured repository host and a working write-auth
path before long implementation; if unavailable, ask for supported auth setup instead of promising publication.

## Risk and authorization

| Tier | Scope | Gates |
|---|---|---|
| LOW | docs, isolated unit tests, local scripts/skills/config, log messages | plan inline; no blocking gate |
| MEDIUM | a projection, UI or orchestrator-script behavior change within one component, a new read-only API field, build/Make changes with bounded impact | G1 implementation + verification plan |
| HIGH | the read-only server boundary or security headers, the dispatch gate and receipt validation, atomic mailbox publication, Jira/AIO write scripts, token or secret handling, mailbox record format changes, workspace `AGENTS.md`/skill protocol changes, container hardening, dependency/runtime version bumps, cross-component changes, anything hard to reverse | G1 and G2 full risky-file diff |

State the tier and reason. Use the higher tier when uncertain; only the user may explicitly lower it.
G1/G2 require a decision when authorization for that action is still missing. Existing explicit authorization
persists: log it and continue, without asking again. Otherwise present a concrete reviewable package and
wait for approve/amend/reject; silence is never approval. On MEDIUM show diff stat and saved diff path
at handoff; on LOW the final report suffices.

## Standing rule: no raw error text for the user

Never send a raw error message to a client and never show one in a user interface. Raw means any text the
code did not write for the user: a Python traceback, an exception's `str()` from a library or the OS, subprocess
or HTTP client output, a token or environment value, an internal host name or address. It applies to every
surface a user or client can see: HTTP responses, the dashboard UI and CLI output of the orchestrator scripts.
The existing documented record warnings (record path plus a validation reason) and `400 invalid_request` messages
are existing behavior; a change that touches them must keep them free of tracebacks, tokens and host details.

- A user-facing message is a fixed text or message key chosen by the code; the technical detail goes to the log
  and stays in the exception cause.
- Every run that touches code able to produce or forward an error adds this as a criterion at step 1, traces at
  step 2 where the exception or status text of the touched path can end up, and proves it with a test that feeds a realistic raw text and
  asserts the fixed one.
- Writer, test-writer and reviewer briefs quote the rule; the reviewer treats a raw text reaching a user as
  blocking, also when the leak is in code the change only makes reachable.

## Standing rule: strict task scope, no improvement proposals

Follow the accepted scope of each task.

- Deliver exactly what the goal, the frozen criteria and the user's decisions ask for — nothing more. No
  unrequested features, refactors, clean-ups, hardening, renames, extra tests or extra checks beyond the plan.
- Never propose improvements, enhancements, follow-up work, "nice to have" items, optional next steps or
  alternative designs — not in plans, G1/G2 packages, reports, retros, chat or agent briefs.
- Something outside the scope that blocks a criterion is an escalation stating the blocker and the affected
  criteria only, without a proposed improvement; the user decides. Anything else outside the scope is not
  reported and not acted on.
- Questions are limited to decisions needed to deliver the stated scope (step 2, G1, G2, escalations).
- Writer, test-writer and reviewer briefs quote this rule; agents return only in-scope results, and the main
  conversation drops any improvement proposal an agent returns instead of relaying it.

## Working language

Follow the workspace language preference for chat and terminal updates. Write shared project artifacts in English.

## Durable state

One run log is the source of truth; mirror tasks in a todo tool only if available. Do not create a second
checklist for workflow steps. Update task state when it changes, not every tool call.

```
# <goal> ticket: <KEY|none> tier: <tier> base: <sha> branch: <branch> start: <UTC> status: <running|waiting-user|escalated|stopped|done> step: <1-10> loop: <version>
## Done-criteria
## Tasks
## Decisions
## Plan
## AC matrix
## Delegations
## Evidence
## Review
## Gates
## Escalations
## Resumes
## Cost
## Report
## Retro
```

Every run ends `## Retro` with one machine line — `recurred: K12, K30` or `recurred: none` — that step 10's
learning loop reads to count recurrence and to promote or revert an applied rule.

Create headings once; append under them. Task lines use
`- [ ] T1 · outcome · AC/C ids · depends-on · allowed files · todo|in-progress|blocked`
and `- [x] T1 · ... · done · evidence ref`. Clear the checkbox on reopening.
AC matrix: `| criterion | owner task | evidence/test file::name | review | status |`.

Record delegation launch with handle, role, task, allowed files, attempt and `pending`; finish the row with
verdict, report path, available tokens/duration and parallel peers. Evidence includes command, cwd (usually the repository root), exit code, summary, output path and the revision/file state it tested. Do not log secrets: no
identity/session credentials, client secrets, database passwords, tokens, codes or user data.

## Closing contract — every run finishes steps 9 and 10

A run is closed only when its log has all of these:
- a header with a final status (`done`, `escalated` or `stopped`) and `step: 10`;
- a non-empty `## Report` with the step 9 report (the chat report is a copy of it);
- a `## Retro` with retro text and exactly one `recurred:` line;
- its row under `## Runs` in `lessons.md`;
- a `LESSONS=OK|FAIL` line under `## Evidence`;
- for `done` only: every task ticked;
- when the repository tracks an ADR directory and `## Delegations` names `da-python-writer`: a last
  `ADR-review:` line that says `PASS` (step 5, ADR compliance check).

Setting a final status starts the closing; it does not end the run. Write the report and run step 10 in the same
turn. `escalated` and `stopped` runs close the same way. Never end a turn with `status: running`: continue the
work, or set `waiting-user` when a user decision is needed.

Enforcement:
- `scripts/run-close-hook.mjs` runs on Stop (Claude skill frontmatter, `.codex/hooks.json`). It checks the run log
  modified last, if that was within the last two hours. A turn cannot end while that log is `running`, or while it
  has a final status with closing gaps. The hook blocks at most three times per log; the counter is
  `<log>.closehook`, which is gitignored.
- Pre-flight reports every unclosed run since `da.loop@2026-11-20.1` as `stale` on every host, Copilot included,
  so the next start closes it first.

## Task checklist — required for every goal

Every goal, including analysis, docs and config, gets T1..Tn; a small goal has T1, not no checklist.
Split substantial work into independently provable vertical slices. Signals to split: more than one component
(server + frontend, dashboard + workspace scripts, code + container/Make workflow), a record-format change plus its readers, or distinct safety/business flows. Do not fragment a tightly coupled change just to create
delegations. Each task records outcome, owned criteria, dependencies, allowed files, verification and
applicable writer/test-writer. Every criterion has one accountable owner; other tasks may contribute evidence.
Goal-wide criteria name their contributing tasks explicitly.

Tick each task as soon as its implementation/output and scoped verification are proven, then start the next
ready task immediately. Review or later failures reopen the owner; a tick never substitutes for goal-wide
completion. Never ask whether to continue already authorized tasks.

For substantial independent tasks, use parallel agents when delegation is available and its saved time
justifies the context cost. Group small related work in one role delegation while retaining each task's
identity and evidence. Never give concurrent writers overlapping files, and never run competing build/test
targets at the same time (all Make targets share the repository mount and `.coverage-output/`). Tell workers they are not alone and
must preserve others' edits. Shared-file work is sequential unless isolated worktrees and integration are
worth the overhead. While agents run, do useful independent work; wait/poll only live handles, without busy
polling. Main conversation owns the log; agents return reports rather than concurrently editing it.
Analysis slices are briefed by entry points (an HTTP route, a `dispatch_gate.py` operation, an orchestrator script
command or a UI view), not file lists: agents follow callers/callees across the server ↔ graph projection ↔
frontend and gate ↔ workspace script boundaries and tag findings outside their
slice with the owning slice id; the main conversation merges duplicates by file:line before reporting.

## The delivery steps

### 1. Define

Restate the goal and resolve a ticket only when the goal is ticket-driven. Do not guess a key from the branch;
confirm an ambiguous reference before fetching. Fetch the relevant issue once with the configured issue-tracker tools,
including acceptance-criteria fields, attachment metadata and all comment pages; reuse the captured sources
in all briefs. When working from a ticket, read its full description, acceptance criteria, comments and linked parent/specification before
freezing criteria. Issue-tracker attachment downloads must follow redirects (a bare request returns HTTP 406).

Before freezing criteria or planning implementation:
- Locate the owning component first. Trace behavior across the server, graph/delivery projection, frontend,
  dispatch gate, workspace scripts and container workflow as appropriate. Do not assume a coordinated project's
  code or the live mailbox data belongs in this repository.
- Follow design links in the description, AC and comments; download and visually analyze attached images in
  the issue and its comments. Readable text alone, filenames or thumbnails do not substitute for image analysis.
- Read every comment page and account for clarifications, changed expectations, reported defects and
  decisions (comments may clarify acceptance criteria or record decisions). Record relevant comment ids,
  authors, timestamps and their effect on requirements; distinguish superseded remarks from current expectations.
  Comments do not silently authorize scope changes.
- Identify the branch/revision carrying the feature (see Start). A wrong checkout must not become the
  reference implementation.
- Record source URLs/ids, retrieved revision/time, local artifact paths and concrete findings in Evidence;
  link their implications to criteria/tasks. Never copy secrets or customer data into the run log.

If a source cannot be downloaded, rendered or read, record the exact access/tool failure and affected
criteria; do not claim it was analyzed. Escalate when the missing source prevents proving the requested
behavior. Resolve conflicts between AC, comments and designs explicitly; never silently choose a source
or rewrite product scope.

Copy in-scope ticket acceptance criteria verbatim as AC1..ACn. Record user-approved exclusions. No stated AC: label derived
criteria and resolve material ambiguity with the user. No ticket: derive C1..Ck from the actual request.
Add technical completion criteria appropriate to the change — include the compatibility constraints that apply
(read-only server boundary, guarded reads, gate deny paths, atomic publication, record-format compatibility) and the
touched-code coverage gate (≥85 % per touched production module, measured), and the standing rule on raw error text when the
change can produce or forward an error. Freeze criteria after definition; only the user
can change product scope. An unreachable criterion is an escalation, never a silent deletion.

### 2. Resolve unknowns

Resolve code-derived questions with scoped searches/reads. Ask only remaining blocking product or ownership
questions, batched with options where helpful; log answers. Do this inline by default. Delegate focused
read-only exploration only for a large unfamiliar surface or independent uncertainty that warrants its cost.

### 3. Plan and break down

Write the plan and T1..Tn checklist inline by default: modules and files, the architecture path
(mailbox records → guarded reader → graph/delivery projection → `/api/dashboard` → frontend view; or records → `dispatch_gate.py`/orchestrator script → atomic publication; or the corresponding container/Make path), edge cases, risks,
dependencies and verification. Use a separate planning agent only for substantial architectural uncertainty;
pass known findings so it does not repeat discovery. Do not force a model override; respect the routing in
`references/cost-controls.md`.
For complex plans include least certain assumptions, relevant rejected alternatives, hard-to-reverse choices
and explicit rulebook exceptions. A brief cannot authorize a rule violation.
When server and frontend tasks meet at `/api/dashboard`, or the gate and a workspace script meet at a record or
receipt format, the plan freezes that contract (field names, types, nullability, error codes) before either writer
starts; both briefs quote it, and a writer that needs to change it reports back instead of editing the other side.
A record-format change names every reader (server, gate, scripts, demo data) and its check; a container or Make
change names the affected image or target and verification evidence.

### 4. Verification plan and G1

Define evidence per task and criterion before implementation. Choose meaningful unit and integration tests and
exact Make commands from `references/stack.md` and the test rulebook. Retrieved acceptance criteria are the basis
of the test plan: map every in-scope AC to named test cases, expected assertions and required fixtures in the
AC matrix. Include relevant clarifications and image findings from step 1 in test scenarios, without replacing
verbatim AC. For criteria not meaningfully automatable (live dashboard rendering, Docker Desktop setup, real Jira/AIO behavior), name the manual or integration check and required evidence
explicitly; never leave an AC uncovered merely because unit tests cannot prove it.
Docs/config/analysis use suitable structural and semantic checks; do not manufacture unit tests or coverage
for prose or low-impact reversible changes. Record justified non-applicable checks. Present implementation and
verification together for G1.

G1 spec review — before asking for G1 approval, always: brief `da-python-reviewer` with `mode: spec` and
the G1 package (frozen criteria, T1..Tn, plan, verification plan/AC matrix, tier, rule exceptions). Log
`G1-spec-review: PASS|BLOCK <report path>` under `## Review`, fix the findings in the specs or list them as
open questions, and include the verdict in the G1 package. Present G1 with the host's question tool (header `G1`; on Copilot, whose
`ask_user` has no header, start the question text with `G1`) when available. The G1 hook (Claude frontmatter, `.codex/hooks.json`, `.github/hooks/da-loop.json`) denies a G1
question — and on Claude/Codex a turn ending at G1 — until that line exists; on Codex the hook must have been
trusted once with `/hooks`, and on a host where the hook is not active this text is the rule.

Run affected existing test baselines once after authorization and before code changes through the documented
`make test` target (add `make lint` when shell or JavaScript changes). Pre-existing failures are recorded, never
misreported as a clean gate; escalate if they prevent proving the requested behavior. A Make or Docker failure
with no usable output is unavailable evidence, never a pass; diagnose it through the documented Make target and
preserve the actual exit status.

Name one owner for each integrated gate in the plan (`make check`, `make coverage`, `make build` or `make up` with `make health` when runtime or Docker files changed). Task agents report `pending: integrated` rather than
running the full gate independently. Reuse evidence only when command, cwd, exit code, output path and
relevant file/dependency/configuration hashes still match. Preserve compiler exit codes; a failed compiler
startup is unavailable evidence, never zero errors.

### 5. Implement per task

Use `da-python-writer` and `da-python-test-writer` for Python and JavaScript code in any component (Claude: Agent
tool; Codex/Copilot: project custom agent). Docker/Compose, Make, shell scripts and docs stay inline
unless delegation adds value (then assign a writer with an explicit file list). Production writers cannot edit tests, test resources or test config; test-writers cannot edit
production, build config or deployment configuration. A missing testability seam routes to the production writer. Neither
role may skip/delete tests, disable checks or lower coverage to pass. On Claude the `.claude/hooks/path-guard.py`
hook enforces the file boundary for delegated roles; on Codex and Copilot nothing enforces it (their hooks are
session-wide, not per agent), so the main conversation checks `git diff --name-only` of each delegation against
the role's allowed files before accepting its report, and the reviewer treats a crossed boundary as a finding.

Bugfix/regression: test-writer first, capture failure against the unfixed code for the reported behavior, then
writer fixes it. Feature: use red-first when useful; otherwise validate meaningful assertions can detect
incorrect behavior. Do red checks in a detached worktree in the scratchpad, never by stashing user changes.
Tests for behavior changes bring every touched production module to ≥85 % measured line coverage. Use only a
verified report artifact: `make coverage` writes `.coverage-output/coverage.json` for Python; touched `static/*.js`
needs a recorded `node --test` coverage run (see `references/stack.md`). Do not claim coverage until an artifact is
available and tied to the tested revision. State `unavailable` when no measured report can be produced and explain
why. Shell, Docker, Make and config-only changes that cannot be instrumented are recorded as n/a with the
applicable structural or manual evidence.

Regression tests are required wherever possible, in every mode: a reproducer for each described defect
(red at `BASE_SHA`, green after the fix) and guards pinning unchanged pre-existing behavior of touched code
that no existing test pins. Pass known defects and behaviors to protect as the test-writer's `protect:` list —
for this stack that usually includes: mutating HTTP methods still rejected, no mailbox writes from the server, guarded reads unchanged, gate deny paths unchanged, no overwrite on publication, older record formats still readable, no tokens in output, no raw error text on any user-visible surface.

Brief each delegation with the goal in one sentence, task id, criteria verbatim, relevant plan, component and
allowed files, reference revision/base, Make command(s), known evidence and constraints.
Test-writer also receives baseline, mode, writer handoff and `protect:`. When a change widens a condition,
the writer and reviewer briefs name the newly reachable code. Pass a focused brief and relevant artifacts; use
a fresh agent context when supported rather than full conversation history. Reuse existing agents for
follow-up fixes. Respect the routing in `references/cost-controls.md`.

Briefs never dictate a verdict vocabulary; each agent uses its own `VERDICT=` values.
Validate the agents' `VERDICT=` and `RULEBOOK=` headers against the current `RULEBOOK:` lines in
`.claude/skills/da-python-conventions/SKILL.md` and `.claude/skills/da-python-test-conventions/SKILL.md`
(writer/reviewer: both; test-writer: tests). Missing/stale metadata: ask the same agent to correct or read the
missing book once, without rerunning valid work; a second failure blocks acceptance. Route findings via
`writer|test-writer`. Keep friction to at most three concrete bullets when present. Log report paths, not
duplicated full prose.

**ADR compliance check after every code-writer delegation.** This applies when the repository tracks a directory
named `adr` (any case, any depth; `git ls-files ':(icase)**/adr/**'`). As soon as a `da-python-writer`
delegation finishes, and before any other agent, question or the end of the turn:
1. Save `git diff <BASE_SHA> -- <the writer's files>` to the scratchpad.
2. Brief `da-python-reviewer` with `mode: adr`, the ADR directories, the diff path, the task ids and any
   user-approved ADR deviations from `## Decisions`.
3. Record `ADR-review: PASS|BLOCK <report path>` under `## Review`, and write each violation as a finding.

Only ADRs with `status: accepted` bind. A violation is blocking: route it to the writer, fix it and run the ADR
check again. Never lift the gate by editing an ADR, by recording an exception the user did not approve, or by
writing the line without the review. The test-writer, the integrated gates and step 7 start only after the last
ADR check passed.

Enforcement on Claude Code:
- The writer agent's Stop hook (`scripts/adr-gate-hook.mjs --event writer-done`) marks the active run in
  `<log>.adrpending`.
- The skill's PreToolUse (`Agent|AskUserQuestion`) and Stop hooks then deny every agent except
  `da-python-reviewer`, every question and the end of the turn until a new `ADR-review:` line exists. The Stop
  hook blocks at most three times per log (`<log>.adrhook`).

On Codex and Copilot there is no per-agent hook, so this text is the rule, backed by the closing contract.

### 6. Verify

An agent's summary is a claim. Inspect actual command output and artifacts. The main conversation runs the
planned gates or validates captured tool output/artifacts against the exact files tested. Do not rerun an
identical successful command solely because an agent ran it; rerun when output/exit status is unavailable,
evidence is stale, or a relevant file/dependency changed. Capture full output once and show only exit code,
meaningful summary and path.

Check the test-writer's regression table: each described defect has a reproducer and each changed
pre-existing unit has a guard or a recorded reason; a missing reproducer reopens the test-writer.
Red→green evidence must fail for the expected behavioral reason, then pass after the fix; unrelated setup
errors (Docker daemon unavailable, missing tools image, fixture setup) are not red proof. Coverage comes from actual
reports. Product failure routes to writer; wrong test or fixture routes to test-writer. No review acceptance
on unresolved verification failures.
When the server, frontend or a shared record format changes, verify the integration seam: rebuild and start the dashboard (`make up`, `make health`) and check the affected view against demo or fixture data, recording the evidence. When the gate or a workspace script changes, run it against a temporary mailbox fixture; never against the live mailbox without explicit authorization.
Tick a proven task, update its AC rows and start the next ready task. Integrated evidence stays pending until
the relevant final checks pass; do not mark a goal-wide criterion passed from a scoped test alone.

### 7. Review

Use one `da-python-reviewer` review per settled coherent change by default, covering all its task ids,
criteria, production and tests across modules. The main conversation saves `git diff <BASE_SHA> -- <allowed files>`
(plus the initial dirty-tree snapshot where applicable) to a file in the scratchpad and passes that path; the
reviewer's shell is limited to read-only git. LOW isolated changes can use the main conversation's diff check; rules
requiring the reviewer (anything HIGH) still apply. Early task review is justified for risky decisions or to
unblock dependent work, not automatic. After early reviews, final review focuses on integration seams
(record format ↔ reader, `/api/dashboard` ↔ frontend, graph projection ↔ dispatch gate) and later changes.
Reviewers use logged gate results instead of launching duplicate builds. Missing AC implementation is
blocking; outside-scope changes are findings. Write every finding under `## Review` (id, severity, file:line,
one-line scenario) so a later session can reuse it; never log only a pointer to a chat report. Findings are
never posted externally automatically.

### 8. Fix and G2

Reopen the owner of an in-scope finding. Blocking/should-fix issues require scoped verification and reviewer
confirmation of the fix; nits need affected gates, without a new full review. Reuse the reviewer context when
possible and send the changed diff plus finding ids. Out-of-scope/pre-existing issues are neither fixed nor
proposed (standing rule on strict task scope); they stay untouched and unreported unless they block a criterion.
Completion requires every in-scope AC passed and no unresolved blocking/should-fix finding; a reviewer
suggestion outside the scope is dropped, an in-scope one is fixed or recorded as not applicable. User exclusions/acceptances cannot be invented by the loop.

If PR comments are in scope, read all pages of review threads with the configured repository-host CLI,
with ids, author, resolved state, location and text. Classify against applicable rules, route accepted changes
and track evidence per comment. Draft requested replies in English for the user; never post/resolve/approve
automatically.

For HIGH G2 show stat, full risky-file diff, criterion evidence, findings/fixes and deviations from the plan.
Apply the authorization rule above; log the decision. MEDIUM shows stat and saved full-diff path.

### 9. Completion audit and report

Sweep T1..Tn against current disk and evidence. Reopen missing/stale evidence; finish ready work; report
blocked dependencies. Only mark done when every task is ticked, every in-scope criterion has sufficient
current evidence, required checks/coverage pass, reviews are clear and required authorizations exist.
Budget exhaustion or a blocker does not fulfill the goal.

Write the report under `## Report` in the run log, then show it: report what changed (per module), validation and limitations, checklist and log path. Name the deployment
consequence explicitly: "needs `make restart`", "needs `make setup`", "needs `.env` change", "needs workspace copy refresh in the live mailbox", "record format change for coordinated sessions" — or "none". State the number of review fix loops.
Record measured tokens, duration and delegation count/parallelism using `references/cost-controls.md`.
Missing usage is `unavailable`, never estimated. Do not make unavailable accounting block delivery.

### 10. Close the learning loop (automatic)

The loop learns from every run by itself. After the report, in the same run:

1. Write the retro from concrete friction; `none` is a valid answer. End `## Retro` with `recurred: K12, K30`
   when an already applied rule's signal showed up again, otherwise `recurred: none`.
2. File each reusable lesson as one row under `## Lessons` in `lessons.md`: `target: conventions §K<n> (P1|P2|P3)`
   or `target: tests §T<n> (P1|P2|P3)` — the section that owns the concern — one imperative `rule` sentence
   (≤280 characters, no secrets, no customer data) and `evidence` (commit, ticket key or run log). Add the run row
   under `## Runs` in the existing contiguous table, without blank lines between rows.
3. Run `sh <this skill's dir>/scripts/da-node.sh learn.mjs apply`. All new rulebook rules require semantic
   review; syntax checks cannot prove they preserve existing requirements. Coverage policy is **manual-only**:
   never weaken **≥85% measured line coverage for every touched production module**, substitute module averages
   or enumerated counts, or introduce exemptions through a lesson. Entire sections `tests §T4`–`§T8` are
   structurally manual-only regardless of wording. Coverage/threshold/percent/bar proposals in other sections
   also require explicit maintenance. These text checks are conservative diagnostics, not semantic proof;
   every other proposal still needs an auditable reviewer approval.
4. Send every `review` row to `da-lesson-reviewer` on Claude, Codex or Copilot with the exact row, **both complete
   current rulebooks**, evidence and the output of `learn.mjs review-key --id Kxx`. The fingerprint identifies
   the reviewed inputs; computing it is not approval. Copy the reviewer's three-line artifact verbatim into
   `## Review` of the current `.claude/goal-runs/*.md` run log (outside a code fence):
   ```text
   LESSON-APPROVED Kxx <fingerprint>
   LESSON-RULE {"target":"<exact target>","rule":"<exact rule>"}
   VERDICT=APPROVE
   ```
   Use valid single-line JSON for `LESSON-RULE`; the run needs a valid header and a row under `## Runs`.
   Record the reviewer delegation handle with it. Only after `APPROVE` and persisting this artifact apply
   the exact row with `learn.mjs apply --id Kxx --reviewed <fingerprint>`. The script requires both a current
   fingerprint and the matching artifact, and retains `review=<fingerprint>` in lifecycle statuses so `check`
   detects missing/deleted approval, a changed verdict or a changed target/rule after application. The record
   proves a visible review assertion, not authenticated agent execution; never manufacture it in the caller.
   A changed row or either book invalidates approval; `REWRITE` requires another review of the final row.
   Process rows sequentially because each applied rule changes a book. Record `REJECT` as `rejected (reason)`.
   `manual` rows are reported for explicitly authorized maintenance, never bypassed with a fingerprint.
   Application adds a cited rule and bumps `RULEBOOK:`; it cannot modify existing rules. Retirement below is
   the only automatic removal, limited to an exact lesson-generated line. Neither operation edits this skill or agents.

5. Run `sh <this skill's dir>/scripts/da-node.sh learn.mjs check` (also part of pre-flight) and record
   `LESSONS=OK|FAIL` in Evidence. A failing check, a missing run row or a rule contradicting a book is unfinished
   step 10, not a blocker for the code delivery — report it.
6. Commit the lesson rows, the book edits and the version bumps with the delivery (same branch, same PR); the run logs under `.claude/goal-runs/` stay local.
7. `learn.mjs validate` runs at the next start: three completed (`status: done`) runs with exactly one valid
   `recurred:` marker and a start date **strictly after** the application date allow validation. Date-only legacy
   records cannot establish same-day ordering, so same-day runs are excluded. Missing markers are not success.
   The first eligible recurrence removes the exact lesson-generated line, bumps the book version and marks
   it `reverted`; a modified, ambiguous, multiline or coverage rule requires explicit maintenance and fails
   validation with a persisted `blocked@<date>` status, reason and recurrence source in evidence. `check`
   rejects both unresolved blocked records and reverted lessons whose rule is still active. After authorized
   maintenance, remove the exact obsolete rule and set `reverted`, or restore `applied` with its original
   application date and approval stamp to retry a corrected target. Never erase the reason or approval history.
   Validated lessons retain `applied-at=<original date>` and remain monitored for later recurrence. Legacy
   validated rows without that field conservatively use their status date. Malformed headers stay in the run
   inventory, are counted as `invalid=N` and warned about; they never count as validation evidence. Active
   automatic-target legacy lessons without approval fail audit and need explicit review, not synthetic proof.
   File a refined lesson after retirement.


Seeded and historical rows are data, not law: only a rule line inside a rulebook changes behaviour.

## Attempt budget and stop conditions

Count attempted fixes of the same problem, inline or delegated; at most three informed attempts. Cap full
5→6→7→8 cycles per task at three. Attempt 2 must change the hypothesis, not only wording/agent. Log failures
and counters; changing agent or restarting does not reset them. A crash before any result is recorded as
interruption, not a new attempt. Stop sooner for missing credentials/services (Docker daemon unavailable, tools image cannot build, required local configuration, or repository-host authentication) or a user-only decision.

A capped task becomes blocked: explain the exact failure, attempts and real outputs, known/unknown facts,
code state, affected criteria/tasks and concrete next options. Continue independent work; do not spend a
fourth attempt without new user direction. Three capped tasks, or a capped sole task, require escalation.
Never lower checks, coverage or scope to evade a cap. Keep the run/goal incomplete while evidence is missing.
An escalated/stopped run still gets a concise report, actual cost availability and retro. Record its final
status; only a fully proven run is `done`.
