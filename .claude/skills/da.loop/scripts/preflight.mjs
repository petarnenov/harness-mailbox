#!/usr/bin/env node
// da.loop pre-flight: are the project agents, rulebooks and toolchain a run needs present and current?
//
// Usage (from anywhere inside the checkout): node <skill-dir>/scripts/preflight.mjs [--module all|backend|frontend|orchestrator|infra] [--host claude|codex|copilot|all]
// First output line: PREFLIGHT=OK|MISSING|STALE module=<m>; then one line per problem.
// Exit 0 = OK, 1 = something missing or stale. Node >= 18, no dependencies.
//
// Everything da.loop depends on lives inside this repository (.claude/agents, .claude/skills), so the
// skill is shared through git and no user-level copy is consulted.
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { closingGaps, requiresAdrReview } from './run-close-hook.mjs';

const AGENTS = ['da-python-writer', 'da-python-test-writer', 'da-python-reviewer', 'da-lesson-reviewer'];
const BOOKS = ['da-python-conventions', 'da-python-test-conventions'];
const LOOP_FILES = ['da.loop/SKILL.md', 'da.loop/lessons.md', 'da.loop/references/cost-controls.md',
    'da.loop/references/stack.md', 'da.merge/SKILL.md', 'da.commit/SKILL.md',
    'da.jira.comment/SKILL.md', 'da.jira.retro/SKILL.md', 'da.jira.estimate.hh/SKILL.md',
    'da.sync/SKILL.md', 'da.sync/scripts/sync-da.mjs', 'da.sync/scripts/sync-da.test.mjs',
    'da.sync/assets/lessons.template.md', 'da.loop.sync/SKILL.md', 'da.loop.sync/scripts/loop-sync.mjs',
    'da.loop.sync/scripts/loop-sync.test.mjs'];
const HOOK_FILES = ['.claude/hooks/path-guard.py', '.claude/skills/da.loop/scripts/g1-spec-review-hook.mjs',
    '.claude/skills/da.loop/scripts/run-close-hook.mjs', '.claude/skills/da.loop/scripts/adr-gate-hook.mjs'];
const RENDERER = '.claude/skills/da.loop/scripts/render-agents.mjs';
const LEARN = '.claude/skills/da.loop/scripts/learn.mjs';
const LOOP_TESTS = ['.claude/skills/da.loop/scripts/learn.test.mjs', '.claude/skills/da.loop/scripts/run-close-hook.test.mjs',
    '.claude/skills/da.loop/scripts/adr-gate-hook.test.mjs'];
const LEDGER = '.claude/skills/da.loop/lessons.md';
const RUN_LOGS = '.claude/goal-runs';
// Runs started from this loop version on carry the closing contract (`## Report`, retro, ledger row); older
// logs predate it and are not judged.
const CLOSING_CONTRACT_SINCE = '2026-11-20.1';
// Files each non-Claude host needs; the agent adapters are rendered from the Claude files and checked for drift.
const HOST_FILES = {
    codex: ['AGENTS.md', '.codex/config.toml', '.codex/hooks.json', ...AGENTS.map((a) => `.codex/agents/${a}.toml`),
        ...BOOKS.map((b) => `.agents/skills/${b}/SKILL.md`), '.agents/skills/da.loop/SKILL.md',
        '.agents/skills/da.merge/SKILL.md', '.agents/skills/da.commit/SKILL.md', '.agents/skills/da.jira.comment/SKILL.md',
        '.agents/skills/da.jira.retro/SKILL.md', '.agents/skills/da.jira.estimate.hh/SKILL.md',
        '.agents/skills/da.sync/SKILL.md', '.agents/skills/da.loop.sync/SKILL.md'],
    copilot: ['AGENTS.md', '.github/hooks/da-loop.json', ...AGENTS.map((a) => `.github/agents/${a}.agent.md`)],
};
// Paths each dashboard component needs; the repository is one module run from its root through `make`.
const MODULES = {
    backend: ['server.py', 'dependency_graph.py', 'dispatch_gate.py', 'delivery_process.py', 'tests'],
    frontend: ['static', 'tests/js'],
    orchestrator: ['workspace/.agents/skills/mailbox-orchestrator/scripts'],
    infra: ['Dockerfile', 'Dockerfile.tools', 'compose.yaml', 'scripts/workflow.sh'],
};
const REQUIRED_TOOLS = [['git', '--version'], ['node', '--version'], ['make', '--version'], ['docker', '--version']];
const VERSION_LINE = /^(?:RULEBOOK|LOOP|SYNC): (\S+)$/m;
const REQUIRED_NODE_MAJOR = 18;

const isFile = (file) => fs.existsSync(file) && fs.statSync(file).isFile();
const versionOf = (file) => (fs.readFileSync(file, 'utf8').match(VERSION_LINE) || [])[1] ?? null;

// The `skills:` list of a Claude agent's frontmatter.
function frontmatterSkills(agentFile) {
    const skills = [];
    let inList = false;
    for (const line of fs.readFileSync(agentFile, 'utf8').split('---')[1].split(/\r?\n/)) {
        if (line.startsWith('skills:')) {
            inList = true;
        } else if (inList && line.trimStart().startsWith('- ')) {
            skills.push(line.split('- ')[1].trim());
        } else {
            inList = false;
        }
    }
    return skills;
}

class Report {
    constructor(root) {
        this.root = root;
        this.missing = [];
        this.stale = [];
        this.checks = [];
    }

    require(file) {
        if (isFile(file)) {
            return true;
        }
        this.missing.push('missing  ' + path.relative(this.root, file));
        return false;
    }
}

function checkBooks(root, report) {
    for (const book of BOOKS) {
        const file = path.join(root, '.claude/skills', book, 'SKILL.md');
        if (report.require(file) && versionOf(file) === null) {
            report.missing.push('missing  RULEBOOK line in ' + path.relative(root, file));
        }
    }
    for (const name of LOOP_FILES) {
        const file = path.join(root, '.claude/skills', name);
        if (report.require(file) && name.endsWith('SKILL.md') && versionOf(file) === null) {
            report.missing.push('missing  RULEBOOK/LOOP/SYNC version line in ' + path.relative(root, file));
        }
    }
}

function checkAgents(root, report) {
    for (const agent of AGENTS) {
        const agentFile = path.join(root, '.claude/agents', agent + '.md');
        if (!report.require(agentFile)) {
            continue;
        }
        const skills = frontmatterSkills(agentFile);
        if (!skills.length) {
            report.stale.push(`stale    ${agent}.md has no skills: list (rulebooks are not preloaded)`);
        }
        for (const skill of skills) {
            report.require(path.join(root, '.claude/skills', skill, 'SKILL.md'));
        }
    }
    HOOK_FILES.forEach((file) => report.require(path.join(root, file)));
}

// A personal agent of the same name would shadow the project one with different rules.
function checkShadowing(report) {
    const home = process.env.HOME || '';
    for (const agent of AGENTS) {
        const personal = path.join(home, '.claude/agents', agent + '.md');
        if (isFile(personal)) {
            report.stale.push(`stale    ~/.claude/agents/${agent}.md shadows the project agent; remove or align it`);
        }
    }
}

// The G1 hook runs on the host's node; the skill frontmatter skips it silently when node is absent or too old.
function checkNode(report) {
    const major = Number((process.versions.node.match(/^(\d+)/) || [])[1]);
    if (major < REQUIRED_NODE_MAJOR) {
        report.stale.push(`stale    node ${process.versions.node} on PATH; the G1 hook needs node >= ${REQUIRED_NODE_MAJOR}`);
    }
}

// Tests, lint and coverage run in isolated Docker containers through `make`; host Python is not required.
function checkToolchain(root, paths, report) {
    for (const [tool, flag] of REQUIRED_TOOLS) {
        if (spawnSync(tool, [flag], { encoding: 'utf8' }).status !== 0) report.missing.push(`missing  ${tool} on PATH`);
    }
    report.require(path.join(root, 'Makefile'));
    for (const relative of paths) {
        if (!fs.existsSync(path.join(root, relative))) report.missing.push('missing  ' + relative);
    }
}

// The ledger must stay consistent with the books and the run logs, or the loop silently stops learning.
function checkLessons(root, report) {
    const script = path.join(root, LEARN);
    if (!report.require(script)) {
        return;
    }
    const run = spawnSync(process.execPath, [script, 'check'], { encoding: 'utf8', cwd: root });
    const text = (run.stdout || run.stderr || '').trim();
    if (run.status !== 0) {
        const problems = text.split('\n').filter((line) => line.startsWith('error')).slice(0, 3);
        report.stale.push('stale    lessons ledger: ' + (problems.join('; ') || text.split('\n')[0] || 'check failed'));
    }
}

// Run for every host, including Claude. These CLI tests use temporary repositories and never
// mutate the user's books. Do not inherit the test-only baseline override from the caller.
function checkLoopTests(root, report) {
    const env = { ...process.env };
    delete env.LEARN_SCRIPT;
    for (const test of LOOP_TESTS.map((file) => path.join(root, file))) {
        if (!report.require(test)) continue;
        const result = spawnSync(process.execPath, ['--test', test], {
            cwd: root, encoding: 'utf8', env, timeout: 60000,
        });
        if (result.status !== 0) {
            const detail = (result.stderr || result.stdout || result.error?.message || 'test process failed').trim();
            report.stale.push(`stale    loop regression tests failed (${path.basename(test)}): `
                + detail.split('\n').slice(0, 3).join(' '));
        } else {
            report.checks.push(`TESTS=OK ${path.basename(test)}`);
        }
    }
}

const loopVersion = (header) => (header.match(/\bloop: da\.loop@(\S+)/) || [])[1];
const atLeast = (version, since) => version.localeCompare(since, 'en', { numeric: true }) >= 0;

// A finished run under the closing contract that skipped its report, retro or ledger row is unfinished work;
// this catches it on every host, including Copilot, which has no Stop hook.
function checkRunClosing(root, report) {
    const dir = path.join(root, RUN_LOGS);
    const ledgerFile = path.join(root, LEDGER);
    if (!fs.existsSync(dir) || !isFile(ledgerFile)) return;
    const ledger = fs.readFileSync(ledgerFile, 'utf8');
    for (const name of fs.readdirSync(dir).filter((file) => file.endsWith('.md')).sort()) {
        const text = fs.readFileSync(path.join(dir, name), 'utf8');
        const version = loopVersion(text.split('\n', 1)[0]);
        if (!version || !atLeast(version, CLOSING_CONTRACT_SINCE)) continue;
        const gaps = closingGaps(name, text, ledger, { adrRequired: requiresAdrReview(root, text) });
        if (gaps.length) {
            report.stale.push(`stale    run ${name} is not closed (finish steps 9-10): ${gaps.join('; ')}`);
        }
    }
}

function checkHosts(root, host, report) {
    const hosts = host === 'all' ? Object.keys(HOST_FILES) : host === 'claude' ? [] : [host];
    for (const name of hosts) {
        HOST_FILES[name].forEach((file) => report.require(path.join(root, file)));
    }
    if (!hosts.length) {
        return;
    }
    const render = spawnSync(process.execPath, [path.join(root, RENDERER), '--check'], { encoding: 'utf8' });
    if (render.status !== 0) {
        report.stale.push('stale    rendered agent adapters differ from .claude/agents (run render-agents.mjs): '
            + (render.stdout || render.stderr).trim().split('\n').slice(1).join(', '));
    }
}

function main() {
    const { values: args } = parseArgs({
        options: { module: { type: 'string', default: 'all' }, host: { type: 'string', default: 'claude' } },
    });
    const known = ['all', ...Object.keys(MODULES)];
    const knownHosts = ['claude', 'all', ...Object.keys(HOST_FILES)];
    if (!known.includes(args.module) || !knownHosts.includes(args.host)) {
        console.error('usage: preflight.mjs [--module ' + known.join('|') + '] [--host ' + knownHosts.join('|') + ']');
        return 2;
    }
    const heading = `module=${args.module} host=${args.host}`;
    const top = spawnSync('git', ['rev-parse', '--show-toplevel'], { encoding: 'utf8' });
    if (top.status !== 0) {
        console.log(`PREFLIGHT=MISSING ${heading}\nmissing  git checkout (run inside the repo)`);
        return 1;
    }
    const root = top.stdout.trim();
    const paths = args.module === 'all' ? Object.values(MODULES).flat() : MODULES[args.module];
    const report = new Report(root);
    checkBooks(root, report);
    checkAgents(root, report);
    checkLessons(root, report);
    checkLoopTests(root, report);
    checkRunClosing(root, report);
    checkShadowing(report);
    checkNode(report);
    checkHosts(root, args.host, report);
    checkToolchain(root, paths, report);
    const status = report.missing.length ? 'MISSING' : report.stale.length ? 'STALE' : 'OK';
    console.log(`PREFLIGHT=${status} ${heading}`);
    [...report.missing, ...report.stale, ...report.checks].forEach((line) => console.log(line));
    return status === 'OK' ? 0 : 1;
}

process.exitCode = main();
