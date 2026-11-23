---
name: mailbox-orchestrator
description: Coordinate registered project workers from the shared mailbox using its gated delivery protocol.
disable-model-invocation: true
---

Use the mailbox root as the working directory. Read README.md, AGENTS.md and the canonical `.agents/skills/mailbox-orchestrator/SKILL.md` and follow them for the user's requested operation. Use the separate orchestrator routing identity. For session discovery follow `.agents/skills/mailbox-register-sessions/SKILL.md`.

Use the host adapter's file transport for independent workers. A registration or a published inbox file alone does not establish reachability. Dispatch execution assignments and resumes through `dispatch_task.py` and relay the exact published message path. Record delivery as `delivery_unconfirmed` until a current worker acknowledgment arrives. Apply the workspace's Jira, AIO and project-loop rules.
