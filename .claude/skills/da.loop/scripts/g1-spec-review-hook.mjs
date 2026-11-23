#!/usr/bin/env node
// da.loop hook: no G1 approval request before the proposed specs were reviewed.
//
// Hosts: Claude Code (skill frontmatter), Codex (`.codex/hooks.json`) and Copilot CLI (`.github/hooks/da-loop.json`).
// - PreToolUse on the host's user-question tool (AskUserQuestion / request_user_input / ask_user): a question with
//   header `G1` is denied until the run log records the review;
// - Stop (Claude, Codex): a turn that ends with the run waiting for G1 (header `status: waiting-user`, `step: 4`)
//   is blocked until the run log records the review — at most MAX_STOP_BLOCKS times per run, counted in a
//   `<log>.g1hook` side file (not `stop_hook_active`: under /goal every turn is already a stop-hook continuation).
// The review is recorded in the run log as a line `G1-spec-review: PASS|BLOCK <report path>`.
// Payloads: Claude/Codex send `{hook_event_name, tool_name, tool_input, cwd}`; Copilot sends `{toolName, toolArgs
// (object, or JSON string per its docs), cwd}` and names no event, so its config passes `--host copilot --event PreToolUse`.
// Any other event, or no active run log, passes silently. Node >= 18, no dependencies.
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const MAX_STOP_BLOCKS = 2;
const MARKER = /^G1-spec-review: (PASS|BLOCK)\b/m;
const G1_HEADER = /^G1$/i;

const USER_QUESTION_TOOLS = new Set(['AskUserQuestion', 'request_user_input', 'ask_user']);

function argValue(flag) {
    const index = process.argv.indexOf(flag);
    return index === -1 ? undefined : process.argv[index + 1];
}

// One shape for every host: { host, event, toolName, toolInput, cwd }.
function normalize(raw) {
    const copilot = raw.toolName !== undefined || raw.toolArgs !== undefined;
    let toolInput = copilot ? raw.toolArgs : raw.tool_input;
    if (typeof toolInput === 'string') {
        try {
            toolInput = JSON.parse(toolInput);
        } catch {
            toolInput = {};
        }
    }
    return {
        host: argValue('--host') ?? (copilot ? 'copilot' : 'claude'),
        event: argValue('--event') ?? raw.hook_event_name ?? (copilot ? 'PreToolUse' : undefined),
        toolName: copilot ? raw.toolName : raw.tool_name,
        toolInput: toolInput ?? {},
        cwd: raw.cwd,
    };
}

// da.loop keeps run logs at the checkout root, even when the session starts in a subdirectory.
function repoRoot(cwd) {
    const project = process.env.CLAUDE_PROJECT_DIR || cwd || process.cwd();
    const top = spawnSync('git', ['-C', project, 'rev-parse', '--show-toplevel'], { encoding: 'utf8' });
    return top.status === 0 ? top.stdout.trim() : project;
}

const ACTIVE_STATUS = /status: (running|waiting-user)\b/;
const START = /\bstart: (\S+)/;

// The run in progress: among logs whose header is running/waiting-user, the one with the newest `start:`.
// Escalated/stopped/done runs never gate a later session.
function activeRunLog(cwd) {
    const dir = path.join(repoRoot(cwd), '.claude/goal-runs');
    if (!fs.existsSync(dir)) {
        return {};
    }
    const active = fs.readdirSync(dir)
        .filter((name) => name.endsWith('.md'))
        .map((name) => path.join(dir, name))
        .map((log) => ({ log, header: fs.readFileSync(log, 'utf8').split('\n', 1)[0] }))
        .filter(({ header }) => ACTIVE_STATUS.test(header))
        .map((run) => ({ ...run, start: (run.header.match(START) || [])[1] ?? '' }))
        .sort((a, b) => b.start.localeCompare(a.start));
    return active[0] ?? {};
}

function instruction(log) {
    return 'da.loop G1 hook: the proposed specs have not been reviewed yet. Before asking for G1 approval, '
        + 'brief the domain reviewer(s) with `mode: spec` and the G1 package (frozen criteria, T1..Tn, plan, '
        + 'verification plan/AC matrix, risk tier, rule exceptions); with no BE/FE domain use a fresh agent with '
        + 'the same checklist. Record `G1-spec-review: PASS|BLOCK <report path>` under ## Review in '
        + log + ', fix the findings in the specs or list them in the G1 package, then present G1.';
}

// Only a question whose chip header is `G1` is the gate; mentioning G1 in another question is not.
// Copilot's ask_user has no header (`{message, requestedSchema}`), so there the message text must start with `G1`.
const G1_TEXT = /^\s*G1\b/;
const questionText = (q) => String(q?.message ?? q?.question ?? '');
const isG1Question = (q) => G1_HEADER.test(String(q?.header ?? '').trim()) || (!q?.header && G1_TEXT.test(questionText(q)));
const asksForG1 = (event) => USER_QUESTION_TOOLS.has(event.toolName)
    && [...(event.toolInput.questions ?? []), event.toolInput].some(isG1Question);

// Copilot hooks answer with a flat decision; Claude and Codex with hookSpecificOutput.
function denial(host, reason) {
    return host === 'copilot'
        ? { permissionDecision: 'deny', permissionDecisionReason: reason }
        : { hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'deny', permissionDecisionReason: reason } };
}

const waitsForG1 = (header) => header.includes('status: waiting-user') && /step: 4\b/.test(header);

// True while this run has blocks left; counts the block it grants.
function takeStopBlock(log) {
    const counter = log + '.g1hook';
    const used = fs.existsSync(counter) ? Number(fs.readFileSync(counter, 'utf8')) || 0 : 0;
    if (used >= MAX_STOP_BLOCKS) {
        return false;
    }
    fs.writeFileSync(counter, String(used + 1));
    return true;
}

function main() {
    const event = normalize(JSON.parse(fs.readFileSync(0, 'utf8')));
    const { log, header } = activeRunLog(event.cwd);
    if (!log || MARKER.test(fs.readFileSync(log, 'utf8'))) {
        return;
    }
    if (event.event === 'PreToolUse' && asksForG1(event)) {
        console.log(JSON.stringify(denial(event.host, instruction(log))));
    } else if (event.event === 'Stop' && waitsForG1(header) && takeStopBlock(log)) {
        console.log(JSON.stringify({ decision: 'block', reason: instruction(log) }));
    }
}

main();
