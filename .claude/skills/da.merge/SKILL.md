---
name: da.merge
description: "Merge a named branch into the current harness-mailbox dashboard branch with auditable conflict resolution, scoped verification and no implicit commit or push. Use for /da.merge <branch>."
argument-hint: "<incoming-branch-or-sha>"
---

LOOP: da.merge@2026-11-16.1

# DA Merge

Merge one explicitly named incoming branch into the current branch. Preserve unrelated working-tree changes. Do not commit or push. If any conflict requires a product or architecture decision, stop before resolving that conflict and present the concrete alternatives.

## Fixed rules

- Require the incoming branch or SHA as an argument; never infer it from the current branch.
- Record branch, HEAD, merge base, full status, staged/unstaged diff, Git configuration affecting merges, and the state of the named ref before mutation. Preserve all pre-existing work.
- Do not fetch or update remote refs implicitly. If the requested remote ref is absent or stale and cannot be verified from local evidence, stop and ask the user to fetch/update it.
- Create a local safety tag at the current HEAD and a resumable state record before starting. Do not overwrite an existing tag.
- Do not stash, reset, clean, rewrite history, disable hooks, or use force operations. Do not alter global Git configuration.
- Make conflict resolutions only when the intent is mechanically clear from both sides and current repository guidance. Keep a record of each decision and its evidence.
- Never include secrets, tokens or customer data in logs or reports.
- Do not commit or push. The user decides whether to commit the resulting merge.

## Procedure

### 1. Recon and baseline

Run `git status --porcelain=v1 -uall`, `git branch --show-current`, `git rev-parse HEAD`, `git merge-base HEAD <incoming>` and inspect both sides' diffs. Confirm that the incoming ref resolves locally or remotely. Capture changed paths and classify intersections, generated files, contracts, migrations, build configuration and deployment files. Use a task-specific directory under `.git/da.merge/` for state and evidence so it survives session restarts without entering the commit. Record the incoming ref and SHA, current branch and SHA (`OURS_SHA`), merge base (`BASE`), safety tag, phase, conflicts and decisions. For each side, capture the pre-merge diff and scoped build/test baseline when applicable; if a baseline cannot run, record the exact reason.

Inspect `git --version`, `git config --get rerere.enabled`, `git config --get rerere.autoupdate`, `git config --get merge.conflictStyle`, `git config --get core.hooksPath`, and repository `.gitattributes` for custom merge drivers. The merge itself must disable rerere replay and autoupdate locally. List custom drivers that affect changed files and escalate if their behavior cannot be audited.

If a merge is already in progress and the state record identifies the same incoming SHA, resume from that phase and preserve prior baselines and decisions. If a merge is in progress without matching state, stop. Use a non-merge temporary worktree for incoming-side baselines and remove only the worktree created for this run after verification. Never delete existing user data.

If either side's state cannot be captured safely, or uncommitted changes overlap the merge surface, stop and report the exact issue before starting the merge. A dirty tree that is fully disjoint may proceed only after its paths and diffs are recorded and protected in the state file.

### 2. Predict and verify conflict zones

Use `git merge-tree --write-tree HEAD <incoming-sha>` where available to identify likely conflicts and their kinds before merging. If unavailable, inspect in an isolated throwaway worktree; never perform a trial merge in the real checkout. Compute `OURS_SURFACE`, `THEIRS_SURFACE`, `CONFLICTS`, and `OVERLAP = (OURS_SURFACE ∩ THEIRS_SURFACE) − CONFLICTS`; inspect both diffs for every conflict and overlap file. Record conflict kind, each overlapping path, module impact, test selection, and planned gates in the state file. Establish and capture scoped baselines on both current and incoming revisions before the actual merge. If the incoming baseline is unavailable, say so and retain that limitation in the final report. Pay particular attention to:

- the `/api/dashboard` payload shared by `server.py` and `static/app.js`;
- mailbox record and receipt formats shared by `dependency_graph.py`, `dispatch_gate.py`, `delivery_process.py` and the workspace orchestrator scripts;
- the read-only server boundary, security headers, guarded reads and gate deny paths;
- workspace `AGENTS.md`/skill protocol text, especially Jira/AIO write limits and gates;
- `Makefile`, `scripts/workflow.sh`, Dockerfiles, `compose.yaml`, `requirements-dev.txt`, environment variables and secret references;
- agent/rulebook names, generated adapters and hook configuration.

A clean textual merge does not prove compatibility. Identify affected components (backend, frontend, orchestrator scripts, container/Make workflow) from the changed paths. Invoke validation only through the Make targets from the repository root: `make check` (tests and lint), `make coverage`, and `make build` when Docker or runtime files changed; check `make help` for the available targets. Do not substitute host Python, Node or direct Docker runs.

### 3. Merge and resolve

Start only after the pre-merge state, both-side baselines and conflict map are recorded. Use a non-committing merge with local rerere controls: `git -c rerere.enabled=false -c rerere.autoupdate=false merge --no-commit --no-ff <incoming-sha>`. This prevents automatic resolution replay and keeps commit authority with the user.

For every conflict, inspect the base/ours/theirs versions (`git show :1:<path>`, `:2:`, `:3:` when present) and the commits that changed each side. Never blanket-select ours or theirs for text files. Resolve only when the intent is mechanically clear and both sides’ intent survives; otherwise stop and present the exact alternatives for user decision before editing that file. Record the user’s decision before applying it. Use a side wholesale only for an appropriate binary or explicit user decision, and document which side and why.

After resolution, confirm the unmerged-path list, conflict-marker scan (including diff3 base markers), and status are clean. Compare every conflicted path’s `BASE..OURS_SHA` changes with the merged file and count the current-branch hunks that remain (`n/n` per file). A missing hunk is dropped work: restore it if mechanically understood or escalate. Inspect `git diff "$OURS_SHA" -- <path>` against the uncommitted merged tree after resolution to ensure added differences are attributable to the incoming branch or a documented resolution. Require explicit user approval for semantic conflicts; no silent merge decisions.

### 4. Verify

Run the same scoped checks on both pre-merge baselines and the merged tree, using the Make targets only (`make check`, `make coverage`, and `make build` when runtime files changed). For a shared record or payload format, run the tests of every reader. For configuration and shell changes, include `make lint` and `make doctor`. Run gates sequentially; they share the repository mount and `.coverage-output/`. Compare post-merge results against both baselines to distinguish regressions from failures already present on either side. Also run new/changed tests introduced by the incoming branch. Report commands, working directories and outcomes; do not claim a test passed if it was not run.

For every conflicted file and overlap file reviewed, report the hunk-preservation count and whether the final change is attributable to ours, theirs, or an approved resolution. Re-read the full final diff for risky files and shared contracts before reporting.

Inspect the final diff against both parents, status, and merge result. Confirm that unrelated pre-existing changes remain intact. If verification exposes an unresolved behavioral choice or a failure whose cause is unclear, leave the merge state intact and report it; do not abort or reset without explicit instruction. The final report includes the exact `git merge --abort` escape command, but do not run it implicitly.

## Report

Include incoming and current refs, merge base, conflict count and resolution rationale, changed components, verification results, remaining risks or decisions, and the current staged/unstaged state. State clearly that no commit, push, or implicit remote fetch was made. Include the safety tag, saved state directory, incoming/current/base SHAs, rerere/autoupdate status, merge drivers, conflict and overlap counts, semantic decisions, per-file hunk-preservation counts, baseline/post-merge verification comparison, and any unverified areas. If user decisions remain, leave the merge resumable and state where its evidence is saved.
