# Agent host adapters

The mailbox protocol is host-independent: JSON files, the dependency gate (`python3`) and Git are all it needs. This reference maps the host-specific parts for Codex, Claude Code and GitHub Copilot. Use only tools the current host actually exposes; when a capability below is absent, fall back to mailbox files and report `delivery_unconfirmed` instead of improvising another channel.

In the skills, "thread" means one host session or conversation, whatever the host calls it.

## Workspace entry points

| Concern | Codex | Claude Code | GitHub Copilot (CLI and VS Code) |
| --- | --- | --- | --- |
| Instructions | `AGENTS.md` | `CLAUDE.md`, which imports `AGENTS.md` | `AGENTS.md` |
| Skills | `.agents/skills/` | `.claude/skills/<skill>` links | `.github/skills/<skill>` links, `.agents/skills/` |
| Invoke a skill | `$<skill>` | `/<skill>` | `/<skill>` |

`AGENTS.md` and `.agents/skills/` are the single source. `.claude/skills/<skill>` and `.github/skills/<skill>` are per-skill symlinks to `.agents/skills/<skill>`; never copy a skill into them. When adding a skill, add both links. Skill directory names equal the frontmatter `name` (lowercase letters, digits, hyphens; at most 64 characters) and descriptions stay under 1024 characters, the strictest limits among the three hosts.

## Starting Copilot CLI

Start the coordinator in the mailbox directory. Start each worker in its registered project directory and add the mailbox so Copilot can load its agent profile and skills.

```sh
copilot -C ~/harness-mailbox --agent mailbox-orchestrator
copilot -C "/path/from/projects.json" --add-dir ~/harness-mailbox --agent mailbox-worker
```

Both commands use the configured Copilot model. If that model is unavailable, add `--model auto` for this session. Replace the worker path with the project's exact `working_directory`. Supply the mailbox path to the worker if it differs from `~/harness-mailbox`. Inside a coordinator session, `/mailbox-register-sessions` starts discovery. A worker handles its own registration and validates a delivered message using its registered endpoint.

The profiles live in `.github/agents/` in the mailbox root (distributed under `workspace/.github/agents/` in the dashboard checkout). They load the shared instructions and skills. CLI flags such as `--agent` and `--add-dir` are documented in the [Copilot CLI command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference).

### Register a worker and verify the reply

Use the worker and coordinator terminals opened above. Replace `<project>` with the project ID from `projects.json`. Paste the following prompts in the named terminals.

1. **Worker** — enter `/session id` and copy the ID. A session nickname can help you find the terminal, but the registration request needs the ID reported by Copilot.
2. **Coordinator** — enter `/mailbox-register-sessions Request self-registration for <project> at Copilot session ID <session-id>. Give me the full path of the request to relay.` Reuse a pending request addressed to this exact endpoint. If an earlier request used a nickname or another ID, request a fresh one addressed to the real ID.
3. **Worker** — enter `Process the registration request at <request-path>. Verify the recipient against your own CLI session ID and your directory against projects.json. Register this session and publish the reply to the orchestrator.`
4. **Coordinator** — enter `Check inbox/orchestrator for the reply to <request-id>. Show the matching project, working directory, session ID and loop skill, with the registration and reply paths.` The request ID is the UUID in the request filename. Confirm that the reply's `in_reply_to` and the registration's `registration_request_id` both match it.

A published request stays `delivery_unconfirmed` until the worker replies. Each independent CLI session needs the operator to relay the path and prompt it to check its inbox.

### Verify task delivery with a read-only task

1. **Coordinator** — enter `/mailbox-orchestrator Assign the registered worker for <project> a read-only task to report its current Git branch and full HEAD SHA. Use the project loop and dispatch_task.py dispatch. Give me the full path of the published execution message.`
2. **Worker** — enter `Process the gated message at <message-path> using your registered endpoint and the project loop. Publish the result and evidence in your mailbox reply.` The worker runs `dispatch_task.py validate` before claiming the task.
3. **Coordinator** — enter `Verify the worker's result. Show the dispatch and successful validation receipts for the same message, task and endpoint, the correlated reply, and the branch and full SHA. Compare them with the worker checkout and report any discrepancy.`

The receipts are under `dispatch-gates/<task-id>/`. The worker publishes the same reply ID in its outbox and the coordinator inbox and archives the processed request. See the [dependency gate reference](dependency-gate.md) for the exact commands and receipt fields.

## Session identity

Register the ID the host itself reports as `session_id`; never derive one from a title, timestamp or file name. If the host gives none, set `session_id` to null as README.md prescribes.

The dependency gate delivers only to a non-null endpoint. A worker whose host reports no ID therefore generates one UUID, registers it as `agent_id` with `agent_id_source: self_assigned`, and reuses that same value for every later `validate`. This is a mailbox endpoint, not a claim about the host session: keep `session_id` null and never present the UUID as a host ID.

| Host | Where the session learns its own ID | `host` | `transport` |
| --- | --- | --- | --- |
| Codex | Its thread ID as reported by the host's thread tools | `Codex` | `codex_tui` |
| Claude Code | The expanded session ID line in either mailbox skill body; otherwise not exposed | `Claude Code` | `claude_sessions` |
| Copilot CLI | `/session id`, or `COPILOT_AGENT_SESSION_ID` in its shell commands | `GitHub Copilot` | `mailbox_files` |
| Copilot in VS Code | Not exposed: null `session_id` plus a self-assigned `agent_id` | `GitHub Copilot` | `mailbox_files` |

The dependency gate matches a worker by `session_id` or `agent_id` plus its project directory, so these values work the same for every host.

## Session messaging

| Operation | Codex | Claude Code | GitHub Copilot |
| --- | --- | --- | --- |
| List sessions | `list_threads` | `ListAgents` | not available |
| Send a message | `send_message_to_thread` | `SendMessage` | not available |
| Read a session | `read_thread` | not available | not available |
| Wait for replies | `wait_threads` | no blocking wait; replies arrive as new messages | not available |

- Codex thread tools are often exposed under `mcp__codex_tui`.
- Claude Code reaches other Claude Code sessions of the same user on the same machine. It cannot read another session's transcript, so confirm receipt through the mailbox reply or the session's direct answer.
- Independent Copilot CLI sessions use file delivery in this harness, verified with CLI 1.0.114. Background-agent tools operate within the current session. Publish the message, report `delivery_unconfirmed` and give the operator its exact path to relay. If a later Copilot version exposes session tools, verify them by name and description before relying on them.
- No host lists or messages sessions of a different host. A mixed fleet (for example a Claude Code orchestrator with Codex workers) is coordinated through mailbox files, with the user relaying the message path where no shared transport exists. A session being invisible to your host is a visibility limit, not evidence that it is absent.
