#!/usr/bin/env node
// da.loop hook: every finished code-writer delegation is checked strictly against the project ADRs.
//
// Applies only when the repository tracks a directory named `adr` (any case, any depth).
// - `--event writer-done` (Stop hook in `.claude/agents/da-python-writer.md`, run as SubagentStop): marks the
//   active run log as waiting for an ADR review in a `<log>.adrpending` side file. It never blocks the writer.
// - PreToolUse (skill frontmatter, matcher `Agent|AskUserQuestion`): while the review is pending, denies every
//   agent launch except `da-python-reviewer` and every user question.
// - Stop (skill frontmatter): while the review is pending, blocks the end of the turn, at most MAX_STOP_BLOCKS
//   times per run log (`<log>.adrhook` counter).
// The review is pending until the run log holds more `ADR-review: PASS|BLOCK <report path>` lines than when the
// writer finished. Claude Code only: Codex and Copilot have no per-agent hook, so there the closing contract
// (run-close-hook.mjs and pre-flight) requires the final ADR review instead. Node >= 18, no dependencies.
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const MAX_STOP_BLOCKS = 3;
const ADR_REVIEW_LINE = /^ADR-review: (?:PASS|BLOCK)\b/gm;
const ACTIVE_STATUS = /status: (running|waiting-user)\b/;
const START = /\bstart: (\S+)/;
const REVIEWER = 'da-python-reviewer';
const GATED_TOOLS = new Set(['Agent', 'AskUserQuestion']);

/**
 * Directories named `adr` (any case) that hold tracked files, relative to the checkout root, sorted.
 * @param {string} root checkout root
 * @returns {string[]}
 */
export function adrDirs(root) {
    const listed = spawnSync('git', ['-C', root, 'ls-files', '-z', '--', ':(icase)adr/**', ':(icase)**/adr/**'],
        { encoding: 'utf8' });
    if (listed.status !== 0) {
        return [];
    }
    const dirs = listed.stdout.split('\0').filter(Boolean).map((file) => {
        const parts = file.split('/');
        return parts.slice(0, parts.findIndex((part) => part.toLowerCase() === 'adr') + 1).join('/');
    });
    return [...new Set(dirs)].sort();
}

/** Number of recorded ADR review verdict lines in a run log. */
export const adrReviewCount = (text) => (text.match(ADR_REVIEW_LINE) ?? []).length;

const pendingFile = (log) => log + '.adrpending';

/**
 * Records that a writer finished: the review is pending until a review line beyond the current count appears.
 * An unresolved earlier marker is kept, so a second writer cannot hide the first one's missing review.
 */
export function markWriterDone(log) {
    if (fs.existsSync(pendingFile(log))) {
        return;
    }
    const reviews = adrReviewCount(fs.readFileSync(log, 'utf8'));
    fs.writeFileSync(pendingFile(log), JSON.stringify({ reviews, at: new Date().toISOString() }));
}

/** True while the ADR review of a finished writer is missing; clears a resolved marker. */
export function reviewPending(log) {
    if (!fs.existsSync(pendingFile(log))) {
        return false;
    }
    const { reviews } = JSON.parse(fs.readFileSync(pendingFile(log), 'utf8'));
    if (adrReviewCount(fs.readFileSync(log, 'utf8')) > reviews) {
        fs.rmSync(pendingFile(log));
        return false;
    }
    return true;
}

/** Whether a PreToolUse call must wait for the ADR review: only the reviewer may be launched meanwhile. */
export function isGatedCall(toolName, toolInput) {
    return GATED_TOOLS.has(toolName) && !(toolName === 'Agent' && toolInput?.subagent_type === REVIEWER);
}

export function instruction(log, dirs) {
    return 'da.loop ADR gate: a code writer finished and its changes have not been checked against the ADRs in '
        + dirs.map((dir) => '`' + dir + '`').join(', ') + '. Save `git diff <BASE_SHA> -- <writer files>` to the '
        + 'scratchpad, brief `da-python-reviewer` with `mode: adr`, the ADR directories and the diff path, then '
        + 'record `ADR-review: PASS|BLOCK <report path>` under ## Review in ' + log + '. On BLOCK route the findings '
        + 'to the writer and review again. No other agent, question or turn end until that line exists.';
}

function repoRoot(cwd) {
    const project = process.env.CLAUDE_PROJECT_DIR || cwd || process.cwd();
    const top = spawnSync('git', ['-C', project, 'rev-parse', '--show-toplevel'], { encoding: 'utf8' });
    return top.status === 0 ? top.stdout.trim() : project;
}

// The run in progress: among running/waiting-user logs, the one with the newest `start:`.
function activeRunLog(root) {
    const dir = path.join(root, '.claude/goal-runs');
    if (!fs.existsSync(dir)) {
        return null;
    }
    const active = fs.readdirSync(dir)
        .filter((name) => name.endsWith('.md'))
        .map((name) => path.join(dir, name))
        .map((log) => ({ log, header: fs.readFileSync(log, 'utf8').split('\n', 1)[0] }))
        .filter(({ header }) => ACTIVE_STATUS.test(header))
        .map((run) => ({ ...run, start: (run.header.match(START) || [])[1] ?? '' }))
        .sort((a, b) => b.start.localeCompare(a.start));
    return active[0]?.log ?? null;
}

// True while this run log has Stop blocks left; counts the block it grants.
function takeStopBlock(log) {
    const counter = log + '.adrhook';
    const used = fs.existsSync(counter) ? Number(fs.readFileSync(counter, 'utf8')) || 0 : 0;
    if (used >= MAX_STOP_BLOCKS) {
        return false;
    }
    fs.writeFileSync(counter, String(used + 1));
    return true;
}

function argValue(flag) {
    const index = process.argv.indexOf(flag);
    return index === -1 ? undefined : process.argv[index + 1];
}

function main() {
    const raw = JSON.parse(fs.readFileSync(0, 'utf8') || '{}');
    const event = argValue('--event') ?? raw.hook_event_name;
    const root = repoRoot(raw.cwd);
    const dirs = adrDirs(root);
    const log = dirs.length ? activeRunLog(root) : null;
    if (!log) {
        return;
    }
    if (event === 'writer-done') {
        markWriterDone(log);
    } else if (event === 'PreToolUse' && isGatedCall(raw.tool_name, raw.tool_input) && reviewPending(log)) {
        console.log(JSON.stringify({ hookSpecificOutput: { hookEventName: 'PreToolUse',
            permissionDecision: 'deny', permissionDecisionReason: instruction(log, dirs) } }));
    } else if (event === 'Stop' && reviewPending(log) && takeStopBlock(log)) {
        console.log(JSON.stringify({ decision: 'block', reason: instruction(log, dirs) }));
    }
}

if (process.argv[1] && fs.realpathSync(process.argv[1]) === fs.realpathSync(fileURLToPath(import.meta.url))) {
    main();
}
