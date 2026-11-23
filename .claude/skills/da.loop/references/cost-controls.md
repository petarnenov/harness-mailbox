# Cost controls — da.loop

Read when planning delegations, selecting a model or recording usage. Keep these controls outside routine implementation briefs; pass only selected routing and accounting requirements.

## Model routing

Claude uses the model aliases in its canonical agent profiles. Codex and Copilot inherit model selection from their current session. Select a supported model for the task when the host permits an override. Preserve recorded attempts when changing models.

## Usage records

Record under `## Cost` of the run log: delegation count, maximum parallelism, per-agent tokens and duration as the host reports them, and whole-session usage when available. Missing counters are `unavailable`, never estimated. Count parent and children separately; never sum a parent total that already includes children. Inline work is never recorded as zero usage.

## Context and integrations

Briefs contain owned criteria, allowed files, base, components and exact Make commands from `references/stack.md`, relevant findings and evidence paths. This repository runs builds and tests through its Make targets from the repository root; do not give delegated workers host Python, Node or Docker commands as gate substitutes. Avoid full conversation forks when a focused context is supported. Group related tasks without merging ownership; reuse agents for follow-ups. One coherent review covers the settled change across components.

Ticket work uses the configured issue-tracker tools. Repository-host review or CI inspection uses the host CLI and remote configured for this checkout; do not assume a host, project key or board from another repository. Browser work only when the goal asks for it, with the available browser skill.

## Build ownership

One integrated build/test owner at a time per component. Task agents may inspect and edit their assigned files, but the main conversation coordinates repository-root Make builds and tests to avoid competing Docker processes and `.coverage-output/` writes. The main conversation runs the integrated component test and coverage measurement once on the settled change. `make coverage` writes its reports to `.coverage-output/`; verify the report belongs to the tested revision before relying on it.

## Canonical files

Everything da.loop needs is inside this repository: `.claude/skills/da.loop/**`, the target code/test rulebooks, `.claude/agents/da-*.md` and `.claude/hooks/path-guard.py`. Codex and Copilot adapters live in `.agents/skills/`, `.codex/agents/`, `.codex/hooks.json`, `.github/agents/` and `.github/hooks/`; renderer-owned outputs must be regenerated from canonical files and reviewed. Lessons are shared through git; run logs under `.claude/goal-runs/` stay local and ignored. Preserve existing target history and merge rows when branches diverge.
