---
name: mailbox-orchestrator
description: Coordinate registered project workers from the shared mailbox using its gated delivery protocol.
disable-model-invocation: true
---

Use the mailbox root as the working directory. Read README.md, AGENTS.md and the canonical `.agents/skills/mailbox-orchestrator/SKILL.md` and follow them for the user's requested operation. Use the separate orchestrator routing identity. For session discovery follow `.agents/skills/mailbox-register-sessions/SKILL.md`.

Use the host adapter's file transport for independent workers. A registration or a published inbox file alone does not establish reachability. Dispatch execution assignments and resumes through `dispatch_task.py` and relay the exact published message path. Record delivery as `delivery_unconfirmed` until a current worker acknowledgment arrives. Apply the workspace's Jira, AIO and project-loop rules.

When launched with the mailbox listener, your current directory is the mailbox root. Use relative paths from here or read `HARNESS_MAILBOX_ROOT` directly in the shell. Treat a `registration_offer` from a registered project directory as a candidate for a harmless probe. Verify the mapping in projects.json, issue or reuse its non-executable `registration_request` with `to_project`, `to_endpoint` and `from.session_id` (read your own `COPILOT_AGENT_SESSION_ID` directly in the shell that publishes the request), and archive the handled offer. On the registration response, match the original request, session, directory and loop. Continue the user's authorized objective on confirmation. End the turn awaiting a reply only after draining pending requests and confirming the listener can wake this coordinator for that reply. Otherwise follow the active receive loop in the canonical skill. When the worker reports, correlate the reply, verify gate receipts and evidence, archive the handled incoming record, and report the verified result to the user. Never acknowledge a reply with another reply solely to signal receipt.
