#!/usr/bin/env node
// Bootstrap the portable package on a colleague's machine before stack adaptation.
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { parseArgs } from 'node:util';
import { fileURLToPath } from 'node:url';

const ATLASSIAN_URL = 'https://mcp.atlassian.com/v2/mcp';
const HOSTS = ['copilot', 'codex', 'claude'];
const HELP = {
  copilot: 'Install/authenticate GitHub Copilot CLI, then use `copilot mcp list` and `/mcp` to inspect the connection.',
  codex: 'Install/authenticate Codex CLI, then use `codex mcp list` and `codex mcp login atlassian` if OAuth is needed.',
  claude: 'Install/authenticate Claude Code, then use `claude mcp list` and `/mcp` to complete OAuth.',
};

function run(command, args, options = {}) {
  return spawnSync(command, args, { encoding: 'utf8', stdio: options.stdio ?? 'pipe', env: options.env ?? process.env });
}

function available(command) {
  const result = run(command, ['--version']);
  return !result.error && result.status === 0;
}

function detail(host, name) {
  const result = run(host, ['mcp', 'get', name]);
  return result.status === 0 ? `${result.stdout}\n${result.stderr}` : null;
}

function addArgs(host, name) {
  if (name === 'atlassian') {
    if (host === 'copilot') return ['mcp', 'add', '--transport', 'http', name, ATLASSIAN_URL];
    if (host === 'codex') return ['mcp', 'add', name, '--url', ATLASSIAN_URL];
    return ['mcp', 'add', '--transport', 'http', '--scope', 'user', name, ATLASSIAN_URL];
  }
  const pkg = name === 'zeplin' ? '@zeplin/mcp-server@latest' : 'chrome-devtools-mcp@latest';
  const command = ['npx', '-y', pkg];
  if (host === 'copilot') return ['mcp', 'add', name, ...(name === 'zeplin' ? ['--env', `ZEPLIN_ACCESS_TOKEN=${process.env.ZEPLIN_ACCESS_TOKEN}`] : []), '--', ...command];
  if (host === 'codex') return ['mcp', 'add', name, ...(name === 'zeplin' ? ['--env', `ZEPLIN_ACCESS_TOKEN=${process.env.ZEPLIN_ACCESS_TOKEN}`] : []), '--', ...command];
  return ['mcp', 'add', name, '--scope', 'user', ...(name === 'zeplin' ? ['--env', `ZEPLIN_ACCESS_TOKEN=${process.env.ZEPLIN_ACCESS_TOKEN}`] : []), '--', ...command];
}

function instructions(host, name) {
  if (name === 'zeplin') return `For ${host}: install Node.js 20+, obtain a personal Zeplin token privately, export ZEPLIN_ACCESS_TOKEN in this terminal, then rerun. See https://support.zeplin.io/en/articles/11559086-zeplin-mcp-server`;
  if (name === 'chrome-devtools') return `For ${host}: install Node.js LTS, npm and current Chrome, then rerun. See https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/client-configurations.md`;
  return `For ${host}: configure Atlassian MCP v2 and complete OAuth in the host. See https://support.atlassian.com/atlassian-ai-gateway/docs/get-started-with-the-atlassian-remote-mcp-server/`;
}

function main() {
  const { values } = parseArgs({ options: {
    target: { type: 'string' },
    hosts: { type: 'string' },
    apply: { type: 'boolean', default: false },
    help: { type: 'boolean', default: false },
  } });
  if (values.help || !values.target) {
    console.log('Usage: node install.mjs --target <project> [--hosts copilot,codex,claude] [--apply]');
    console.log('Default is a read-only dependency preview. --apply configures missing MCP servers and copies the package.');
    return values.help ? 0 : 2;
  }
  const target = path.resolve(values.target);
  if (!fs.statSync(target).isDirectory()) throw new Error(`target is not a directory: ${target}`);
  const selected = values.hosts ? values.hosts.split(',').map(x => x.trim()) : HOSTS.filter(available);
  if (!selected.length || selected.some(x => !HOSTS.includes(x))) throw new Error('select at least one host: copilot,codex,claude');
  const problems = [];
  for (const host of selected) {
    if (!available(host)) { problems.push(`${host}: CLI unavailable. ${HELP[host]}`); continue; }
    for (const name of ['atlassian', 'zeplin', 'chrome-devtools']) {
      const current = detail(host, name);
      if (current) {
        if (name === 'atlassian' && /mcp\.atlassian\.com\/v1\//.test(current)) {
          problems.push(`${host}/atlassian: v1 configuration found. Migrate with https://support.atlassian.com/atlassian-ai-gateway/docs/how-to-upgrade-from-atlassian-mcp-v1-to-atlassian-mcp-v2/`);
        } else console.log(`MCP=present host=${host} server=${name}`);
        continue;
      }
      if (name !== 'atlassian' && !available('npx')) { problems.push(`${host}/${name}: npx unavailable. ${instructions(host, name)}`); continue; }
      if (name === 'zeplin' && !process.env.ZEPLIN_ACCESS_TOKEN) { problems.push(`${host}/${name}: token unavailable. ${instructions(host, name)}`); continue; }
      if (!values.apply) { console.log(`MCP=missing host=${host} server=${name}`); continue; }
      const added = run(host, addArgs(host, name));
      if (added.status !== 0 || !detail(host, name)) {
        problems.push(`${host}/${name}: setup failed (exit ${added.status ?? 'unavailable'}). ${instructions(host, name)}`);
      } else console.log(`MCP=added host=${host} server=${name}`);
    }
  }
  for (const problem of problems) console.error(`SETUP_REQUIRED ${problem}`);
  if (problems.length) return 3;
  const source = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..');
  const prefix = path.basename(path.dirname(path.dirname(fileURLToPath(import.meta.url)))).replace(/\.sync$/, '');
  const script = path.join(source, `.claude/skills/${prefix}.sync/scripts/sync-${prefix}.mjs`);
  const sync = run(process.execPath, [script, '--source', source, '--target', target, ...(values.apply ? ['--apply'] : [])]);
  process.stdout.write(sync.stdout ?? '');
  process.stderr.write(sync.stderr ?? '');
  if (sync.status !== 0) return sync.status ?? 1;
  console.log('MCP_AUTH=VERIFY_REQUIRED Test a real read-only Atlassian and Zeplin tool call and a Chrome DevTools page read in each installed host. Complete OAuth if prompted. Then adapt and verify the copied package using its sync skill.');
  return 0;
}

try { process.exitCode = main(); }
catch (error) { console.error(`INSTALL=ERROR ${error.message}`); process.exitCode = 1; }
