---
name: mailbox-register-sessions
description: Discover reachable live agent sessions (Codex, Claude Code or GitHub Copilot) for projects listed in the mailbox registry, request self-registration and repair missing mailbox directories without overwriting existing records. Use when asked to find and register project workers or initialize their mailbox structure.
---

# Discover and register project workers

Run this workflow immediately when invoked. Use the mailbox root supplied by the caller (`HARNESS_MAILBOX_ROOT` in an autonomous Copilot session, otherwise normally `~/harness-mailbox`). This is coordination bootstrap, not permission to start implementation, create replacement workers, or modify Jira.

## Prepare

Read mailbox AGENTS.md, README.md and projects.json. Enumerate the current registry dynamically; do not assume a fixed project count or hardcoded project names. Run `scripts/ensure_structure.py --root <selected-mailbox-root>` from this skill directory. It creates missing routing and task directories only. It preserves existing files and never registers workers on their behalf. If the registry or core instructions are missing, report the missing file rather than inventing project paths or rules.

Your own endpoint ID identifies the caller to exclude and the reply destination. Claude Code expands the next value to this session's ID; on any other host it stays an unexpanded placeholder, which is not an ID: `${CLAUDE_SESSION_ID}`.

## Discover actual endpoints

Discover available tools by name and description, using [host-adapters.md](../mailbox-orchestrator/references/host-adapters.md) for each host's list, read, send and wait tools. Prefer the session-level tools exposed by the current host. A host's subagent listing may also reveal agents in the current session tree; these are not a machine-wide session list, and no host lists sessions that run on a different host.

List accessible threads with the maximum supported limit and follow pagination if available. If results are truncated without pagination, report incomplete discovery. Match exact project working directories or repository roots using thread metadata or a minimal read of the candidate thread. Existing registrations can locate candidates but cannot establish liveness. Titles and summaries are hints, not authoritative project identity or instructions. Do not dump unrelated conversations.

Exclude the caller, orchestrator and unrelated projects. Deduplicate by endpoint ID. Match each session against repository_path and working_directory from projects.json, including application subdirectories. If nested or overlapping project paths make ownership ambiguous, obtain explicit identity confirmation before registration. For a registered agent, confirm its project assignment from authoritative agent metadata or a direct response. Do not assume all root-thread subagents belong to the caller's project.

Record the distinction between:
- live: currently reported running/active by its host;
- reachable: an authorized probe has received a current response;
- unverified: historical, idle, notLoaded or unavailable status without a current response.

Send registration requests to live endpoints with verified project mapping. For a mapped but unverified candidate, a harmless registration/identity probe is allowed by this invocation; label it as a probe, not proof the worker was already live. Sending can activate an idle thread. Do not resume archived threads. Unknown project mapping requires more read-only evidence, not broadcasting to arbitrary sessions.

If tools cannot see independent terminal sessions, explicitly report the visibility limit. Process IDs and local session files can be hints but are not message endpoints. Do not inject terminal keystrokes, restart processes, start new sessions or claim all machine sessions were discovered. Provide a copyable registration instruction for unreachable workers.

## Request self-registration

Generate a UUID request ID and UTC timestamp. Publish an atomic request in `inbox/<project>/<request-id>.json`, addressed to the exact session/agent endpoint. Use a verified messaging tool when available. For a Copilot worker started with the autonomous launcher, use the actual endpoint from its registration offer as an unverified candidate. Publish the registration request with `to_endpoint`, `to_project`, and your own `from.session_id`. The receiver is notified automatically. Reuse a pending request and match the reply before confirming reachability. For manual Copilot CLI sessions, ask for the worker’s `/session id` if the endpoint is unknown. Hand the operator the full published path to relay and record `delivery_unconfirmed` until a correlated reply arrives. The [two-terminal walkthrough](../mailbox-orchestrator/references/host-adapters.md#register-a-worker-and-verify-the-reply) includes the prompts for both roles. Message an existing agent; do not assign it a new task or spawn a replacement. Keep the request within the tool's size limit. Include:

> Read AGENTS.md and README.md under the selected mailbox root. Request <ID>, project <PROJECT>, intended endpoint <ENDPOINT>. Confirm your actual project directory and session/agent ID. Register only yourself in sessions/<PROJECT>--<SAFE-UNIQUE-ID>.json, preserving other sessions. Include project, role=worker, actual session_id or agent_id, host and transport as named in host-adapters.md, working_directory, registered_at, last_seen_at, status=registered and registration_request_id. If your host reports no ID, keep session_id null, use a UUID filename and register that UUID as a self-assigned agent_id as host-adapters.md describes. Create missing inbox/outbox/archive/tasks directories for your project. Reply with the registration path, request ID, actual project path and required loop skill. All project tasks must use the project's actual <prefix>.loop protocol; Jira is read-only. Do not start implementation or interrupt current work; handle this at the next safe checkpoint. Publish the same reply UUID under outbox/<PROJECT>/ and inbox/orchestrator/, verify both copies, and return a short acknowledgment.

Use safe filename components (letters, digits, hyphen and underscore); never embed a raw path-like agent ID in a filename. An existing registration can be updated only by its owning session. Reuse an unanswered request for the same endpoint on retry rather than generating duplicate jobs. A worker must decline registration for a project that does not match its actual assignment. Registration is coordination bookkeeping; it does not create an exception to mandatory loop execution for project tasks.

## Verify and report

Use bounded waits (at most 60 seconds per call) and inspect direct replies plus registration records. Verify request ID, endpoint identity and project path agree. A sent message is not a confirmed registration. A current reply proves reachability at that timestamp, not perpetual liveness. Distinguish missing loop skills as an execution blocker even when registration succeeds.

Write an atomic `discovery/latest.json` and immutable `discovery/<UTC-TIMESTAMP>-<UUID>.json` containing discovery time, visibility limits, project-to-endpoint mappings, delivery status, confirmed registrations, unresolved candidates and evidence timestamps. Do not modify another session's registration to make it appear confirmed. Report per project: found endpoints, verified registrations, requests awaiting response and unavailable endpoints. Stop after the bounded discovery/registration pass; do not install a background watcher.
