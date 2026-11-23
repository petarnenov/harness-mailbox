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

## Starting a team

After `make setup`, open a new terminal and run `team` from any directory. On Windows, run setup in the WSL 2 checkout and open the installed command in PowerShell, Command Prompt or that WSL distribution.

Select one or more projects, then set the worker count for each project. Choose a harness, model and reasoning effort for the orchestrator, then choose them separately for the workers. The worker configuration applies to every worker in this team. The menu reads the installed harness's model catalog. Press `R` on the model screen to refresh it or enter a model ID directly.

For a scripted launch, run the launcher below from the installed mailbox root. This example starts one Claude Code orchestrator and two Codex workers in the same registered project. Replace both occurrences of `app` with your project ID.

```sh
./copilot-mailbox/team --orchestrator-harness claude --worker-harness codex app app
```

Use `--orchestrator-model` and `--worker-model` to pass different model IDs, and `--orchestrator-effort` and `--worker-effort` for their reasoning levels. Omit model and effort flags to use the launcher defaults. New participants inherit the launching shell's provider configuration directories.

Enter the objective in the orchestrator window. For example, ask each selected worker to report its current branch and HEAD commit, then ask the orchestrator to verify the replies. Use the default tmux binding `Ctrl-b` then `w` to visit each participant for any first-run trust prompts. Detach with `Ctrl-b` then `d`. Open `team` again to reconnect, stop a worker or stop the whole team.

Across hosts, a published mailbox file carries the message. A running Copilot mailbox listener can notify its own session. For a recipient without an available notification tool, relay the file path and ask it to check its inbox. A correlated reply confirms receipt. See [Session messaging](#session-messaging) for the host-specific tools.

## Starting Copilot CLI

### Autonomous CLI sessions

The team menu starts the selected participants together. To launch Copilot roles in separate terminals or resume a specific Copilot session, use the participant launcher from the installed mailbox root.

```sh
./copilot-mailbox/start worker <project-id>
./copilot-mailbox/start orchestrator --project <project-id>
```

Each coordinator `--project` grants that registered directory to Copilot for verification and loads its trusted Copilot configuration. You can repeat the flag for additional projects when launching the roles separately.

Give the coordinator the objective once, in its terminal or with `--objective "…"` at launch. For a first check, ask it to register the worker, assign a read-only task through the project loop to report its branch and full HEAD SHA, then verify the returned evidence. You can leave both terminals open for subsequent tasks.

The launcher loads a local experimental Copilot CLI extension into each session. The worker extension publishes a non-executable registration offer carrying its own host ID. The coordinator sends an addressed registration request. Only a correlated response confirms the worker. The extension queues a notification in its own Copilot session when a mailbox message arrives. The worker validates gated execution requests and sends the evidence back. The coordinator wakes to verify the reply. The listener also receives eligible mailbox messages published by other hosts.

The launcher preserves the operator’s tool policy. Incoming messages trigger a turn even when native Autopilot is disabled. Use `--autopilot` to enable Copilot’s additional continuation turns. Review the CLI’s trust prompt on first use and grant the tools needed for the intended work using normal Copilot controls or repeatable `--allow-tool` rules. The launcher accepts `--model auto`, `--sync-session off` and `--resume <session-uuid>`. Resume in the same role and directory to receive pending work addressed to that ID. The listener runs for the lifetime of its Copilot process. Only one autonomous coordinator can own the orchestrator inbox at a time. Untagged legacy replies received during coordinator downtime require an inbox check when their original request names no coordinator session. A fresh worker session registers a new endpoint and requires a fresh dispatch for its own ID.

### Manual CLI sessions

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

A published request stays `delivery_unconfirmed` until the worker replies. In manual mode the operator relays the path and prompts the receiver to check its inbox.

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
| Wait for replies | `wait_threads` | replies arrive as new messages | launcher queues notifications for the running session; manual mode requires an inbox check |

- Codex thread tools are often exposed under `mcp__codex_tui`.
- Claude Code reaches other Claude Code sessions of the same user on the same machine. It cannot read another session's transcript, so confirm receipt through the mailbox reply or the session's direct answer.
- Copilot CLI sessions started with the mailbox launcher receive local SDK notifications. In manual CLI sessions, use file delivery. Background-agent tools operate within the current session. Publish the message, report `delivery_unconfirmed` and give the operator its exact path to relay. If a later Copilot version exposes session tools, verify them by name and description before relying on them.
- No host lists or messages sessions of a different host. A mixed fleet (for example a Claude Code orchestrator with Codex workers) is coordinated through mailbox files, with the opted-in Copilot listener handling its own inbox and the user relaying paths for receivers without a notification transport. A session being invisible to your host is a visibility limit, not evidence that it is absent.

The native Codex and Claude launchers install no inbox listener. While awaiting an event for an active objective, follow the bounded receive loop in the orchestrator skill unless a mechanism verified for this receiving session can wake it for that event. A same-host wait tool can be used during the turn, but a worker’s notification capability does not establish inbound wake for the coordinator. Registration alone finishes after inventory.
