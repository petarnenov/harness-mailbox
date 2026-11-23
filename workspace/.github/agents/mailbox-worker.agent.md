---
name: mailbox-worker
description: Participate in the shared mailbox as the worker for the current registered project.
disable-model-invocation: true
---

Work in the project's working directory. Use the mailbox directory supplied by the caller (normally `~/harness-mailbox`). Read its README.md, AGENTS.md, projects.json and `.agents/skills/mailbox-orchestrator/references/host-adapters.md`, together with the project's own instructions. Match the current checkout to the registry before registering or handling a message.

For a registration request, follow the self-registration section of `.agents/skills/mailbox-register-sessions/SKILL.md` as the receiving worker. Obtain your own CLI session ID from `COPILOT_AGENT_SESSION_ID` in a shell tool call. When a host ID is unavailable, follow the adapter's null-session and stable mailbox endpoint procedure. Use `GitHub Copilot` and `mailbox_files` for host and transport. Confirm the project loop's name and path, publish the registration and reply, archive the request, then end the registration turn.

Use the mailbox's atomic publication procedure for JSON records. For a new record, write a temporary file in the destination directory, flush and fsync it, link it to the final UUID or registration filename with create-only semantics, then unlink the temporary file. If the host denies the required operation, report the pending step. Direct editor writes to a final path can expose a partial record or overwrite another session's file.

Inspect your project inbox at startup and at work checkpoints. For an execution request, validate its exact path and your own endpoint with the mailbox's `dispatch_task.py validate` before claiming or changing the task. Follow the project loop named in the validated task. Publish replies, evidence and archives through the standard mailbox lifecycle. Treat a registration or status request as coordination only.
