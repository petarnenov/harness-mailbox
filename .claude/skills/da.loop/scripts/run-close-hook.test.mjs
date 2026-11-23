import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { closingGaps, requiresAdrReview, stopReason } from './run-close-hook.mjs';

const script = fileURLToPath(new URL('./run-close-hook.mjs', import.meta.url));
const LOG = '2026-11-20-sample.md';
const LEDGER = `## Runs\n| 2026-11-20 | .claude/goal-runs/${LOG} | LOW |\n\n## Lessons\n`;

function runLog({ status = 'done', step = 10, tasks = '- [x] T1 · done', report = 'Changed one file.',
    retro = 'No friction.', recurred = ['recurred: none'], evidence = 'LESSONS=OK' } = {}) {
    return [`# sample ticket: none tier: LOW start: 2026-11-20T10:00Z status: ${status} step: ${step} loop: da.loop@2026-11-20.1`,
        '## Tasks', tasks, '## Evidence', evidence, '## Report', report, '## Retro', retro, ...recurred, ''].join('\n');
}

test('a fully closed run has no gaps', () => {
    assert.deepEqual(closingGaps(LOG, runLog(), LEDGER), []);
});

test('running and waiting-user runs are not judged by the closing contract', () => {
    assert.deepEqual(closingGaps(LOG, runLog({ status: 'running', step: 5, report: '', retro: '' }), ''), []);
    assert.deepEqual(closingGaps(LOG, runLog({ status: 'waiting-user', step: 4, report: '' }), ''), []);
});

test('a done run stopped after step 9 reports every missing closing step', () => {
    const gaps = closingGaps(LOG, runLog({ step: 9, report: '', retro: '', recurred: [], evidence: '' }), '## Runs\n');
    assert.equal(gaps.length, 6);
    assert.match(gaps.join('\n'), /step: 10/);
    assert.match(gaps.join('\n'), /## Report/);
    assert.match(gaps.join('\n'), /## Retro/);
    assert.match(gaps.join('\n'), /found 0/);
    assert.match(gaps.join('\n'), /## Runs/);
    assert.match(gaps.join('\n'), /LESSONS=/);
});

test('a retro holding only the recurred marker is not a retro', () => {
    assert.deepEqual(closingGaps(LOG, runLog({ retro: '' }), LEDGER), ['`## Retro` has no retro text (step 10.1)']);
});

test('two recurred markers are a gap', () => {
    const gaps = closingGaps(LOG, runLog({ recurred: ['recurred: none', 'recurred: K12'] }), LEDGER);
    assert.deepEqual(gaps, ['expected exactly one `recurred: none|K..` line, found 2 (step 10.1)']);
});

test('the ledger row must sit under ## Runs, not elsewhere', () => {
    const ledger = `## Runs\n\n## Lessons\n| K1 | evidence .claude/goal-runs/${LOG} |\n`;
    assert.deepEqual(closingGaps(LOG, runLog(), ledger), ['no row under `## Runs` in lessons.md (step 10.2)']);
});

test('done needs every task ticked; escalated and stopped runs may leave tasks open', () => {
    const open = '- [x] T1 · done\n- [ ] T2 · blocked';
    assert.deepEqual(closingGaps(LOG, runLog({ tasks: open }), LEDGER), ['`status: done` with an unticked task (step 9)']);
    assert.deepEqual(closingGaps(LOG, runLog({ status: 'escalated', tasks: open }), LEDGER), []);
    assert.deepEqual(closingGaps(LOG, runLog({ status: 'stopped', tasks: open }), LEDGER), []);
});

test('an escalated run still needs its report and retro', () => {
    const gaps = closingGaps(LOG, runLog({ status: 'escalated', report: '', retro: '' }), LEDGER);
    assert.deepEqual(gaps, ['`## Report` is missing or empty (step 9)', '`## Retro` has no retro text (step 10.1)']);
});

test('stopReason blocks a running run and an unclosed finished run, and passes a closed one', () => {
    assert.match(stopReason(`/r/${LOG}`, runLog({ status: 'running', step: 6 }), LEDGER), /still `status: running`/);
    assert.match(stopReason(`/r/${LOG}`, runLog({ report: '' }), LEDGER), /closing steps are incomplete: `## Report`/);
    assert.equal(stopReason(`/r/${LOG}`, runLog(), LEDGER), null);
    assert.equal(stopReason(`/r/${LOG}`, runLog({ status: 'waiting-user', step: 4 }), ''), null);
});

function repo(t) {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'close-hook-test-'));
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    assert.equal(spawnSync('git', ['init', '-q', root]).status, 0);
    fs.mkdirSync(path.join(root, '.claude/goal-runs'), { recursive: true });
    fs.mkdirSync(path.join(root, '.claude/skills/da.loop'), { recursive: true });
    fs.writeFileSync(path.join(root, '.claude/skills/da.loop/lessons.md'), LEDGER);
    const hook = () => spawnSync(process.execPath, [script], {
        input: JSON.stringify({ hook_event_name: 'Stop', cwd: root }), encoding: 'utf8',
        env: { ...process.env, CLAUDE_PROJECT_DIR: root },
    });
    return { root, hook };
}

test('the Stop hook blocks an unclosed recent run at most three times', (t) => {
    const { root, hook } = repo(t);
    fs.writeFileSync(path.join(root, '.claude/goal-runs', LOG), runLog({ report: '' }));
    for (let i = 0; i < 3; i++) {
        const out = hook();
        assert.equal(out.status, 0, out.stderr);
        assert.equal(JSON.parse(out.stdout).decision, 'block');
    }
    assert.equal(hook().stdout, '');
    assert.equal(fs.readFileSync(path.join(root, '.claude/goal-runs', LOG + '.closehook'), 'utf8'), '3');
});

test('the Stop hook ignores a run log not touched within two hours, and a closed run', (t) => {
    const { root, hook } = repo(t);
    const log = path.join(root, '.claude/goal-runs', LOG);
    fs.writeFileSync(log, runLog());
    assert.equal(hook().stdout, '');
    fs.writeFileSync(log, runLog({ status: 'running', step: 5 }));
    const old = new Date(Date.now() - 3 * 60 * 60 * 1000);
    fs.utimesSync(log, old, old);
    assert.equal(hook().stdout, '');
});

test('a run that needs the ADR review closes only when its last ADR verdict is PASS', () => {
    const adr = { adrRequired: true };
    const gap = ['the last `ADR-review:` line is not `PASS` (code writer used in a repository with ADRs)'];
    assert.deepEqual(closingGaps(LOG, runLog(), LEDGER, adr), gap);
    assert.deepEqual(closingGaps(LOG, runLog({ evidence: 'LESSONS=OK\nADR-review: BLOCK /r1' }), LEDGER, adr), gap);
    const passed = runLog({ evidence: 'LESSONS=OK\nADR-review: BLOCK /r1\nADR-review: PASS /r2' });
    assert.deepEqual(closingGaps(LOG, passed, LEDGER, adr), []);
    assert.deepEqual(closingGaps(LOG, runLog(), LEDGER), []);
});

test('requiresAdrReview needs both tracked ADRs and a code writer delegation', (t) => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'close-adr-test-'));
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    assert.equal(spawnSync('git', ['init', '-q', root]).status, 0);
    const withWriter = '# x status: done\n## Delegations\n- da-python-writer T1 done\n## Review\n';
    assert.equal(requiresAdrReview(root, withWriter), false);
    fs.mkdirSync(path.join(root, 'docs/adr'), { recursive: true });
    fs.writeFileSync(path.join(root, 'docs/adr/0001.md'), 'status: accepted\n');
    spawnSync('git', ['-C', root, 'add', 'docs']);
    assert.equal(requiresAdrReview(root, withWriter), true);
    assert.equal(requiresAdrReview(root, '# x status: done\n## Delegations\nnone\n## Review\nda-python-writer\n'), false);
});
