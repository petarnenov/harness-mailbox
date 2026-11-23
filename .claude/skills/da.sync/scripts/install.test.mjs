import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const installer = fileURLToPath(new URL('./install.mjs', import.meta.url));

test('missing Zeplin token stops before copy; setup resumes and preserves existing MCP entries', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'da-install-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const bin = path.join(root, 'bin');
  const state = path.join(root, 'state');
  const target = path.join(root, 'sample-project');
  fs.mkdirSync(bin);
  fs.mkdirSync(state);
  fs.mkdirSync(target);
  fs.writeFileSync(path.join(state, 'copilot-atlassian'), 'https://mcp.atlassian.com/v2/mcp\n');
  const host = path.join(bin, 'copilot');
  fs.writeFileSync(host, `#!/bin/sh
if [ "$1" = "--version" ]; then exit 0; fi
if [ "$1" = "mcp" ] && [ "$2" = "get" ]; then cat "$FAKE_MCP_STATE/copilot-$3" 2>/dev/null; exit $?; fi
if [ "$1" = "mcp" ] && [ "$2" = "add" ]; then
  shift 2
  for arg in "$@"; do
    case "$arg" in atlassian|zeplin|chrome-devtools) name="$arg";; esac
  done
  [ -n "$name" ] || exit 2
  printf '%s\\n' "$*" > "$FAKE_MCP_STATE/copilot-$name"
  exit 0
fi
exit 2
`);
  fs.chmodSync(host, 0o755);
  const npx = path.join(bin, 'npx');
  fs.writeFileSync(npx, '#!/bin/sh\nexit 0\n');
  fs.chmodSync(npx, 0o755);
  const env = { ...process.env, PATH: `${bin}:${process.env.PATH}`, FAKE_MCP_STATE: state };
  const run = (extraEnv = {}) => spawnSync(process.execPath, [installer, '--target', target, '--hosts', 'copilot', '--apply'], {
    encoding: 'utf8', env: { ...env, ...extraEnv },
  });
  const blocked = run();
  assert.equal(blocked.status, 3, blocked.stderr);
  assert.match(blocked.stderr, /token unavailable/);
  assert.equal(fs.existsSync(path.join(target, '.claude')), false);
  const installed = run({ ZEPLIN_ACCESS_TOKEN: 'test-value' });
  assert.equal(installed.status, 0, installed.stderr);
  assert.match(installed.stdout, /MCP=added host=copilot server=zeplin/);
  assert.match(installed.stdout, /MCP=present host=copilot server=chrome-devtools/);
  assert.match(installed.stdout, /MCP_AUTH=VERIFY_REQUIRED/);
  assert.doesNotMatch(installed.stdout + installed.stderr, /test-value/);
  assert.equal(fs.readFileSync(path.join(state, 'copilot-atlassian'), 'utf8'), 'https://mcp.atlassian.com/v2/mcp\n');
  assert.equal(fs.existsSync(path.join(target, '.claude/skills/sp.sync/SKILL.md')), true);
});
