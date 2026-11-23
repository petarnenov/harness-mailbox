# da.loop host wiring

The workflow in `SKILL.md` is shared across supported hosts. This file records repository-owned paths and wiring; host CLI behavior and available integrations may vary by installation, so verify them in the current host before relying on a hook or connector.

| | Claude Code | Codex CLI | GitHub Copilot CLI |
|---|---|---|---|
| invoke | `/da.loop <goal>` | `$da.loop <goal>` | `/da.loop <goal>` |
| canonical skills | `.claude/skills/` | `.claude/skills/` through `.agents/skills/` symlinks | `.claude/skills/` |
| repo guidance | `CLAUDE.md` and `AGENTS.md` | `AGENTS.md` | `AGENTS.md` and, if supported, `CLAUDE.md` |
| canonical agents | `.claude/agents/*.md` | `.claude/agents/*.md`, rendered into `.codex/agents/` | `.claude/agents/*.md`, rendered into `.github/agents/` |
| renderer | `.claude/skills/da.loop/scripts/render-agents.mjs` | same renderer | same renderer |
| G1 hook config | skill frontmatter and `.claude/skills/da.loop/scripts/g1-spec-review-hook.mjs` | `.codex/hooks.json` | `.github/hooks/da-loop.json` |
| run close hook | skill frontmatter (Stop) and `.claude/skills/da.loop/scripts/run-close-hook.mjs` | `.codex/hooks.json` (Stop), trust once with `/hooks` | none (no Stop event); pre-flight reports the unclosed run at the next start |
| ADR gate | writer agent Stop hook (`.claude/skills/da.loop/scripts/adr-gate-hook.mjs --event writer-done`) marks the run; skill PreToolUse (`Agent\|AskUserQuestion`) and Stop gate it | none (no per-agent hook); step 5 text and closing contract (`ADR-review: PASS`) | none (no per-agent hook); step 5 text and closing contract via pre-flight |
| path guard | `.claude/hooks/path-guard.py` | role boundary in the agent prompt plus diff review | role boundary in the agent prompt plus diff review |
| run logs | `.claude/goal-runs/` | same | same |

Canonical agents are maintained in `.claude/agents/`. After changing an agent, run the repository renderer from the root and review generated adapters. Do not edit rendered adapters by hand. Preserve existing host configuration when merging hook or agent registrations.

The project package contains Jira helpers, but the availability and authentication of issue-tracker tools are host-specific. Read helper instructions before any external write; comments and estimates require their documented confirmation gates. If a required read-only integration is unavailable, record that limitation and continue with repository evidence where possible.

Do not claim a hook is active because its config file exists. Check the running host's hook/trust state. Where a hook is unavailable, the written G1 and role-boundary rules still apply.
