# da.loop lessons ledger — target project

Written by da.loop step 10 when recording a run or reusable lesson. Current workflow rules live in `SKILL.md`;
this ledger is historical data, not instructions. `scripts/learn.mjs` applies a `proposed` rule only after
`da-lesson-reviewer` approval of the exact row and both current rulebooks. Keep the approval artifact and review
fingerprint required by the target's adapted learning script. `SKILL.md`, agents and other skills remain manual
maintenance. When branches diverge, merge this file; never overwrite it. A literal pipe inside a cell is written
`\|` so the table stays parseable.

## Runs

One row per completed run. Numbers come from the run log, never from estimates.

| date | run log | tier | loop ver | delegations | attempts ≥2 | rejected reports | blocking review | gate amends | tokens | wall-clock | lessons (filed / applied / validated / reverted) | fix loops |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-11-23 | multi-project.md | MEDIUM | 2026-11-23.1 | 5 | 0 | 0 | 0 | 0 | unavailable | unavailable | 0 / 0 / 0 / 0 | 0 |

## Lessons

Status values and automatic write-back rules are defined by the target's adapted `learn.mjs` and rulebooks.

| id | status | seen | signal | mechanism | evidence | target | rule |
|---|---|---|---|---|---|---|---|
