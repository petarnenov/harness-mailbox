---
name: da.sync
description: "Replicate the complete prefixed delivery setup into another project and adapt it to that project's actual stack. Copies every skill plus canonical agents and hook support, derives the target prefix from the project name, preserves target changes, updates stack rulebooks/build/test/deploy commands and host wiring, and verifies the result. Use whenever the user asks to copy, port, replicate, install or synchronize the skills/agents into another repository, including \"/da.sync <project>\" (targets use \"/<prefix>.sync <project>\"), \"copy the da skills\", or \"sync the setup to the target stack\"."
argument-hint: "<target-project-path> [--dry-run]"
globs: []
---

SYNC: da.sync@2026-11-05.1

# DA Sync — stack-aware project replication

Port the delivery system without porting the source repository's technology choices by accident. The deterministic
copier installs only missing files and never overwrites a conflict; the semantic work is to make the copied
instructions true for the target repository. Preserve the workflow, review and authorization gates even when the
languages, frameworks and deployment model change.

Hosts: Claude Code (`/da.sync`), Codex CLI (`$da.sync`) and GitHub Copilot CLI (`/da.sync`). The source is the
current repository's canonical `.claude/` package; Codex reads it through `.agents/skills/da.sync`.

## Prefix naming

The source prefix is discovered from this skill's own directory (`.claude/skills/<prefix>.sync`) and is never
hardcoded. The target prefix comes from the target directory's project name:

| target basename | target prefix |
|---|---|
| `payments` | `pa` (first two letters of one word) |
| `identity-service` | `is` (first letters of multiple words) |
| `document-knowledge-platform` | `dkp` |
| `fooBar` | `fb` (camel-case words) |

The result is lowercase. Every copied skill, agent, hook and internal reference is converted from the source
prefix to that target prefix, including `sync-<prefix>.mjs` and its tests. After the copy, invoke the target skill
as `<prefix>.sync <path to project>`; for example, `document-knowledge-platform` uses `dkp.sync`. A project name
that cannot produce a valid lowercase identifier fails the dry run before any write, so rename that directory
rather than hand-editing copied paths.

## Fixed rules

- Treat the target's `AGENTS.md`, `CLAUDE.md`, repository instructions and existing project conventions as the
  source of truth. Read them before changing anything. Do not weaken their checks to make the da package fit.
- Never copy `.claude/settings*.json`, user-level config, run history, credentials, tokens or machine-local paths.
  The copier includes canonical skills and agents, plus the three hook support files only.
- Dry-run first. The copier never overwrites an existing different file or symlink and rejects symlinked
  destination ancestors so writes cannot escape the target. Resolve conflicts as a real merge: preserve target
  adaptations and unrelated configuration, and back up any file before a manual overwrite.
- Do not copy source-project lessons into a new project's active ledger. The copier excludes the source ledger
  and installs `assets/lessons.template.md` as `da.loop/lessons.md` instead. Keep an existing target ledger and
  merge only deliberate portable lessons with their evidence; never copy run rows.
- Preserve generic procedures and safety invariants in `da.loop`, `da.merge`, `da.commit`, Jira skills and review
  agents: acceptance criteria, verification plans, risk gates, review, no unconfirmed writes, no silent merges,
  explicit-path staging, no `--no-verify`, no history rewrite, no implicit push, no AI attribution and no secrets.
  Adapt project facts, not these controls.
- Do not run the copied stack-specific preflight, renderer or path guard on the target until their stack constants and
  command allow-lists have been adapted. Otherwise they can produce false failures or reject valid target commands.
- No git mutation, Jira/Confluence write or remote-machine copy is implicit in this skill. The target is the local
  path supplied by the user unless remote access was explicitly requested.

## Procedure

When installing from a portable tarball, run `node .claude/skills/da.sync/scripts/install.mjs --target <absolute-project-path>` from the extracted package for a read-only dependency preview. Then run it with `--apply` to configure missing Atlassian, Zeplin and Chrome DevTools MCP servers on installed Copilot, Codex and Claude CLIs before copying package files. Use `--hosts copilot,codex,claude` to require specific hosts. The installer reports exact setup instructions and stops before copying when a prerequisite is unavailable. It does not authenticate on the colleague's behalf or prove a server can read data; the operator must complete host OAuth/token setup and verify real read-only tool calls before declaring the install complete. Continue with the stack adaptation and verification below after it copies files.

### 1. Resolve and preview

Resolve the source and target to absolute directories and confirm they differ. Record the target branch and
`git status --porcelain=v1 -uall`; preserve pre-existing work. Read all applicable target guidance.

Run from the source checkout (dry-run is the default):

```sh
node .claude/skills/da.sync/scripts/sync-da.mjs --target /absolute/path/to/target
```

The inventory is dynamic: every `.claude/skills/da.*` and `.claude/skills/da-*` directory, including `da.sync`,
every `.claude/agents/da-*.md` file, `.claude/hooks/path-guard.py`, `.codex/hooks.json` and
`.github/hooks/da-loop.json`. The dry run reports the derived `prefix=`, and every planned target path already
uses it. Review every `conflict` before applying. An existing `.codex/hooks.json` is expected to conflict when the
target already has hooks; merge its target-prefix entries instead of replacing the file.

After review, install the missing package files:

```sh
node .claude/skills/da.sync/scripts/sync-da.mjs --target /absolute/path/to/target --apply
```

Exit 2 means conflicts remain, not that existing content was overwritten. The command may still have added the
missing, non-conflicting files; inspect the reported actions and target diff.

### 2. Build the target stack map

Inspect target documentation, dependency manifests/lockfiles, source and test layout, CI and deployment files.
Write a short mapping before editing copied instructions:

| Fact | Required evidence |
|---|---|
| project name and repository/module layout | root docs and directories |
| languages, frameworks and maintained versions | manifests and build config, not guesses |
| architecture boundaries and security invariants | source, ADRs and existing guidance |
| format/lint/typecheck/build commands | package scripts, build files and CI |
| unit/integration/E2E commands and coverage policy | test config and CI |
| runtime/deployment consequences | Docker, CI/CD, infra and runbooks |
| issue tracker, board, Confluence and ticket defaults | target docs or user confirmation |
| supported AI hosts and existing agent/hook config | target `.claude`, `.agents`, `.codex`, `.github` |

Distinguish verified facts from unknowns. Ask about an unknown only when it changes safety or acceptance criteria;
otherwise leave a concise `TODO(target)` instead of inventing a command, version, ticket key or URL. Use the
target's current commands exactly, including the directory from which each runs.

### 3. Adapt content, names and references

The copier has already applied the target prefix to names, content and links. Do not change that prefix during
stack adaptation. Choose a short lowercase `<stack>` slug from the primary target stack (for example `spring`,
`react` or `node`). For a multi-stack repository, choose the slug of the primary delivery stack and document every other module or stack in the rulebooks and stack reference. This package's own stack slug is `python`. Rename the stack-specific names consistently:

- `da-python-conventions` → `da-<stack>-conventions`;
- `da-python-test-conventions` → `da-<stack>-test-conventions`;
- `da-python-writer`, `da-python-test-writer`, `da-python-reviewer` → the corresponding
  `da-<stack>-*` names;
- `RULEBOOK: da-python...` markers and every `skills:` entry, script constant, path, handoff and documentation
  reference to those names.

The source-prefix strings shown above become the target prefix automatically during the copy (for example,
`dkp-micronaut-conventions` → `dkp-<stack>-conventions`). Rename `da-lesson-reviewer` only if the target already
uses a different consistent convention. Update `.agents` links after every skill rename so they point to the
renamed canonical directory.

Adapt the following by evidence, not by global find-and-replace:

- **`da.loop`**: project/stack description, module facts, risk examples, implementation paths, build/test gates,
  deployment checks and host notes. Keep its delivery state machine and gates.
- **code and test rulebooks**: derive rules from the target's architecture and established patterns. Remove
  source-stack rules that do not apply; carry over only genuinely relevant security,
  contract, testing, determinism and coverage invariants. Never retain a fabricated source citation.
- **writer/test-writer/reviewer agents**: role boundaries, allowed paths and commands, rulebook names and handoff.
  Keep production/test separation and read-only review unless target instructions explicitly define a stricter one.
- **`da.loop/references/stack.md`**: rewrite as the target's concise operational fact sheet. Keep
  `cost-controls.md` generic and verify model/host claims instead of copying assumptions.
- **scripts**: adapt agent/book lists, required toolchain, module checks, host files and command allow-lists in
  `preflight.mjs`, `render-agents.mjs`, `learn.mjs` and `path-guard.py`. Keep `g1-spec-review-hook.mjs`,
  `da-node.sh` and the learning algorithm unless a target contract requires a change.
- **`da.commit`**: replace source-project commit assumptions, Jira key source, and branch guidance,
  Ignore/Commit/Ask path examples, secret names and rendered-agent check with the target's verified facts (its
  instructions first, then `git log`). Fall back to Conventional Commits only when the target has neither a stated
  rule nor a consistent history. Keep the operating-rules table unchanged.
- **Jira and merge skills**: preserve procedures and confirmation/read-only rules. Replace identity-specific
  project trees, boards, issue families, role slices, estimate reference work and examples only with verified
  target facts. If none exist, make inputs required rather than defaulting to GEO tickets.
- **historical origin references**: leave `references/geo.loop-origin/**` unchanged as provenance, not active
  target guidance.
- **`da.sync`**: keep portable; its source default resolves to whichever repository contains the copied skill.

### 4. Merge host wiring without losing target config

Canonical Claude agents and skill frontmatter already carry their hooks. Preserve them after adaptation.

- **Codex skills:** ensure one relative symlink per canonical skill:
  `.agents/skills/<name> -> ../../.claude/skills/<name>`.
- **Codex agents:** when `.codex/config.toml` is absent, run the adapted renderer to produce the agent TOMLs and
  initial config. When it exists, do not run the current whole-config renderer in the real target: render in an
  isolated temporary target copy, then merge only da-owned registrations into the real config. Preserve unrelated
  target model, MCP, provider and agent settings byte-for-byte where possible and test this with a pre-existing
  MCP/provider fixture. The isolated copy must contain the adapted canonical agents and renderer.
- **Codex G1 hook:** merge the target-prefix `PreToolUse` and `Stop` entries into existing `.codex/hooks.json`, deduplicating
  by command. Do not remove other hooks.
- **Copilot:** render `.github/agents/*.agent.md` and retain `.github/hooks/da-loop.json`; merge rather than replace
  if the target already owns that file.
- **Claude settings:** do not copy source settings. Add only an explicitly reviewed target-specific SessionStart
  notice if the target wants one.
- **Guidance:** update target `AGENTS.md`/`CLAUDE.md` with invocation, canonical/rendered ownership and rulebook
  locations. Preserve all unrelated content.

### 5. Verify and report

Run checks appropriate to the adapted target, at minimum:

```sh
node --test .claude/skills/da.sync/scripts/sync-da.test.mjs
node .claude/skills/da.sync/scripts/sync-da.mjs --help
```

The target copy uses the same commands with the derived prefix in place of the source prefix. Run the ported tests from the
target; they prove the renamed paths, internal references and executable remain usable. For the real post-copy
sync check, rerun the original source → target dry-run: `add=0` is expected only for files left byte-identical;
adapted files correctly appear as `conflict` and must not be overwritten.

On the target, validate every changed `SKILL.md` frontmatter, all renamed references and relative symlinks, run the
adapted learning tests, lessons validation and stack-appropriate preflight for each supported host. Verify copied
paths and active text use only the derived target prefix, except deliberate historical provenance. Run renderer
`--check` in the real target only when its whole `.codex/config.toml` is renderer-owned; for a merged pre-existing
config, run the check in the isolated rendered copy, compare the generated target-prefix registrations with the
real config, and verify unrelated settings are unchanged. Run target build/test/config checks when scripts or
executable hooks changed. Review the final target diff for stale source-project names, stack terms, ticket prefixes and module names; a match is acceptable only in historical provenance or an explicitly portable example.

Report: source and target, derived target prefix and invocation, detected stack and evidence, copied/renamed/merged
files, preserved conflicts, unknown `TODO(target)` items, exact verification results and any host hook that still
needs user trust. Do not claim the target is synchronized while stack adaptation or verification is pending.
