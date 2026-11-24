import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { adrDirs, adrReviewCount, isGatedCall } from './adr-gate-hook.mjs';

const script = fileURLToPath(new URL('./adr-gate-hook.mjs', import.meta.url));
const LOG = '.claude/goal-runs/2026-11-20-sample.md';
const RUNNING = '# sample start: 2026-11-20T10:00Z status: running step: 5 loop: da.loop@2026-11-20.3\n## Review\n';

function repo(t, { adr = true } = {}) {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'adr-gate-test-'));
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    assert.equal(spawnSync('git', ['init', '-q', root]).status, 0);
    const put = (file, text) => {
        fs.mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
        fs.writeFileSync(path.join(root, file), text);
    };
    if (adr) {
        put('docs/ADR/0001-decision.md', '---\nstatus: accepted\n---\n# Decision\n');
        assert.equal(spawnSync('git', ['-C', root, 'add', 'docs']).status, 0);
    }
    put(LOG, RUNNING);
    const hook = (payload, ...args) => spawnSync(process.execPath, [script, ...args], {
        input: JSON.stringify({ cwd: root, ...payload }), encoding: 'utf8',
        env: { ...process.env, CLAUDE_PROJECT_DIR: root },
    });
    const appendReview = () => fs.appendFileSync(path.join(root, LOG), 'ADR-review: PASS /tmp/report.md\n');
    const pending = () => fs.existsSync(path.join(root, LOG + '.adrpending'));
    return { root, put, hook, appendReview, pending };
}

const agent = (subagentType) => ({ hook_event_name: 'PreToolUse', tool_name: 'Agent', tool_input: { subagent_type: subagentType } });
const question = { hook_event_name: 'PreToolUse', tool_name: 'AskUserQuestion', tool_input: { questions: [] } };
const denied = (out) => JSON.parse(out.stdout).hookSpecificOutput.permissionDecision === 'deny';

test('adrDirs finds tracked adr directories in any case and ignores untracked ones', (t) => {
    const { root, put } = repo(t);
    put('build/adr/0001.md', 'untracked\n');
    put('adr/0002.md', 'root level\n');
    spawnSync('git', ['-C', root, 'add', 'adr']);
    assert.deepEqual(adrDirs(root), ['adr', 'docs/ADR']);
});

test('adrReviewCount counts only verdict lines at the start of a line', () => {
    assert.equal(adrReviewCount('ADR-review: PASS a\nnote ADR-review: PASS b\nADR-review: BLOCK c\nADR-review: maybe\n'), 2);
});

test('only the reviewer may be launched and no question may be asked while gated', () => {
    assert.equal(isGatedCall('Agent', { subagent_type: 'da-python-reviewer' }), false);
    assert.equal(isGatedCall('Agent', { subagent_type: 'da-python-test-writer' }), true);
    assert.equal(isGatedCall('AskUserQuestion', {}), true);
    assert.equal(isGatedCall('Bash', {}), false);
});

test('a finished writer gates agents, questions and the turn end until a new ADR review line exists', (t) => {
    const { hook, appendReview, pending } = repo(t);
    assert.equal(hook({ hook_event_name: 'SubagentStop' }, '--event', 'writer-done').stdout, '');
    assert.ok(pending());
    assert.ok(denied(hook(agent('da-python-test-writer'))));
    assert.ok(denied(hook(question)));
    assert.equal(hook(agent('da-python-reviewer')).stdout, '');
    assert.match(JSON.parse(hook({ hook_event_name: 'Stop' }).stdout).reason, /mode: adr/);
    appendReview();
    assert.equal(hook(agent('da-python-test-writer')).stdout, '');
    assert.equal(pending(), false);
    assert.equal(hook({ hook_event_name: 'Stop' }).stdout, '');
});

test('a review recorded before the writer finished does not clear the gate', (t) => {
    const { hook, appendReview } = repo(t);
    appendReview();
    hook({ hook_event_name: 'SubagentStop' }, '--event', 'writer-done');
    assert.ok(denied(hook(question)));
    appendReview();
    assert.equal(hook(question).stdout, '');
});

test('a second writer keeps the unresolved marker of the first', (t) => {
    const { root, hook, appendReview } = repo(t);
    appendReview();
    hook({}, '--event', 'writer-done');
    hook({}, '--event', 'writer-done');
    appendReview();
    assert.equal(JSON.parse(fs.readFileSync(path.join(root, LOG + '.adrpending'), 'utf8')).reviews, 1);
    assert.equal(hook(question).stdout, '');
});

test('a second writer after a recorded review is gated until its own review line exists', (t) => {
    const { hook, appendReview, pending } = repo(t);
    hook({}, '--event', 'writer-done');
    appendReview();
    hook({}, '--event', 'writer-done');
    const answer = hook(question);
    assert.notEqual(answer.stdout, '', 'the second writer needs its own ADR review');
    assert.ok(denied(answer));
    assert.ok(pending());
    assert.equal(JSON.parse(hook({ hook_event_name: 'Stop' }).stdout).decision, 'block');
    appendReview();
    assert.equal(hook(question).stdout, '');
    assert.equal(pending(), false);
});

test('the Stop gate blocks at most three times per run log', (t) => {
    const { hook } = repo(t);
    hook({}, '--event', 'writer-done');
    for (let i = 0; i < 3; i++) {
        assert.equal(JSON.parse(hook({ hook_event_name: 'Stop' }).stdout).decision, 'block');
    }
    assert.equal(hook({ hook_event_name: 'Stop' }).stdout, '');
});

test('without an adr directory or without an active run nothing is gated', (t) => {
    const noAdr = repo(t, { adr: false });
    noAdr.hook({}, '--event', 'writer-done');
    assert.equal(noAdr.pending(), false);
    const done = repo(t);
    done.put(LOG, RUNNING.replace('status: running', 'status: done'));
    done.hook({}, '--event', 'writer-done');
    assert.equal(done.pending(), false);
    assert.equal(done.hook(question).stdout, '');
});
