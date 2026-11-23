---
name: da.jira.retro
description: "Draft evidence-based retrospective notes from a user-specified Jira cycle and Confluence board; read-only. Use for /da.jira.retro or a pasted retro-board request in the harness-mailbox dashboard repository."
argument-hint: "<cycle dates> <whiteboard title|url> <Jira board id|url> [focus]"
---

LOOP: da.jira.retro@2026-11-16.1

# DA Jira Retro

Create one or two concise English sticky notes for each requested retrospective section. This skill is read-only. The project-specific Jira project key, board, sprint mapping and Confluence whiteboard are not established in repository guidance; require the user to provide them or discover them from connected Atlassian data. Never infer a board or cycle from unrelated tickets.

## Hosts

| | Claude Code | Codex CLI | Copilot CLI |
|---|---|---|---|
| Invoke | `/da.jira.retro <cycle> <whiteboard> <board>` | `$da.jira.retro <cycle> <whiteboard> <board>` | `/da.jira.retro <cycle> <whiteboard> <board>` |
| Data access | Use available Atlassian connector operations; discover unsupported operations before calling | Use configured Atlassian MCP operations | If unavailable, request whiteboard text/SVG and a Jira CSV export |

## Rules

- **Read-only:** do not write to Jira, Confluence, Slack, or publish artifacts.
- **Evidence-backed:** each note cites the relevant issue keys and the whiteboard section. Separate facts from interpretation.
- **English notes**, professional and concise. Do not include credentials, tokens, customer PII or real data records.
- Do not present project completion based only on scaffolding, health checks or successful builds; follow current project status in `AGENTS.md`, `docs/05-delivery/sdlc.md` and the active project evidence.
- If the board, cycle dates, or ticket scope is unclear, ask for the missing input rather than guessing.

## Procedure

1. Resolve the requested Jira site from connected Atlassian resources. Read the specified Confluence whiteboard and confirm the cycle dates and section headings.
2. Read the specified Jira board/filter and its sprint dates. Query issues for the requested cycle, paginate fully, and open relevant issues and comments for context.
3. Group only evidence that falls within the stated project scope. Distinguish dashboard backend, frontend, orchestrator-script and container work when useful; do not assume undocumented board labels.
4. Draft one or two notes per requested section. Each note should capture an actionable observation, supporting examples and a practical next step. Include issue keys as evidence.
5. Present the notes grouped by the whiteboard's sections, with source references and any missing-data limitations. Do not edit the board.
