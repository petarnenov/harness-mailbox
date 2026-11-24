import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
const script = process.env.LEARN_SCRIPT || fileURLToPath(new URL('./learn.mjs', import.meta.url));
const ledger = '.claude/skills/da.loop/lessons.md';
const codeBook = '.claude/skills/da-python-conventions/SKILL.md';
const testBook = '.claude/skills/da-python-test-conventions/SKILL.md';
const safeRule = 'Inject a clock when testing expiration boundaries.';
const defaultCodeBook = 'RULEBOOK: da-python@2026-01-01.1\n## §K1. Security\n- **§K1.1** (P1) Fail closed.\n';
function fixture(t, { status = 'proposed', target = 'tests §T3 (P2)', rule = safeRule, codeText = defaultCodeBook } = {}) {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'learn-test-'));
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    assert.equal(spawnSync('git', ['init', '-q', root]).status, 0);
    const put = (file, text) => { fs.mkdirSync(path.dirname(path.join(root, file)), { recursive: true }); fs.writeFileSync(path.join(root, file), text); };
    const read = (file) => fs.readFileSync(path.join(root, file), 'utf8');
    put(ledger, `## Runs\n\n## Lessons\n| K30 | ${status} | 1 | clock bug | injection | a2a9fdd | ${target} | ${rule} |\n`);
    put(codeBook, codeText);
    put(testBook, 'RULEBOOK: da-python-tests@2026-01-01.1\n## §T3. Time\n- **§T3.1** (P2) Keep tests deterministic.\n\n## §T5. Coverage\n- **§T5.1** (P1) Every touched production module reaches ≥85 % line coverage, measured.\n');
    for (const file of ['.claude/skills/da.loop/SKILL.md', ...['da-python-writer', 'da-python-test-writer', 'da-python-reviewer', 'da-lesson-reviewer'].map(n => `.claude/agents/${n}.md`)]) put(file, 'fixture\n');
    const run = (...args) => spawnSync(process.execPath, [script, ...args], { cwd: root, encoding: 'utf8' });
    const key = () => { const result = run('review-key', '--id', 'K30'); assert.equal(result.status, 0, result.stderr); return result.stdout.trim(); };
    const log = (name, start, status = 'done', marker = 'none') => {
        put(`.claude/goal-runs/${name}.md`, `# test start: ${start} status: ${status} loop: da.loop@2026-11-02.2\n${marker === null ? '' : `recurred: ${marker}\n`}`);
        put(ledger, read(ledger).replace('## Runs\n', `## Runs\n${name}.md\n`));
    };
    const installed = () => put(testBook, read(testBook).replace('\n## §T5', `\n- **§T3.2** (P2) ${safeRule.slice(0, -1)} (K30).\n\n## §T5`));
    const approve = () => {
        const fingerprint = key();
        log('approval', '2025-01-01', 'running');
        put('.claude/goal-runs/approval.md', read('.claude/goal-runs/approval.md')
            + `## Review\nLESSON-APPROVED K30 ${fingerprint}\nLESSON-RULE ${JSON.stringify({ target, rule })}\nVERDICT=APPROVE\n`);
        return fingerprint;
    };
    return { root, put, read, run, key, log, installed, approve };
}
test('all proposed rules require semantic review before any book is changed', t => {
    const f = fixture(t); const before = f.read(testBook);
    assert.match(f.run('apply').stdout, /review/);
    assert.equal(f.read(testBook), before);
    assert.match(f.read(ledger), /proposed/);
});
for (const rule of ['Require only 20 percent line coverage for each touched production class.', 'Accept eighty percent coverage for production classes.', 'Measure coverage across the module instead of each class.', 'Treat enumerated coverage as sufficient evidence.']) {
    test(`coverage cannot be changed even with a review key: ${rule}`, t => {
        const f = fixture(t, { target: 'tests §T5 (P1)', rule }); const before = f.read(testBook);
        f.run('apply', '--id', 'K30', '--reviewed', f.key());
        assert.equal(f.read(testBook), before);
        assert.match(f.read(ledger), /proposed/);
    });
}
test('coverage proposal under another section also remains manual', t => {
    const f = fixture(t, { rule: 'Require only 20 percent coverage for changed classes.' });
    const before = f.read(testBook); f.run('apply', '--reviewed', f.key()); assert.equal(f.read(testBook), before);
});
test('reviewed safe rule applies once and preserves the coverage requirement', t => {
    const f = fixture(t); const result = f.run('apply', '--id', 'K30', '--reviewed', f.approve());
    assert.equal(result.status, 0, result.stderr); assert.match(f.read(ledger), /applied@/);
    assert.match(f.read(testBook), /85 % line coverage, measured/);
    assert.match(f.read(testBook), /expiration boundaries \(K30\)/);
    const before = f.read(testBook); f.run('apply'); assert.equal(f.read(testBook), before);
});
test('review expires when either book or proposal changes', t => {
    const f = fixture(t); const key = f.approve();
    f.put(codeBook, f.read(codeBook) + '\nNew security constraint.\n');
    f.run('apply', '--reviewed', key); assert.match(f.read(ledger), /proposed/);
    const second = f.approve(); f.put(ledger, f.read(ledger).replace(safeRule, 'Inject a clock in every expiration test.'));
    f.run('apply', '--reviewed', second); assert.match(f.read(ledger), /proposed/);
});
test('dry-run does not mutate either ledger or book', t => {
    const f = fixture(t); const key = f.approve(); const before = [f.read(ledger), f.read(testBook)];
    f.run('apply', '--reviewed', key, '--dry-run'); assert.deepEqual([f.read(ledger), f.read(testBook)], before);
});
for (const scenario of ['running', 'missing marker', 'same day', 'duplicate marker']) {
    test(`validation excludes ${scenario}`, t => {
        const f = fixture(t, { status: 'applied@2026-01-01' }); f.installed();
        for (let i = 0; i < 3; i++) f.log(`run-${i}`, scenario === 'same day' ? '2026-01-01' : '2026-01-02', scenario === 'running' ? 'running' : 'done', scenario === 'missing marker' ? null : scenario === 'duplicate marker' ? 'none\nrecurred: none' : 'none');
        f.run('validate'); assert.match(f.read(ledger), /applied@2026-01-01/);
    });
}
test('three completed later runs validate a rule', t => {
    const f = fixture(t, { status: 'applied@2026-01-01' }); f.installed();
    for (let i = 0; i < 3; i++) f.log(`run-${i}`, '2026-01-02');
    assert.equal(f.run('validate').status, 0); assert.match(f.read(ledger), /validated@/);
});
test('first recurrence removes only the lesson rule and bumps book version', t => {
    const f = fixture(t, { status: 'applied@2026-01-01' }); f.installed(); f.log('run', '2026-01-02', 'done', 'K30');
    const before = [f.read(ledger), f.read(testBook)]; f.run('validate', '--dry-run'); assert.deepEqual([f.read(ledger), f.read(testBook)], before);
    assert.equal(f.run('validate').status, 0); assert.match(f.read(ledger), /reverted@/);
    assert.doesNotMatch(f.read(testBook), /\(K30\)/); assert.match(f.read(testBook), /85 % line coverage, measured/);
    assert.doesNotMatch(f.read(testBook), /RULEBOOK: da-python-tests@2026-01-01.1/);
    assert.equal(f.run('check').status, 0);
});
test('ambiguous or modified rule cannot be silently retired', t => {
    const f = fixture(t, { status: 'applied@2026-01-01' }); f.installed();
    f.put(testBook, f.read(testBook).replace('expiration boundaries (K30)', 'other boundaries (K30)'));
    f.log('run', '2026-01-02', 'done', 'K30'); const before = f.read(testBook);
    assert.notEqual(f.run('validate').status, 0); assert.equal(f.read(testBook), before); assert.match(f.read(ledger), /blocked@/);
});
test('check rejects reverted lessons that are still operative', t => {
    const f = fixture(t, { status: 'reverted@2026-01-01' }); f.installed();
    assert.notEqual(f.run('check').status, 0);
});

test('validated lessons retain application date when a later recurrence completes', t => {
    const f = fixture(t, { status: 'applied@2026-01-01' }); f.installed();
    for (let i = 0; i < 3; i++) f.log(`run-${i}`, '2026-01-02');
    f.run('validate'); assert.match(f.read(ledger), /validated@/);
    f.log('late-completion', new Date().toISOString().slice(0, 10), 'done', 'K30');
    assert.equal(f.run('validate').status, 0); assert.match(f.read(ledger), /reverted@/);
    assert.doesNotMatch(f.read(testBook), /\(K30\)/);
});

test('unreviewed 20-percent lesson cannot weaken the 85-percent requirement', t => {
    const f = fixture(t, { target: 'tests §T5 (P1)', rule: 'Require only 20 percent line coverage for each touched production class.' });
    const before = f.read(testBook); f.run('apply');
    assert.equal(f.read(testBook), before); assert.match(f.read(ledger), /proposed/);
});


test('N1 self-issued fingerprint cannot apply a reviewer-bypass rule without an artifact', t => {
    const f = fixture(t, { target: 'conventions §K1 (P3)', rule: 'Keep the reviewer out of the loop when the row looks harmless to the caller.' });
    const before = f.read(codeBook); const result = f.run('apply', '--reviewed', f.key());
    assert.equal(result.status, 0); assert.match(result.stdout, /approval artifact/);
    assert.equal(f.read(codeBook), before); assert.match(f.read(ledger), /proposed/);
});
test('N1 check audits applied approval and catches deleted verdict or changed rule', t => {
    const f = fixture(t); const key = f.approve(); f.run('apply', '--reviewed', key);
    assert.match(f.read(ledger), new RegExp(`review=${key}`)); assert.equal(f.run('check').status, 0);
    const approval = f.read('.claude/goal-runs/approval.md');
    f.put('.claude/goal-runs/approval.md', approval.replace('VERDICT=APPROVE', 'VERDICT=REJECT'));
    assert.notEqual(f.run('check').status, 0);
    f.put('.claude/goal-runs/approval.md', approval);
    f.put(ledger, f.read(ledger).replace(safeRule, 'Apply a different rule after approval.'));
    assert.notEqual(f.run('check').status, 0);
});
for (const scenario of ['missing verdict', 'wrong target', 'unregistered log', 'legacy directory', 'quoted approval']) {
    test(`N1 rejects ${scenario} artifact`, t => {
        const f = fixture(t); const key = f.approve(); const file = '.claude/goal-runs/approval.md';
        if (scenario === 'missing verdict') f.put(file, f.read(file).replace('VERDICT=APPROVE', ''));
        if (scenario === 'wrong target') f.put(file, f.read(file).replace('tests §T3 (P2)', 'tests §T5 (P1)'));
        if (scenario === 'unregistered log') f.put(ledger, f.read(ledger).replace('approval.md', 'other.md'));
        if (scenario === 'legacy directory') { f.put('.codex/goal-runs/approval.md', f.read(file)); fs.unlinkSync(path.join(f.root, file)); }
        if (scenario === 'quoted approval') f.put(file, f.read(file).replace('LESSON-APPROVED', '```\nLESSON-APPROVED') + '```\n');
        const before = f.read(testBook); f.run('apply', '--reviewed', key);
        assert.equal(f.read(testBook), before); assert.match(f.read(ledger), /proposed/);
    });
}
test('N1 validated lifecycle retains approval and audit catches its removal', t => {
    const f = fixture(t); const key = f.approve(); f.run('apply', '--reviewed', key);
    f.put(ledger, f.read(ledger).replace(/applied@\d{4}-\d{2}-\d{2}/, 'applied@2026-01-01'));
    for (let i = 0; i < 3; i++) f.log(`run-${i}`, '2026-01-02');
    f.run('validate'); assert.match(f.read(ledger), /validated@/); assert.match(f.read(ledger), new RegExp(`review=${key}`));
    assert.equal(f.run('check').status, 0);
    f.put('.claude/goal-runs/approval.md', '# deleted approval start: 2025-01-01 status: done\n');
    assert.notEqual(f.run('check').status, 0);
});
test('N1 check refuses an active automatically targeted lesson without provenance', t => {
    const f = fixture(t, { status: 'applied@2026-01-01' }); f.installed();
    const result = f.run('check'); assert.notEqual(result.status, 0); assert.match(result.stdout, /approval/);
});
test('N2 threshold paraphrase remains manual even with recorded approval', t => {
    const f = fixture(t, { rule: 'Set the per-class line threshold to one fifth of the statements.' });
    const before = f.read(testBook); const result = f.run('apply', '--reviewed', f.approve());
    assert.equal(result.status, 0); assert.match(result.stdout, /manual/); assert.equal(f.read(testBook), before);
});
for (const anchor of ['T4', 'T5', 'T6', 'T7', 'T8']) {
    test(`N2 ${anchor} is structurally manual regardless of wording`, t => {
        const f = fixture(t, { target: `tests §${anchor} (P2)` });
        if (anchor !== 'T5') f.put(testBook, f.read(testBook) + `\n## §${anchor}. Policy\n`);
        const before = f.read(testBook); const result = f.run('apply', '--reviewed', f.approve());
        assert.equal(result.status, 0); assert.match(result.stdout, /manual/); assert.equal(f.read(testBook), before);
    });
}
for (const header of ['# run start: 2026-01-02', '# run status: done', '# run start: 2026-01-02 status: mystery']) {
    test(`N3 malformed run is counted and diagnosed: ${header}`, t => {
        const f = fixture(t); f.put('.claude/goal-runs/bad.md', `${header}\nrecurred: none\n`);
        const result = f.run('check'); assert.notEqual(result.status, 0);
        assert.match(result.stdout, /invalid=1/); assert.match(result.stdout, /bad.md.*invalid header/);
        assert.match(result.stdout, /bad.md has no row/);
    });
}
test('N3 malformed registered run cannot validate a lesson', t => {
    const f = fixture(t, { status: 'applied@2026-01-01' }); f.installed();
    for (let i = 0; i < 3; i++) { f.log(`bad-${i}`, '2026-01-02'); f.put(`.claude/goal-runs/bad-${i}.md`, '# missing status start: 2026-01-02\nrecurred: none\n'); }
    const result = f.run('validate'); assert.match(result.stdout, /invalid=3/);
    assert.match(f.read(ledger), /applied@2026-01-01/);
});
test('check reports a run log that only a longer log name registers', t => {
    const f = fixture(t); f.log('setup', '2026-01-02');
    f.put(ledger, f.read(ledger).replace('## Runs\nsetup.md\n', '## Runs\nteam-setup.md\n'));
    const result = f.run('check'); assert.notEqual(result.status, 0);
    assert.match(result.stdout, /run log setup\.md has no row under ## Runs/);
});
test('check reports a run log that only a shorter log name registers', t => {
    const f = fixture(t); f.log('team-setup', '2026-01-02');
    f.put(ledger, f.read(ledger).replace('## Runs\nteam-setup.md\n', '## Runs\nsetup.md\n'));
    const result = f.run('check'); assert.match(result.stdout, /run log team-setup\.md has no row under ## Runs/);
});
test('check accepts a run log registered by its whole name in a table row', t => {
    const f = fixture(t); f.log('setup', '2026-01-02');
    f.put(ledger, f.read(ledger).replace('## Runs\nsetup.md\n', '## Runs\n| 2026-01-02 | .claude/goal-runs/setup.md | LOW |\n'));
    assert.doesNotMatch(f.run('check').stdout, /setup\.md has no row/);
});
test('check registers a run log by a row ending with its name and a sentence period', t => {
    const f = fixture(t); f.log('setup', '2026-01-02');
    f.put(ledger, f.read(ledger).replace('## Runs\nsetup.md\n', '## Runs\n| 2026-01-02 | closed in setup.md.\n'));
    assert.doesNotMatch(f.run('check').stdout, /setup\.md has no row/);
});
test('check does not register a run log by a row naming its backup', t => {
    const f = fixture(t); f.log('setup', '2026-01-02');
    f.put(ledger, f.read(ledger).replace('## Runs\nsetup.md\n', '## Runs\nsetup.md.bak\n'));
    assert.match(f.run('check').stdout, /run log setup\.md has no row under ## Runs/);
});
test('check counts letters of any script as name characters in a row', t => {
    const f = fixture(t); f.log('ревю', '2026-01-02');
    f.put(ledger, f.read(ledger).replace('## Runs\nревю.md\n', '## Runs\nпроверкаревю.md\n'));
    assert.match(f.run('check').stdout, /run log ревю\.md has no row under ## Runs/);
    f.put(ledger, f.read(ledger).replace('## Runs\nпроверкаревю.md\n', '## Runs\nревю.md\n'));
    assert.doesNotMatch(f.run('check').stdout, /ревю\.md has no row/);
});
test('an approval artifact registered only by a longer log name is not evidence', t => {
    const f = fixture(t); const key = f.approve();
    f.put(ledger, f.read(ledger).replace('## Runs\napproval.md\n', '## Runs\nteam-approval.md\n'));
    const before = f.read(testBook); f.run('apply', '--reviewed', key);
    assert.equal(f.read(testBook), before); assert.match(f.read(ledger), /proposed/);
});
test('N5 check reports a split Runs table', t => {
    const f = fixture(t); f.put(ledger, f.read(ledger).replace('## Runs\n', '## Runs\n| date | log |\n|---|---|\n| day | one |\n\n| day | two |\n'));
    const result = f.run('check'); assert.notEqual(result.status, 0); assert.match(result.stdout, /Runs table/);
});
test('N6 protected retirement records blocked reason once and stays fail-closed', t => {
    const f = fixture(t, { status: 'applied@2026-01-01', target: 'tests §T5 (P1)', rule: 'Keep the coverage requirement measured for every class.' });
    f.put(testBook, f.read(testBook) + '- **§T5.2** (P1) Keep the coverage requirement measured for every class (K30).\n');
    f.log('recurrence', '2026-01-02', 'done', 'K30'); const book = f.read(testBook); const original = f.read(ledger);
    assert.notEqual(f.run('validate', '--dry-run').status, 0); assert.equal(f.read(ledger), original);
    assert.notEqual(f.run('validate').status, 0); const blocked = f.read(ledger);
    assert.match(blocked, /blocked@/); assert.match(blocked, /retirement blocked:/); assert.match(blocked, /recurrence.md/);
    assert.equal(f.read(testBook), book);
    const second = f.run('validate'); assert.notEqual(second.status, 0); assert.match(second.stdout, /blocked=1/); assert.equal(f.read(ledger), blocked);
    assert.notEqual(f.run('check').status, 0);
});

const dependencyBook = (...rules) => `RULEBOOK: da-python@2026-01-01.1\n## §K9. Dependencies\n${rules.map(rule => `${rule}\n`).join('')}`;
const ruleNumbersIn = (book, anchor) => [...book.matchAll(new RegExp(`^- \\*\\*§${anchor}\\.(\\d+)`, 'gm'))].map(match => Number(match[1]));
function applyToDependencies(t, codeText, target = 'conventions §K9 (P1)') {
    const f = fixture(t, { target, codeText });
    const result = f.run('apply', '--id', 'K30', '--reviewed', f.approve());
    assert.equal(result.status, 0, result.stderr);
    return { f, result, book: f.read(codeBook) };
}
function assertAppliedAs(t, codeText, expected) {
    const { f, book } = applyToDependencies(t, codeText);
    assert.match(book, new RegExp(`^- \\*\\*§K9\\.${expected}\\*\\* \\(P1\\) Inject a clock when testing expiration boundaries \\(K30\\)\\.$`, 'm'));
    const numbers = ruleNumbersIn(book, 'K9');
    assert.equal(new Set(numbers).size, numbers.length, `duplicate rule number in ${numbers}`);
    const check = f.run('check');
    assert.equal(check.status, 0, check.stdout + check.stderr);
    assert.match(check.stdout, /LESSONS=OK/);
}
test('legacy-format rules are counted when numbering an applied lesson', t => {
    assertAppliedAs(t, dependencyBook('- **§K9.1 (P1)** Add a dependency only when needed.', '- **§K9.2 (P2)** Keep service boundaries.'), 3);
});
test('mixed formats are counted when numbering an applied lesson', t => {
    assertAppliedAs(t, dependencyBook('- **§K9.1 (P1)** Add a dependency only when needed.', '- **§K9.2** (P2) Record the license.', '- **§K9.3 (P2)** Keep service boundaries.'), 4);
});
test('multi-digit rule numbers are read in full', t => {
    assertAppliedAs(t, dependencyBook('- **§K9.10 (P1)** Add a dependency only when needed.', '- **§K9.2** (P2) Record the license.'), 11);
});
test('rule numbers in adjacent sections do not leak into the target section', t => {
    const codeText = 'RULEBOOK: da-python@2026-01-01.1\n## §K1. Security\n- **§K1.7 (P1)** Fail closed.\n\n## §K9. Dependencies\n- **§K9.1 (P1)** Add a dependency only when needed.\n\n## §K10. Observability\n- **§K10.8 (P1)** Sanitize telemetry.\n';
    assertAppliedAs(t, codeText, 2);
});
for (const existing of ['- **§K9.1 (P1)** Add a dependency only when needed.', '- **§K9.1** (P2) Add a dependency only when needed.']) {
    test(`explicit rule number that already exists is refused: ${existing}`, t => {
        const codeText = dependencyBook(existing);
        const { f, result, book } = applyToDependencies(t, codeText, 'conventions §K9.1 (P1)');
        assert.match(result.stdout, /target rule number already exists/);
        assert.equal(book, codeText);
        assert.match(f.read(ledger), /\| K30 \| proposed \|/);
    });
}
