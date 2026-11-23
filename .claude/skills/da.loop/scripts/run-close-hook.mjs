#!/usr/bin/env node
// da.loop hook: a run never ends before its closing steps — step 9 report and step 10 retro and ledger — are done.
//
// Hosts: Claude Code (skill frontmatter) and Codex (`.codex/hooks.json`) call it on Stop. Copilot has no Stop
// event; there pre-flight reports the same gaps through `closingGaps()` at the next start.
// - The run is the run log under `.claude/goal-runs/` modified last, and only when modified within
//   RECENT_MS, so a session that is not driving a run is never gated by an old log.
// - `status: running` ends no turn: continue the run, or set `waiting-user`, `escalated`, `stopped` or `done`.
// - `status: done|escalated|stopped` ends no turn until `closingGaps()` is empty; in a repository that tracks ADRs,
//   a run that used the code writer also needs a final `ADR-review: PASS` line.
// - `status: waiting-user` (a pending question or gate) passes.
// Blocks are capped at MAX_STOP_BLOCKS per run log, counted in a `<log>.closehook` side file, so a run that
// cannot be closed never traps the session. Node >= 18, no dependencies.
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { adrDirs } from './adr-gate-hook.mjs';

const MAX_STOP_BLOCKS = 3;
const RECENT_MS = 2 * 60 * 60 * 1000;
const FINAL_STATUSES = new Set(['done', 'escalated', 'stopped']);
const STATUS = /\bstatus: ([a-z-]+)/;
const RECURRED_LINE = /^recurred: (?:none|K\d+(?:,\s*K\d+)*)\s*$/gm;
const LESSONS_EVIDENCE = /\bLESSONS=(?:OK|FAIL)\b/;
const OPEN_TASK = /^- \[ \] T\d+\b/m;
const ADR_VERDICT = /^ADR-review: (PASS|BLOCK)\b/gm;
const CODE_WRITER = 'da-python-writer';

// Lines under `## <name>` up to the next `## ` heading; null when the heading is missing.
function sectionLines(text, name) {
    const lines = text.split('\n');
    const start = lines.findIndex((line) => line.trim() === `## ${name}`);
    if (start === -1) {
        return null;
    }
    const end = lines.findIndex((line, index) => index > start && line.startsWith('## '));
    return lines.slice(start + 1, end === -1 ? lines.length : end).filter((line) => line.trim() !== '');
}

const runsSection = (ledger) => (sectionLines(ledger, 'Runs') ?? []).join('\n');

/**
 * Whether the closing contract needs a final ADR review: the repository tracks ADRs and the run used a code writer.
 * @param {string} root checkout root
 * @param {string} text run log content
 */
export function requiresAdrReview(root, text) {
    return (sectionLines(text, 'Delegations') ?? []).join('\n').includes(CODE_WRITER) && adrDirs(root).length > 0;
}

const lastAdrVerdict = (text) => [...text.matchAll(ADR_VERDICT)].map((match) => match[1]).pop();

/**
 * The closing steps a finished run log still misses; empty when the run is closed or not finished.
 * @param {string} logName file name of the run log, as the `## Runs` row of `lessons.md` cites it
 * @param {string} text run log content
 * @param {string} ledger `lessons.md` content
 * @param {{adrRequired?: boolean}} [options] adrRequired: the run must end with `ADR-review: PASS`
 * @returns {string[]}
 */
export function closingGaps(logName, text, ledger, { adrRequired = false } = {}) {
    const header = text.split('\n', 1)[0];
    const status = (header.match(STATUS) || [])[1];
    if (!FINAL_STATUSES.has(status)) {
        return [];
    }
    const gaps = [];
    if (!/\bstep: 10\b/.test(header)) {
        gaps.push('header is not at `step: 10`');
    }
    if (!(sectionLines(text, 'Report') ?? []).length) {
        gaps.push('`## Report` is missing or empty (step 9)');
    }
    const retro = sectionLines(text, 'Retro') ?? [];
    if (!retro.some((line) => !line.startsWith('recurred:'))) {
        gaps.push('`## Retro` has no retro text (step 10.1)');
    }
    const markers = text.match(RECURRED_LINE) ?? [];
    if (markers.length !== 1) {
        gaps.push(`expected exactly one \`recurred: none|K..\` line, found ${markers.length} (step 10.1)`);
    }
    if (!runsSection(ledger).includes(logName)) {
        gaps.push('no row under `## Runs` in lessons.md (step 10.2)');
    }
    if (!LESSONS_EVIDENCE.test((sectionLines(text, 'Evidence') ?? []).join('\n'))) {
        gaps.push('no `LESSONS=OK|FAIL` line under `## Evidence` (step 10.5)');
    }
    if (adrRequired && lastAdrVerdict(text) !== 'PASS') {
        gaps.push('the last `ADR-review:` line is not `PASS` (code writer used in a repository with ADRs)');
    }
    if (status === 'done' && OPEN_TASK.test((sectionLines(text, 'Tasks') ?? []).join('\n'))) {
        gaps.push('`status: done` with an unticked task (step 9)');
    }
    return gaps;
}

/**
 * Why the turn must not end yet, or null when it may.
 * @param {string} logPath absolute run log path (used in the message)
 * @param {string} text run log content
 * @param {string} ledger `lessons.md` content
 * @param {{adrRequired?: boolean}} [options] passed to closingGaps
 * @returns {string|null}
 */
export function stopReason(logPath, text, ledger, options = {}) {
    const status = (text.split('\n', 1)[0].match(STATUS) || [])[1];
    if (status === 'running') {
        return `da.loop close hook: ${logPath} is still \`status: running\`. Continue the run; if it now waits for `
            + 'the user set `status: waiting-user`; if it is finished, blocked or stopped set `done`, `escalated` '
            + 'or `stopped` and complete steps 9 and 10 (report and retro) before ending the turn.';
    }
    const gaps = closingGaps(path.basename(logPath), text, ledger, options);
    if (!gaps.length) {
        return null;
    }
    return `da.loop close hook: ${logPath} is \`status: ${status}\` but its closing steps are incomplete: `
        + gaps.join('; ') + '. Finish steps 9 and 10 of SKILL.md now (an escalated or stopped run also gets its '
        + 'report and retro), then end the turn.';
}

function repoRoot(cwd) {
    const project = process.env.CLAUDE_PROJECT_DIR || cwd || process.cwd();
    const top = spawnSync('git', ['-C', project, 'rev-parse', '--show-toplevel'], { encoding: 'utf8' });
    return top.status === 0 ? top.stdout.trim() : project;
}

// The run log this session is most likely driving: the one modified last, if modified recently.
function currentRunLog(root, now) {
    const dir = path.join(root, '.claude/goal-runs');
    if (!fs.existsSync(dir)) {
        return null;
    }
    const newest = fs.readdirSync(dir)
        .filter((name) => name.endsWith('.md'))
        .map((name) => path.join(dir, name))
        .map((log) => ({ log, mtime: fs.statSync(log).mtimeMs }))
        .sort((a, b) => b.mtime - a.mtime)[0];
    return newest && now - newest.mtime <= RECENT_MS ? newest.log : null;
}

// True while this run log has blocks left; counts the block it grants.
function takeStopBlock(log) {
    const counter = log + '.closehook';
    const used = fs.existsSync(counter) ? Number(fs.readFileSync(counter, 'utf8')) || 0 : 0;
    if (used >= MAX_STOP_BLOCKS) {
        return false;
    }
    fs.writeFileSync(counter, String(used + 1));
    return true;
}

function main() {
    const raw = JSON.parse(fs.readFileSync(0, 'utf8') || '{}');
    if ((raw.hook_event_name ?? 'Stop') !== 'Stop') {
        return;
    }
    const root = repoRoot(raw.cwd);
    const log = currentRunLog(root, Date.now());
    if (!log) {
        return;
    }
    const ledgerFile = path.join(root, '.claude/skills/da.loop/lessons.md');
    const ledger = fs.existsSync(ledgerFile) ? fs.readFileSync(ledgerFile, 'utf8') : '';
    const text = fs.readFileSync(log, 'utf8');
    const reason = stopReason(log, text, ledger, { adrRequired: requiresAdrReview(root, text) });
    if (reason && takeStopBlock(log)) {
        console.log(JSON.stringify({ decision: 'block', reason }));
    }
}

if (process.argv[1] && fs.realpathSync(process.argv[1]) === fs.realpathSync(fileURLToPath(import.meta.url))) {
    main();
}
