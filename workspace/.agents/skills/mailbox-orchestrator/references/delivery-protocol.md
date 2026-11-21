# Ticket intake, gates and integration protocol

This protocol applies to any supplied Epic, Jira issue, Story or Bug. The user's explicit workflow delegates project questions, G1/G2 decisions, PR review and integration merge to the orchestrator. It does not authorize Jira writes or deployment. Apply the project's loop protocol throughout; the orchestrator replaces the human decision recipient for these gates, not the required reviews, tests or evidence.

## 1. Mandatory project baseline before planning remaining work

Before every task, including this baseline, every project agent must satisfy [mandatory integration synchronization](../SKILL.md#mandatory-integration-synchronization-before-every-task): synchronize its confirmed integration branch with `origin`, preserve local changes, and record branch, before/remote/after SHAs and command results. A fetch alone is insufficient. Coordinate delegated agents through the checkout owner and resolve synchronization blockers before task execution.

After reading the requested scope and identifying affected projects, request a full baseline report from every relevant project session using its actual `<prefix>.loop`. Intake discovery can proceed, but do not finalize remaining work or dispatch implementation until the required baseline reports have been received and reconciled. An unavailable worker is an explicit intake blocker.

Each report covers:
- issue/Epic and acceptance criteria, actual session ID, repository and working directory;
- startup integration branch, evidence for that choice, current branch and HEAD, remote and integration SHA;
- relevant existing implementation, changed files, commits, worktrees, open/merged PRs and integration state;
- uncommitted work and ownership, without overwriting or incorporating unrelated changes;
- completed/partial/missing acceptance criteria, test commands and outcomes, coverage, review findings, existing G1/G2 decisions and their scope;
- dependencies, blockers, unresolved questions and remaining work.

Distinguish observed evidence from assumptions. If no prior work exists, report that explicitly with the inspected scope; do not manufacture a history. Store reports as `reports/<scope-key>/<project>/<timestamp>-<UUID>.json` with artifact links and an optional readable companion. The worker publishes the report through its outbox and the orchestrator inbox, sends a short notification when thread transport is available, and links the report from its task. The orchestrator records which report revision it accepts, reconciles overlap and existing progress, records its intake assessment, and only then plans remaining tasks. A missing report is not evidence that no work exists; do not dispatch duplicate implementation while intake is unresolved. For an Epic, cover all affected child issues rather than only the Epic description. On resume, request changes since the accepted baseline instead of duplicating the same work.

## 2. Immutable startup integration branch and isolated worktrees

Every worker uses the branch from which its project session started as its integration branch. Capture `integration_branch`, `integration_remote`, `integration_start_sha`, `integration_branch_source` and `startup_working_directory` in its own registration at startup. Existing sessions must reconstruct this from reliable startup context/history and confirm it in the baseline. Do not substitute the current branch, registry expected_branch, default branch or task branch when historical startup evidence is missing. Route ambiguity to the orchestrator; it resolves from evidence or asks the user if the actual starting branch cannot be established. A detached starting HEAD also requires an explicit integration target.

Freeze this binding for the session's assignment. Changing it requires explicit user direction, not automatic registry refresh. Child task agents inherit their owning project's integration binding, even though they are launched inside a feature worktree. Multiple sessions of the same project may have different bindings; preserve them separately and bind each task to one exact target.

Every implementation task runs on a dedicated task branch in a separate Git worktree based on the confirmed integration branch. Record `worktree_path`, `task_branch`, `base_sha`, `integration_branch`, `integration_remote`, owner and affected paths before editing. Keep the integration checkout as the integration target; completing a task in a worktree does not change the session binding. Each assignment and delegated subtask must carry this binding explicitly. Preserve relative application paths inside the worktree (for example an app/ directory). Inspect existing worktrees and dirty state; never reset or stash someone else's work automatically. Baseline analysis is read-only; no implementation goes into the session's integration checkout.

## 3. Questions and G1/G2 decisions

Workers send questions to the orchestrator through the mailbox with task ID, loop stage, exact question, evidence, options, recommendation and affected work. The orchestrator follows the project's loop escalation/decision rules, answers from accepted requirements and evidence, and records the rationale. Continue independent authorized work while a dependent decision is pending. If a decision would invent product requirements, change user scope or require unavailable authority, escalate that specific unresolved question to the user. Never infer an answer from silence.

Store immutable decisions under `decisions/<task-id>/<UUID>.json`: request/message ID, decision type, question/gate, scope, verdict (`accepted`, `changes_requested`, `blocked`), rationale, evidence, decision timestamp and orchestrator identity.

Before delivering any new execution assignment or resume, apply [dependency-gate.md](dependency-gate.md). Publish only through `scripts/dispatch_task.py dispatch`; the worker validates the bound receipt/message against current dependencies before claiming or changing the task. A G1/G2 decision does not bypass an unmet dependency or independently resume held work. A changed dependency snapshot requires a new gated execution request, not a direct thread prompt. Status/registration/reference consultations are not execution assignments.

G1 and G2 both require explicit recorded orchestrator acceptance for this workflow. Preserve their timing in the project's loop: G1 accepts the specification/implementation and verification plan before implementation; G2 accepts the final verified change before PR creation. Require the loop's specialist reviews and checks. If the local loop normally omits a gate for low-risk work, record a proportionate explicit acceptance of that gate rather than silently skipping it. Do not approve G1 retrospectively to legitimize implementation started without it; record the deviation and follow remediation.

Bind G1 to the exact criteria/plan revision and G2 to base SHA, task head SHA and evidence. A changed scope invalidates affected G1 acceptance; changed code or integration base requires revalidation and refreshed G2 evidence before advancing. A worker cannot approve its own gate. The orchestrator records its acceptance or requested corrections and replies through the mailbox, linking the decision ID. The worker records the reply in its loop evidence before proceeding to the dependent stage. Rejection returns to the owning worker under the same loop.

Every question or gate request must identify the verified integration binding, task/worktree and applicable plan revision or commit, and link the required loop evidence. A thread reply alone is not the durable decision: publish the decision and reply through the mailbox before the worker advances. Keep the affected stage pending until explicit acceptance arrives; continue only independent work already authorized by the accepted scope.

**Delivery unit.** The intake baseline ends at G1. Implementation runs in one execution task per Jira Sub-task, each with its own gated dispatch, PR(s), G2, merge, post-merge verification and Closed transition. The dispatch gate refuses an execution task linked to several Sub-tasks; only investigation tasks may span several.

## 4. PR, approval and merge

After both gates are accepted, the worker publishes its task branch and creates a PR targeting the recorded integration branch. The user's workflow authorizes this task-branch push and PR creation; it does not authorize direct pushes to the integration branch, force pushes, unrelated publishing or deployment. Record PR URL/number, base/head refs and SHAs and gate decision IDs, then notify the orchestrator.

The orchestrator reviews the actual current PR diff, acceptance evidence and required checks, approves through the hosting provider when permitted, and merges the PR into the exact integration branch. Verify target repository and branch immediately before merging. Revalidate if the head/base has changed; serialize competing integration merges for a shared target and refresh affected checks. Return conflict resolution to the owning worker using its loop/merge protocol rather than improvising source edits in the orchestrator.

Respect branch protection and enforced independent reviews. Under the explicit user authorization recorded in mailbox AGENTS.md, accepted G2 authorizes the orchestrator to merge the reviewed PR into its confirmed integration branch without another human approval request. If the available account authored the PR and cannot submit a provider approval, record the orchestrator's local acceptance separately; an optional separate provider approval is not a blocker. Obtain an eligible reviewer only when enforced provider protections require one. Never impersonate a different reviewer, bypass controls or claim a provider approval succeeded when it did not. Missing credentials, merge permission or an unmet enforced protection is a blocker, not grounds for a direct integration push.

Verify the provider reports the PR merged and capture merge commit SHA, actual target, merged_at and check results. Request required post-merge verification from the worker in an isolated checkout of the integrated revision. Failed verification reopens the task under the same loop. Never mark work delivered based only on a green PR or requested merge.

## 5. Durable handoff and completion

Use the mailbox's UUID, atomic-write and reply/archive protocol. Add scope key, Epic/issue key, session identity and task ID to all related records. Workers own baseline and task evidence; the orchestrator owns decisions, intake assessments, aggregate progress and delivery receipts. Link loop artifacts instead of duplicating their canonical contents.

Store `deliveries/<task-id>.json` with baseline report links, integration binding, task/worktree details, G1/G2 decision IDs, question/answer links, review and test evidence, PR URL and head/base SHAs, provider approval result, merge SHA/time, post-merge verification and remaining risks. The worker sends its final handoff to the orchestrator and receives the recorded acceptance outcome. The handoff states what changed, acceptance-criteria outcomes, verification commands/results, integration branch and merged SHA, unresolved limitations, and links to the baseline, gates, PR, merge and post-merge artifacts. The orchestrator updates local Epic/project progress from that evidence; neither party writes Jira comments or statuses. Archive processed requests without generating acknowledgment loops.

The final report explicitly separates work found in the accepted baseline from work delivered by this task. Record remaining work or state that none remains, and identify the exact integrated revision that was verified. The orchestrator records its acceptance or requested fixes against that report revision before counting the task as verified in project or Epic statistics. A successful merge without this report and verification remains pending completion.

Track delivery phase separately from existing task statuses: `baseline_pending`, `baseline_accepted`, `g1_pending`, `implementing`, `g2_pending`, `pr_open`, `approved`, `merged`, `integration_verified`. Expose blockers separately. For implementation tasks, verified completion requires integration_verified plus acceptance evidence. Report PR-open or merged-but-unverified progress explicitly. Read-only analysis tasks need their protocol evidence and acceptance but do not create empty PRs or fabricate merge records. Keep Jira read-only throughout.
