---
name: da.jira.estimate.hh
description: "Estimate a Jira issue in person-hours, show scope and assumptions, and set Original Estimate only after explicit confirmation. Use for /da.jira.estimate.hh <issue> in the harness-mailbox dashboard repository."
argument-hint: "<issue-key|url> [--dry-run] [focus notes]"
---

LOOP: da.jira.estimate.hh@2026-11-16.1

# DA Jira Estimate (hours)

Prepare a defensible remaining-work estimate in whole hours. Include implementation, tests, review fixes and deployment/documentation work required by the issue. Exclude elapsed waiting time and unrelated work. The project Jira key, project-specific estimation history and board defaults are not established by repository evidence: use the provided issue and actual comparable resolved issues, or state that calibration data is unavailable.

## Safety rules

- Read the issue, all comments, parent/child issues, linked design/specification and relevant current repository evidence before estimating.
- Show the estimate, breakdown, assumptions, confidence and exact Jira field change. Do not write before the user explicitly confirms this exact value.
- On confirmation, update only Original Estimate and re-read the issue to verify the saved value. If the host has no write tool, provide the prepared estimate and stop.
- Never add worklogs, change status/assignee/priority, or edit other fields.
- Do not expose secrets, access tokens, private keys, customer data or environment data.

## Procedure

1. Resolve the issue from the argument; if absent, ask. Resolve the Jira site from connected Atlassian resources rather than assuming a site ID.
2. Read the issue and all comments, linked/parent work, acceptance criteria and design references. Review current code and project guidance when the issue concerns this repository. Record inaccessible evidence as an assumption.
3. Break remaining work into applicable items: specification/record-format design, backend implementation, frontend implementation, orchestrator-script work, tests, review fixes, deployment/configuration and handoff. Do not charge unrelated architecture decisions or already completed tasks.
4. Find comparable resolved issues only when their scope and completion evidence can be verified. If none are available, give an uncalibrated estimate and say so.
5. Present whole-hour subtotal by work item, uncertainty/risk allowance, rounded total and confidence. Separate dependencies/waiting time from effort.
6. Show the user the exact Original Estimate value (for example `8h`) and ask for explicit confirmation. `--dry-run` stops here.
7. After confirmation only, set the Original Estimate field, re-read the issue and report the stored value with the issue link returned by Jira.
