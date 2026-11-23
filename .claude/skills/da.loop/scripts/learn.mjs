#!/usr/bin/env node
// da.loop learning loop: apply filed lessons to the rulebooks, validate them, audit the ledger.
//
// Deterministic on purpose. It never invents or weakens rules; retirement removes only an exact lesson line, and
// never touches SKILL.md, the agents or the other skills — those stay manual, user-authorized maintenance.
// Every addition requires semantic approval bound to the exact row and both current rulebooks.
//
// Usage (from anywhere inside the checkout):
//   node learn.mjs check                      ledger/book/run-log invariants; first line LESSONS=OK|FAIL
//   node learn.mjs review-key --id K30        fingerprint for the reviewer (not approval itself)
//   node learn.mjs apply [--id K30]… [--reviewed <fingerprint>] [--dry-run]
//   node learn.mjs validate [--dry-run]
//   node learn.mjs status
// Node >= 18, no dependencies.
import fs from 'node:fs';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { parseArgs } from 'node:util';

const LEDGER = '.claude/skills/da.loop/lessons.md';
const BOOKS = {
    conventions: {
        file: '.claude/skills/da-python-conventions/SKILL.md',
        section: 'K',
        name: 'da-python',
        label: 'code book',
    },
    tests: {
        file: '.claude/skills/da-python-test-conventions/SKILL.md',
        section: 'T',
        name: 'da-python-tests',
        label: 'test book',
    },
};
const RUN_DIRS = ['.claude/goal-runs', '.codex/goal-runs'];
const CITATION_FILES = [
    '.claude/skills/da.loop/SKILL.md',
    ...Object.values(BOOKS).map((book) => book.file),
    ...['da-python-writer', 'da-python-test-writer', 'da-python-reviewer', 'da-lesson-reviewer']
        .map((agent) => `.claude/agents/${agent}.md`),
];
const STATUSES = new Set(['seeded', 'proposed', 'applied', 'validated', 'reverted', 'rejected', 'stale', 'blocked']);
const VALIDATE_AFTER_RUNS = 3;
const RULE_MIN = 20;
const RULE_MAX = 280;
const TARGET = /^(conventions|tests)\s+§([KT]\d+)(?:\.(\d+))?\s*\((P[123])\)$/;
const SECRET = /(BEGIN [A-Z ]*PRIVATE KEY|password\s*[=:]|secret\s*[=:]|\btoken\s*[=:]|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,})/i;
const INSTRUCTION = /\b(ignore|skip|bypass|disable|always approve|never verify|no need to)\b/i;
const CITATION = /\((K\d{2}(?:,\s*K\d{2})*)\)/g;

function repoRoot() {
    const top = spawnSync('git', ['rev-parse', '--show-toplevel'], { encoding: 'utf8' });
    if (top.status !== 0) {
        throw new Error('run inside the repository');
    }
    return top.stdout.trim();
}

const today = () => new Date().toISOString().slice(0, 10);
const read = (file) => fs.readFileSync(file, 'utf8');
const write = (file, text) => fs.writeFileSync(file, text);

function parseStatus(cell) {
    const match = cell.trim().match(/^([a-z]+)(?:@(\d{4}-\d{2}-\d{2}))?/);
    return match ? { code: match[1], date: match[2] ?? null, raw: cell.trim() } : { code: 'unknown', date: null, raw: cell.trim() };
}

function parseTarget(cell) {
    const match = cell.trim().match(TARGET);
    return match
        ? { book: match[1], anchor: match[2], minor: match[3] ?? null, severity: match[4] }
        : null;
}

// Ledger cells are markdown table cells: a literal pipe is written `\|`, so split on unescaped pipes only.
function splitRow(line) {
    const cells = [];
    let current = '';
    for (let i = 0; i < line.length; i += 1) {
        if (line[i] === '\\' && line[i + 1] === '|') {
            current += '|';
            i += 1;
        } else if (line[i] === '|') {
            cells.push(current.trim());
            current = '';
        } else {
            current += line[i];
        }
    }
    cells.push(current.trim());
    return cells;
}

function rebuildRow(cells) {
    return cells
        .map((cell, index) => (index === 0 || index === cells.length - 1 ? '' : ` ${cell.replace(/\|/g, '\\|')} `))
        .join('|');
}

// Cells: '', id, status, seen, signal, mechanism, evidence, target, rule, ''.
function parseRow(line) {
    const cells = splitRow(line);
    if (cells.length !== 10) {
        return null;
    }
    const [, id, status, seen, signal, mechanism, evidence, target, rule] = cells;
    return {
        line,
        id,
        status: parseStatus(status),
        seen,
        signal,
        mechanism,
        evidence,
        target,
        rule,
        targetRef: parseTarget(target),
    };
}

function parseLedger(text) {
    const lines = text.split('\n');
    const rows = [];
    const malformed = [];
    let inLessons = false;
    for (const line of lines) {
        if (/^## /.test(line)) {
            inLessons = /^## Lessons\b/.test(line);
            continue;
        }
        if (!inLessons || !/^\|\s*K\d{2}\s*\|/.test(line)) {
            continue;
        }
        const row = parseRow(line);
        if (row) {
            rows.push(row);
        } else {
            malformed.push(line);
        }
    }
    return { lines, rows, malformed };
}

function withStatus(row, status) {
    const cells = splitRow(row.line);
    cells[2] = status;
    return rebuildRow(cells);
}

function withCell(row, index, value) {
    const cells = splitRow(row.line);
    cells[index] = value;
    return rebuildRow(cells);
}

function reloadLessons(root) {
    const parsed = parseLedger(read(path.join(root, LEDGER)));
    if (parsed.malformed.length) {
        throw new Error(`malformed ledger row(s): ${parsed.malformed.length}; run \`check\` for details`);
    }
    return parsed;
}

function saveRow(root, oldLine, newLine) {
    const file = path.join(root, LEDGER);
    const text = read(file);
    if (!text.includes(oldLine)) {
        throw new Error(`ledger row vanished: ${oldLine.slice(0, 40)}…`);
    }
    write(file, text.replace(oldLine, newLine));
}

function sectionBounds(lines, anchor) {
    const head = lines.findIndex((line) => new RegExp(`^## §${anchor}\\.`).test(line));
    if (head < 0) {
        return null;
    }
    let end = lines.length;
    for (let i = head + 1; i < lines.length; i += 1) {
        if (/^## /.test(lines[i])) {
            end = i;
            break;
        }
    }
    return { head, end };
}

// Rulebooks number rules in two styles: original `- **§K9.1 (P1)** ...` and lesson-generated
// `- **§K4.5** (P2) ...`. The anchor needs no escaping: TARGET only admits `[KT]\d+`.
function ruleNumber(line, anchor) {
    const match = line.match(new RegExp(`^- \\*\\*§${anchor}\\.(\\d+)(?: \\(P[123]\\))?\\*\\*`));
    return match ? Number(match[1]) : null;
}

function nextMinor(lines, bounds, anchor) {
    let max = 0;
    for (let i = bounds.head + 1; i < bounds.end; i += 1) {
        max = Math.max(max, ruleNumber(lines[i], anchor) ?? 0);
    }
    return max + 1;
}

function bumpVersion(text, book) {
    const re = new RegExp(`^RULEBOOK: ${book.name}@(\\d{4}-\\d{2}-\\d{2})\\.(\\d+)$`, 'm');
    const match = text.match(re);
    if (!match) {
        throw new Error(`no RULEBOOK: ${book.name}@ line in ${book.file}`);
    }
    const date = today();
    const minor = match[1] === date ? Number(match[2]) + 1 : 1;
    return text.replace(re, `RULEBOOK: ${book.name}@${date}.${minor}`);
}

function normalized(text) {
    return text.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
}

// A rule is mechanically appliable only if it is one general, self-contained sentence with no secret,
// no instruction to weaken verification and no markup that would break the ledger or the book.
function ruleProblem(rule) {
    const text = rule.trim();
    if (text.length < RULE_MIN) {
        return `rule too short (${text.length} < ${RULE_MIN} chars)`;
    }
    if (text.length > RULE_MAX) {
        return `rule too long (${text.length} > ${RULE_MAX} chars)`;
    }
    if (!/\.$/.test(text)) {
        return 'rule must be one sentence ending with a full stop';
    }
    if (SECRET.test(text)) {
        return 'rule looks like it carries a credential';
    }
    if (INSTRUCTION.test(text)) {
        return 'rule reads as an instruction to weaken or skip verification';
    }
    if (/\b[A-Z][a-z]+\s+[A-Z][a-z]+\b.*\b(ticket|asked|customer|user)\b/.test(text)) {
        return 'rule looks like a one-off event instead of a stack-level rule';
    }
    return null;
}

function reviewKey(root, row) {
    return createHash('sha256').update(JSON.stringify([
        row.line, ...Object.values(BOOKS).map(book => read(path.join(root, book.file))),
    ])).digest('hex');
}

// A fingerprint establishes freshness, not who reviewed it. Require an auditable, repository-owned
// approval record as well. The record is copied from the reviewer, never emitted by review-key/apply.
function approvalRecord(root, row, fingerprint) {
    if (!/^[a-f0-9]{64}$/.test(fingerprint ?? '')) return null;
    const runs = read(path.join(root, LEDGER)).split('## Runs')[1]?.split(/\n## /)[0] ?? '';
    for (const log of runLogs(root)) {
        if (log.dir !== '.claude/goal-runs' || !log.valid || !runs.includes(log.name)) continue;
        const file = path.join(root, log.dir, log.name);
        // Local .codex logs and symlinks outside the committed run directory are not PR evidence.
        if (fs.realpathSync(file) !== file) continue;
        const lines = read(file).split(/\r?\n/);
        let inReview = false;
        let fence = null;
        for (let i = 0; i < lines.length; i += 1) {
            const line = lines[i];
            const delimiter = line.match(/^\s*(`{3,}|~{3,})/);
            if (delimiter) {
                if (!fence) fence = delimiter[1];
                else if (delimiter[1][0] === fence[0] && delimiter[1].length >= fence.length) fence = null;
                continue;
            }
            if (fence) continue;
            if (/^## /.test(line)) inReview = line === '## Review';
            if (!inReview || line !== `LESSON-APPROVED ${row.id} ${fingerprint}`
                || !lines[i + 1]?.startsWith('LESSON-RULE ') || lines[i + 2] !== 'VERDICT=APPROVE') continue;
            try {
                const approved = JSON.parse(lines[i + 1].slice('LESSON-RULE '.length));
                if (approved.target === row.target && approved.rule === row.rule) return `${log.dir}/${log.name}`;
            } catch { /* Malformed approval is not evidence. */ }
        }
    }
    return null;
}

function reviewStamp(row) {
    const fingerprint = row.status.raw.match(/\breview=([a-f0-9]{64})\b/)?.[1];
    return fingerprint ? `; review=${fingerprint}` : '';
}

// Coverage policy is maintained explicitly, never learned automatically. Semantic review remains
// mandatory for every other rule: a word filter cannot prove consistency with existing requirements.
function protectedCoverage(row) {
    return (row.targetRef?.book === 'tests' && /^T[4-8]$/.test(row.targetRef.anchor))
        || /coverage|jacoco|§T5\b|85\s*%|\b(?:threshold|bar|percent)\b|\d\s*%/i.test(row.rule);
}

function planApply(root, row, reviewed) {
    const ref = row.targetRef;
    if (!ref) {
        const scoped = /^(conventions|tests)\b/.test(row.target.trim());
        return scoped
            ? { review: `target "${row.target}" must be conventions|tests §<K|T><n> (P1|P2|P3)` }
            : { manual: `outside the automatic write-back (target "${row.target}")` };
    }
    if (protectedCoverage(row)) {
        return { manual: 'protected policy: tests §T4–§T8 and coverage/threshold rules require explicit maintenance; ≥85% measured per touched module remains mandatory' };
    }
    const book = BOOKS[ref.book];
    const problem = ruleProblem(row.rule);
    if (problem) {
        return { review: problem };
    }
    if (!/\b([0-9a-f]{7,40}|GEO-\d+|goal-runs\/)\b/i.test(row.evidence)) {
        return { review: 'evidence has no commit sha, Jira key or run log' };
    }
    const file = path.join(root, book.file);
    const text = read(file);
    const lines = text.split('\n');
    const bounds = sectionBounds(lines, ref.anchor);
    if (!bounds) {
        return { review: `section §${ref.anchor} not found in ${book.label}` };
    }
    if (reviewed !== reviewKey(root, row)) {
        return { review: 'semantic approval required for the exact row and current rulebooks; use review-key --id ' + row.id };
    }
    if (!approvalRecord(root, row, reviewed)) {
        return { review: 'approval artifact missing: record LESSON-APPROVED, LESSON-RULE and VERDICT=APPROVE under ## Review in a registered .claude run log' };
    }
    if (text.includes(`(${row.id})`)) {
        return { review: 'lesson is already cited; reconcile the existing rule explicitly' };
    }
    const duplicate = lines.slice(bounds.head + 1, bounds.end).some((line) => normalized(line).includes(normalized(row.rule)));
    if (duplicate) {
        return { review: `an equivalent rule already exists in §${ref.anchor}` };
    }
    const minor = ref.minor === null ? nextMinor(lines, bounds, ref.anchor) : Number(ref.minor);
    if (lines.some(line => ruleNumber(line, ref.anchor) === minor)) {
        return { review: 'target rule number already exists' };
    }
    const sentence = row.rule.trim().replace(/\s*\(K\d{2}\)\.?$/, '');
    const newLine = `- **§${ref.anchor}.${minor}** (${ref.severity}) ${sentence.replace(/\.$/, '')} (${row.id}).`;
    let at = bounds.end;
    while (at - 1 > bounds.head && lines[at - 1].trim() === '') {
        at -= 1;
    }
    return { book, file, lines, at, newLine, anchor: ref.anchor, minor };
}

function applyOne(root, row, dryRun, reviewed) {
    const plan = planApply(root, row, reviewed);
    if (plan.review) {
        return { id: row.id, review: plan.review };
    }
    if (plan.manual) {
        return { id: row.id, manual: plan.manual };
    }
    plan.lines.splice(plan.at, 0, plan.newLine);
    const body = bumpVersion(plan.lines.join('\n'), plan.book);
    if (!dryRun) {
        write(plan.file, body);
        saveRow(root, row.line, withStatus(row, `applied@${today()} (review=${reviewed})`));
    }
    return { id: row.id, applied: `${plan.book.label} §${plan.anchor}.${plan.minor}`, line: plan.newLine };
}

function cmdApply(root, args) {
    const { lines } = reloadLessons(root);
    const wanted = new Set(args.id ?? []);
    const rows = parseLedger(lines.join('\n')).rows
        .filter((row) => row.status.code === 'proposed')
        .filter((row) => !wanted.size || wanted.has(row.id));
    if (!rows.length) {
        console.log(`LESSONS=APPLY nothing proposed${wanted.size ? ` for ${[...wanted].join(', ')}` : ''}`);
        return 0;
    }
    const applied = [];
    const review = [];
    const manual = [];
    for (const row of rows) {
        const result = applyOne(root, row, args['dry-run'], args.reviewed);
        (result.review ? review : result.manual ? manual : applied).push(result);
    }
    applied.forEach((row) => console.log(`applied  ${row.id} → ${row.applied}${row.adopted ? ' (adopted)' : ''}`));
    review.forEach((row) => console.log(`review   ${row.id} — ${row.review}`));
    manual.forEach((row) => console.log(`manual   ${row.id} — ${row.manual}`));
    console.log(`LESSONS=APPLY applied=${applied.length} review=${review.length} manual=${manual.length}`);
    return 0;
}

function runLogs(root) {
    const logs = [];
    for (const dir of RUN_DIRS) {
        const abs = path.join(root, dir);
        if (!fs.existsSync(abs)) {
            continue;
        }
        for (const name of fs.readdirSync(abs)) {
            if (!name.endsWith('.md')) {
                continue;
            }
            const file = path.join(abs, name);
            const head = read(file).split('\n')[0];
            const status = (head.match(/\bstatus: ([a-z-]+)/) || [])[1] ?? null;
            const start = (head.match(/start: (\d{4}-\d{2}-\d{2})/) || [])[1] ?? null;
            const valid = /^# /.test(head) && ['running', 'waiting-user', 'escalated', 'stopped', 'done'].includes(status)
                && start !== null && !Number.isNaN(Date.parse(start))
                && new Date(start).toISOString().slice(0, 10) === start;
            const recurred = (head.match(/recurred: ([^\n]*)/) || [])[1] ?? null;
            const full = read(file);
            // the last `recurred:` line wins; a log with several is flagged by `check`
            const markers = [...full.matchAll(/^recurred: (.*)$/gm)].map((match) => match[1].trim());
            const loop = (head.match(/loop: da\.loop@(\d{4}-\d{2}-\d{2})/) || [])[1] ?? null;
            logs.push({
                name,
                dir,
                start,
                status,
                valid,
                loop,
                markers: markers.length,
                recurred: markers.length ? markers[markers.length - 1] : recurred,
            });
        }
    }
    return logs;
}

// Retire only the exact generated line in its declared section. Never remove a base rule,
// a changed/multiline rule, or any coverage policy automatically; require explicit maintenance instead.
function retireRule(root, row, dryRun) {
    const ref = row.targetRef;
    if (!ref || protectedCoverage(row)) return 'protected or manual target requires explicit retirement';
    const book = BOOKS[ref.book];
    const file = path.join(root, book.file);
    const text = read(file);
    const lines = text.split('\n');
    const bounds = sectionBounds(lines, ref.anchor);
    if (!bounds) return 'target section missing';
    const sentence = row.rule.trim().replace(/\s*\(K\d{2}\)\.?$/, '').replace(/\.$/, '');
    const suffix = `** (${ref.severity}) ${sentence} (${row.id}).`;
    const matches = lines.map((line, i) => ({ line, i })).filter(({ line, i }) =>
        i > bounds.head && i < bounds.end && line.startsWith(`- **§${ref.anchor}.`)
        && line.endsWith(suffix));
    if (matches.length !== 1 || lines.filter(line => line.includes(`(${row.id})`)).length !== 1) {
        return 'rule is missing, modified or ambiguous; explicit retirement required';
    }
    const i = matches[0].i;
    if (lines[i + 1]?.trim() && !/^(?:- |## )/.test(lines[i + 1])) return 'multiline rule requires explicit retirement';
    lines.splice(i, 1);
    const body = bumpVersion(lines.join('\n'), book);
    if (!dryRun) write(file, body);
    return null;
}

function cmdValidate(root, args) {
    const { rows } = reloadLessons(root);
    const logs = runLogs(root);
    const touched = [];
    let blocked = rows.filter(row => row.status.code === 'blocked').length;
    let failed = blocked > 0;
    for (const log of logs.filter(log => !log.valid)) console.log(`warn     ${log.name}: invalid header; excluded from validation`);
    for (const row of rows) {
        if (!['applied', 'validated'].includes(row.status.code) || !row.status.date) continue;
        // Legacy statuses only carry a date. Exclude the whole application day rather than infer
        // chronology. Missing/duplicate/malformed markers and unfinished runs are not evidence.
        const appliedDate = row.status.raw.match(/applied-at=(\d{4}-\d{2}-\d{2})/)?.[1] ?? row.status.date;
        const after = logs.filter(log => log.valid && log.start > appliedDate
            && log.status === 'done' && log.markers === 1
            && /^(?:none|K\d{2}(?:,\s*K\d{2})*)$/.test(log.recurred));
        const recurred = after.filter(log => log.recurred.split(/,\s*/).includes(row.id));
        if (recurred.length) {
            const problem = retireRule(root, row, args['dry-run']);
            if (problem) {
                const note = `retirement blocked: ${problem}; recurrence: ${recurred[0].name}`;
                const evidence = row.evidence.includes(note) ? row.evidence : `${row.evidence} · ${note}`;
                const status = `blocked@${today()} (applied-at=${appliedDate}${reviewStamp(row)})`;
                if (!args['dry-run']) saveRow(root, row.line, withCell({ line: withStatus(row, status) }, 6, evidence));
                touched.push(`blocked ${row.id} — ${problem}; explicit maintenance required`);
                blocked += 1;
                failed = true;
                continue;
            }
            const note = `recurrence: ${recurred[0].name}`;
            const evidence = row.evidence.includes(note) ? row.evidence : `${row.evidence} · ${note}`;
            const line = withCell({ line: withStatus(row, `reverted@${today()} (applied-at=${appliedDate}${reviewStamp(row)})`) }, 6, evidence);
            if (!args['dry-run']) saveRow(root, row.line, line);
            touched.push(`reverted ${row.id} — removed its exact rule; file a refined lesson`);
        } else if (row.status.code === 'applied' && after.length >= VALIDATE_AFTER_RUNS) {
            const line = withStatus(row, `validated@${today()} (applied-at=${appliedDate}${reviewStamp(row)}; no recurrence in ${after.length} runs)`);
            if (!args['dry-run']) saveRow(root, row.line, line);
            touched.push(`validated ${row.id} — no recurrence in ${after.length} runs`);
        }
    }
    touched.forEach(line => console.log(line));
    console.log(`LESSONS=VALIDATE changed=${touched.length} runs=${logs.length} invalid=${logs.filter(log => !log.valid).length} blocked=${blocked}`);
    return failed ? 1 : 0;
}

function cmdCheck(root) {
    const errors = [];
    const warnings = [];
    const ledgerFile = path.join(root, LEDGER);
    if (!fs.existsSync(ledgerFile)) {
        console.log(`LESSONS=FAIL\nmissing  ${LEDGER}`);
        return 1;
    }
    const parsed = parseLedger(read(ledgerFile));
    const logs = runLogs(root);
    const ids = new Map();
    for (const row of parsed.rows) {
        if (ids.has(row.id)) {
            errors.push(`duplicate lesson id ${row.id}`);
        }
        ids.set(row.id, row);
        if (!STATUSES.has(row.status.code)) {
            errors.push(`${row.id}: unknown status "${row.status.code}"`);
        }
        if (row.status.code === 'proposed') {
            const plan = planApply(root, row);
            if (plan.manual) {
                warnings.push(`${row.id} manual: ${plan.manual}`);
            } else if (plan.review) {
                warnings.push(`${row.id} review: ${plan.review}`);
            }
        }
    }
    parsed.malformed.forEach((line) => errors.push(`malformed row: ${line.slice(0, 60)}…`));

    // every run log has a Runs row (tracked logs only; .codex/goal-runs is local, gitignored)
    const runsSection = read(ledgerFile).split('## Runs')[1]?.split(/\n## /)[0] ?? '';
    for (const log of logs) {
        if (!log.valid) warnings.push(`${log.name}: invalid header; excluded from validation`);
        if (log.dir !== '.claude/goal-runs') {
            continue;
        }
        if (!runsSection.includes(log.name)) {
            errors.push(`run log ${log.name} has no row under ## Runs`);
        }
    }

    if (/\|[^\n]*\|\n\s*\n\s*\|/.test(runsSection)) {
        errors.push('Runs table is split by blank lines; keep all rows contiguous');
    }

    // every cited (Kxx) exists
    for (const file of CITATION_FILES) {
        const abs = path.join(root, file);
        if (!fs.existsSync(abs)) {
            errors.push(`missing ${file}`);
            continue;
        }
        const text = read(abs);
        for (const match of text.matchAll(CITATION)) {
            for (const id of match[1].split(/,\s*/)) {
                if (!ids.has(id)) {
                    errors.push(`${file}: cites ${id}, not in ${LEDGER}`);
                }
            }
        }
    }

    // an applied/validated rule is present in its book
    for (const row of parsed.rows) {
        if (!['applied', 'validated', 'reverted', 'blocked'].includes(row.status.code)) {
            continue;
        }
        if (row.status.code === 'blocked') {
            errors.push(`${row.id}: retirement blocked; resolve explicit maintenance recorded in evidence: ${row.evidence}`);
        }
        const ref = row.targetRef;
        if (!ref) {
            warnings.push(`${row.id}: ${row.status.code} targets "${row.target}" — outside the automatic write-back, verify by hand`);
            continue;
        }
        const fingerprint = row.status.raw.match(/\breview=([a-f0-9]{64})\b/)?.[1];
        if ((row.status.code !== 'reverted' || fingerprint) && !approvalRecord(root, row, fingerprint)) {
            errors.push(`${row.id}: ${row.status.code} without a matching approval artifact`);
        }
        const book = read(path.join(root, BOOKS[ref.book].file));
        if (row.status.code === 'reverted') {
            if (book.includes(`(${row.id})`)) errors.push(`${row.id}: reverted but its rule is still active`);
            continue;
        }
        if (!book.includes(`(${row.id})`)) {
            errors.push(`${row.id}: ${row.status.code} but no rule line cites it in ${BOOKS[ref.book].file}`);
        }
    }

    // runs using da.loop@2026-11-02 or newer must declare recurrence so validate() can work
    for (const log of runLogs(root)) {
        if (log.dir !== '.claude/goal-runs') {
            continue;
        }
        if (log.markers > 1) {
            warnings.push(`${log.name}: ${log.markers} "recurred:" lines; excluded from validation`);
        }
        if (log.recurred === null && log.loop && log.loop >= '2026-11-02') {
            warnings.push(`${log.name}: no "recurred:" marker (excluded from validation)`);
        }
    }

    const status = errors.length ? 'FAIL' : 'OK';
    console.log(`LESSONS=${status} lessons=${parsed.rows.length} runs=${logs.length} invalid=${logs.filter(log => !log.valid).length}`);
    [...errors.map((line) => `error    ${line}`), ...warnings.map((line) => `warn     ${line}`)]
        .forEach((line) => console.log(line));
    return errors.length ? 1 : 0;
}

function cmdStatus(root) {
    const { rows } = reloadLessons(root);
    const counts = {};
    rows.forEach((row) => {
        counts[row.status.code] = (counts[row.status.code] ?? 0) + 1;
    });
    console.log(`lessons=${rows.length} ${Object.entries(counts).map(([k, v]) => `${k}=${v}`).join(' ')}`);
    rows.forEach((row) => console.log(`${row.id}\t${row.status.raw}\t${row.target}`));
    return 0;
}

function main() {
    const [command = 'check', ...rest] = process.argv.slice(2);
    const { values: args } = parseArgs({
        args: rest,
        options: {
            id: { type: 'string', multiple: true },
            reviewed: { type: 'string' },
            'dry-run': { type: 'boolean', default: false },
        },
        allowPositionals: false,
    });
    let root;
    try {
        root = repoRoot();
    } catch (error) {
        console.log(`LESSONS=FAIL\nmissing  ${error.message}`);
        return 1;
    }
    switch (command) {
        case 'review-key': {
            const rows = reloadLessons(root).rows.filter(row => args.id?.includes(row.id));
            if (args.id?.length !== 1 || rows.length !== 1) {
                console.error('review-key requires exactly one existing --id');
                return 2;
            }
            console.log(reviewKey(root, rows[0]));
            return 0;
        }
        case 'apply':
            return cmdApply(root, args);
        case 'validate':
            return cmdValidate(root, args);
        case 'status':
            return cmdStatus(root);
        case 'check':
            return cmdCheck(root);
        default:
            console.error(`unknown command ${command}; use check|apply|validate|status|review-key`);
            return 2;
    }
}

process.exitCode = main();
