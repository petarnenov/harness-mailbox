#!/usr/bin/env node
// da.loop: render the Codex and Copilot adapters of the da agents from the canonical Claude files.
//
// Canonical: .claude/agents/<name>.md (frontmatter + body).
// Rendered:  .codex/agents/<name>.toml      (Codex project custom agent body)
//            .codex/config.toml             (Codex project layer: subagent defaults + [agents.<name>] registrations —
//                                            a TOML file in .codex/agents is not discovered on its own, codex-cli 0.163)
//            .github/agents/<name>.agent.md (Copilot CLI project custom agent)
// Usage (from anywhere inside the checkout): node <skill-dir>/scripts/render-agents.mjs [--check]
// --check: exit 1 and list the files that differ from what would be rendered; nothing is written.
// Node >= 18, no dependencies. Never edit the rendered files by hand; change the Claude file and re-render.
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const AGENTS = ['da-python-writer', 'da-python-test-writer', 'da-python-reviewer', 'da-lesson-reviewer'];
// Model selection is inherited from the current Codex or Copilot session.
// Claude tool → Copilot CLI tool group (docs: read, grep, glob, edit, execute).
const COPILOT_TOOL = { Read: 'read', Grep: 'grep', Glob: 'glob', Edit: 'edit', Write: 'edit', Bash: 'execute' };
const GENERATED = 'rendered by .claude/skills/da.loop/scripts/render-agents.mjs from .claude/agents/%s.md — do not edit';
const HOST_NOTE = 'This host neither preloads the rulebooks nor runs the Claude path-guard hook: read the books named below '
    + 'when their `RULEBOOK:` lines are not in your context, and treat the file boundary and allowed commands as binding '
    + 'even though nothing enforces them.';

function repoRoot() {
    const top = spawnSync('git', ['rev-parse', '--show-toplevel'], { encoding: 'utf8' });
    if (top.status !== 0) {
        throw new Error('run inside the repository');
    }
    return top.stdout.trim();
}

function parseClaudeAgent(file) {
    const text = fs.readFileSync(file, 'utf8');
    const match = text.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n([\s\S]*)$/);
    if (!match) {
        throw new Error(`no frontmatter in ${file}`);
    }
    const [, frontmatter, body] = match;
    const scalar = (key) => (frontmatter.match(new RegExp(`^${key}: (.*)$`, 'm')) || [])[1]?.trim() ?? '';
    const unquote = (value) => value.replace(/^"([\s\S]*)"$/, '$1').replace(/\\"/g, '"');
    return {
        name: scalar('name'),
        description: unquote(scalar('description')),
        tools: scalar('tools').split(',').map((t) => t.trim()).filter(Boolean),
        body: body.trim() + '\n',
    };
}

function withHostNote(body) {
    return HOST_NOTE + '\n\n' + body;
}

function tomlString(value) {
    return JSON.stringify(value);
}

function renderCodex(agent) {
    if (agent.body.includes("'''")) {
        throw new Error(`${agent.name}: body contains ''' which cannot be embedded in a TOML literal string`);
    }
    return [
        `# ${GENERATED.replace('%s', agent.name)}`,
        `name = ${tomlString(agent.name)}`,
        `description = ${tomlString(agent.description)}`,
        `developer_instructions = '''`,
        withHostNote(agent.body) + `'''`,
        '',
    ].join('\n');
}

function renderCopilot(agent) {
    const tools = [...new Set(agent.tools.map((t) => COPILOT_TOOL[t]).filter(Boolean))];
    return [
        '---',
        `# ${GENERATED.replace('%s', agent.name)}`,
        `name: ${agent.name}`,
        `description: ${JSON.stringify(agent.description)}`,
        `tools: ${JSON.stringify(tools)}`,
        '---',
        '',
        withHostNote(agent.body),
    ].join('\n');
}

function renderCodexConfig(agents) {
    const registrations = agents.map((agent) => [
        `[agents.${agent.name}]`,
        `description = ${tomlString(agent.description)}`,
        `config_file = ${tomlString('agents/' + agent.name + '.toml')}`,
    ].join('\n'));
    return [
        `# ${GENERATED.replace('%s', '{agents}')}`,
        '# Project layer for Codex CLI; loads once this checkout is a trusted project.',
        '',
        ...registrations.map((r) => r + '\n'),
    ].join('\n');
}

function main() {
    const check = process.argv.includes('--check');
    const root = repoRoot();
    const drift = [];
    const agents = AGENTS.map((name) => parseClaudeAgent(path.join(root, '.claude/agents', name + '.md')));
    const outputs = { [path.join(root, '.codex/config.toml')]: renderCodexConfig(agents) };
    for (const agent of agents) {
        outputs[path.join(root, '.codex/agents', agent.name + '.toml')] = renderCodex(agent);
        outputs[path.join(root, '.github/agents', agent.name + '.agent.md')] = renderCopilot(agent);
    }
    {
        for (const [file, content] of Object.entries(outputs)) {
            const current = fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : null;
            if (current === content) {
                continue;
            }
            if (check) {
                drift.push(path.relative(root, file));
            } else {
                fs.mkdirSync(path.dirname(file), { recursive: true });
                fs.writeFileSync(file, content);
                console.log('rendered ' + path.relative(root, file));
            }
        }
    }
    if (check) {
        console.log(drift.length ? 'RENDER=DRIFT\n' + drift.map((f) => 'drift    ' + f).join('\n') : 'RENDER=OK');
        return drift.length ? 1 : 0;
    }
    return 0;
}

process.exitCode = main();
