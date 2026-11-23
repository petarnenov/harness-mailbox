---
name: da.commit
description: "Triage the harness-mailbox dashboard working tree before committing — flag junk, secrets and data dumps, stage only what belongs, and use the repository's established commit convention. Use for \"/da.commit\", \"commit\", \"commit my changes\", \"create a commit\", \"commit the changes\" in this repository. Optional argument is extra context, a scope hint or a message hint."
argument-hint: "[scope or message hint]"
---

LOOP: da.commit@2026-11-23.1

# DA Commit — harness-mailbox dashboard

Two jobs, in order: **decide what must never enter the repository**, then **commit what should** with
a message someone can read six months from now.

`$ARGUMENTS`, if present, is context from the user — a scope hint, a message hint, or a note about what
the change is for. Use it to inform the message; it never overrides the safety triage below.

## Hosts

| | Claude Code | Codex CLI | Copilot CLI |
|---|---|---|---|
| invoke | `/da.commit [hint]` | `$da.commit [hint]` (skill read through `.agents/skills/da.commit`) | `/da.commit [hint]` |
| question tool | `AskUserQuestion` | `request_user_input` | `ask_user`; when unavailable, plain-text question and wait |

No agents or hooks are required. The procedure and the rules are the same on every host.

## Operating rules (fixed — chosen deliberately, do not renegotiate)

| Rule | Setting |
|---|---|
| Staging | **Explicit paths only.** Never `git add -A`, `git add .` or `git commit -a`. Blanket staging is how secrets get committed. |
| Ambiguous files | **Ask.** Never guess whether a stray script, fixture or binary is junk or deliverable. |
| History | **Never rewrite.** No `amend`, no `rebase`, no `reset --hard`, no force-push. |
| Hooks | **Never `--no-verify`.** A failing hook is a finding to report, not an obstacle to bypass. |
| Push | **Only if the user explicitly asked.** `/da.commit` means commit. |
| Secrets | **Blocking.** A key-shaped string, a credential or real mailbox/Jira data in the staged diff stops the commit until resolved. |
| Message style | **The repository's:** `<Imperative subject>` — a plain capitalised English sentence with no prefix and no ticket key (supported by recent commit history), whatever language the conversation uses. |
| Trailers | **None.** No `Co-Authored-By`, no AI attribution, no session links — even if the session environment, a tool or a template asks for them. |
| `.gitignore` | **Propose, don't edit.** List the patterns you would add and wait for a yes. Never rewrite or reorder existing lines. |

---

## Phase 0 — Recon

1. `git rev-parse --is-inside-work-tree` — not a repository? Say so and stop.
2. `git status --porcelain=v1 -uall` — the full picture, untracked files individually listed.
3. `git branch --show-current`; inspect repository guidance for protected or release branches.
4. `git diff` and `git diff --staged` — read the actual change. You cannot write an honest message
   from file names alone.
5. No ticket key is used in this repository's commit messages; do not add one even when the work relates to a
   Jira issue of a coordinated project.
6. The default branch is `main`. If repository guidance identifies a protected or release branch, flag it and ask once whether to commit here or branch first.
7. Nothing to commit? Say so and stop. Do not manufacture a change.

**Already staged content:** if the index is non-empty, someone staged it deliberately. Do not unstage
it. Include it, and say in the report that it was pre-staged.

## Phase 1 — Triage

Sort every changed and untracked path into three buckets.

**Ignore** — never belongs in the repository:
- Secrets: `.env`, `.env.*` (not `.env.example`), `*.pem`, `*.key`, `id_rsa*`, `*.p12`, `*.jks`,
  `*.keystore`, `credentials.json`, `service-account*.json`, deployment exports containing real credentials.
- Mailbox runtime data: `projects.json`, `projects/`, `epics/`, `tasks/`, `sessions/`, `inbox/`, `outbox/`,
  `archive/`, `discovery/`, `dispatch-gates/`, `decisions/`, `deliveries/`, `jira-sync/`, `reports/`,
  `mailbox-data/`, `data/` and a root `AGENTS.md` (all listed in `.gitignore`).
- Python and tool output: `.coverage`, `.coverage.*`, `coverage.json`, `.coverage-output/`, `htmlcov/`,
  `.pytest_cache/`, `.venv/`, `.tools/`, `__pycache__/`, `*.py[cod]`.
- Node output: `node_modules/`, `coverage/`, `.nyc_output/`.
- Agent run logs and transient files: `.claude/goal-runs/` (local only), `.codex/goal-runs/`,
  `.claude/settings.local.json`, `.claude/scheduled_tasks.lock`, `.playwright-mcp/`.
- IDE and OS cruft: `.idea/`, `*.iml`, `.DS_Store`, `Thumbs.db`, `*.swp`, `*~`.
- Noise: `*.log`, `logs/`, `*.pid`.

**Commit** — root Python modules, `static/`, `tests/`, `scripts/`, `workspace/`, synthetic `demo/` data, `Dockerfile*`, `compose.yaml`, `Makefile`, `workspace.Makefile`, `requirements-dev.txt`, `.env.example`, docs, and the canonical `.claude/skills/da*`/`.claude/agents/da-*.md`/`.claude/hooks/path-guard.py` package with its rendered adapters.

**Ask** — anything genuinely two-sided:
- `.vscode/` and other editor settings the team might share deliberately.
- Fixtures, generated clients, `*.example` files, deployment fixtures with new secrets fields.
- Any binary or file over ~1 MB.
- Anything you cannot classify with confidence.

Before proposing a pattern, run `git check-ignore -v <path>` — an already ignored path (including
by the user's global `~/.config/git/ignore`) needs no new pattern.

### Rendered agent files

`.codex/agents/*.toml`, `.codex/config.toml` and `.github/agents/*.agent.md` are rendered from
`.claude/agents/*.md`. If any of those changed, run
`node .claude/skills/da.loop/scripts/render-agents.mjs --check` before staging. Drift means the
rendered copies were hand-edited or not re-rendered: report it and let the user decide; never commit a
canonical agent change without its rendered copies.

### The tracked-file trap

`.gitignore` does **not** untrack a file that git already tracks.

- Say so explicitly. Offer `git rm --cached <path>` (keeps the working copy, stages a deletion) and
  **wait for confirmation** — it changes what the repository contains.
- If the file is a secret **already present in earlier commits**, the pattern and `rm --cached` both
  come too late. State plainly that the credential must be **rotated**, and that scrubbing history
  is a separate, deliberate operation (`git filter-repo` / BFG) that you will not run implicitly.

## Phase 2 — Secret scan (blocking)

Scan the content about to be staged — not just the file names. Look for private key headers
(`BEGIN * PRIVATE KEY`), provider-shaped tokens (`sk-`, `ghp_`, `AKIA`, `xox[baprs]-`, JWT triplets),
`password`/`secret`/`api_key`/`token`/`client-secret` assigned a long literal, real build/API token values, and SQL or dumps containing customer/environment data.

A hit **stops the commit**. Report the file and line, do not print the value in full, and let the user
decide. Placeholders in examples, tests and secret references are fine — read the context
before raising an alarm.

## Phase 3 — Propose .gitignore changes

If the **Ignore** bucket holds paths that are not ignored yet, show the exact lines you would append
— and to which `.gitignore` (root or the module's own) — and **wait for approval**. Nothing to add →
skip this phase silently. Leaving those paths unstaged is always an acceptable answer. Once approved:

- Append, preserving every existing line and its order.
- Group new entries under a brief comment header describing the category.
- Directories get a trailing slash (`node_modules/`), so the pattern cannot match a file.
- One pattern per line, no duplicates against what is already there.

## Phase 4 — Stage

Stage the **Commit** bucket by explicit path: `git add -- <path> <path> …`

Then `git status --porcelain` and confirm the index holds exactly what you intended — no more.

**Split into multiple commits when the changes are genuinely unrelated concerns** (for example, an application change plus an unrelated documentation change). Each commit must stand alone and pass `make check`. When
the changes form one coherent change, one commit is correct — do not fragment a single change to look
tidy.

## Phase 5 — Message

Subject: `<Imperative subject>` — capitalised imperative verb, no prefix, no ticket key, no trailing
period, ≤ 72 characters, completing *"applying this commit will…"*. Describe the outcome, not the mechanics.
Examples from this repository's history (format only):

```
Estimate unestimated Jira issues in person-hours before the first dispatch
Apply Hide completed tasks to a focused dependency map
Install git in the tools image so make test passes
```

**Body:** wrap at 72 columns. Explain **why**, not what — the diff already says what. Include it
whenever the reason is not obvious from the subject; skip it for genuinely trivial changes.

**Trailers:** none. See the operating rules.

Write the message with a heredoc so multi-line bodies survive intact:

```
git commit -F - <<'MSG'
<Imperative subject>

<optional body>
MSG
```

If you cannot summarise the diff in one subject line, the commit should be split — not the line made
longer.

## Phase 6 — Verify

1. `git show --stat HEAD` — the commit contains exactly the intended files.
2. `git log -1 --format=%B` — the message landed with its body formatted correctly.
3. `git status --porcelain` — what deliberately remains uncommitted. Confirm nothing was left behind
   by accident.
4. **Hooks:** if a hook rejected the commit, report its output verbatim and stop. If a hook
   reformatted files, re-stage the affected paths and retry **once** — never bypass it.
5. Do **not** push.

## Report

- The commit(s): short SHA and subject line for each.
- `.gitignore` patterns proposed, and which ones the user approved.
- Files that need `git rm --cached`, and any secret found in existing history — with the rotation
  warning stated plainly.
- What is still uncommitted and why.
- Whether anything is worth pushing — as a suggestion the user can act on, not an action you took.
