import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const SCRIPT = fileURLToPath(new URL('./sync-da.mjs', import.meta.url));
const TEST = fileURLToPath(new URL('./sync-da.test.mjs', import.meta.url));
const SOURCE_PREFIX = path.basename(TEST).match(/^sync-(.+)\.test\.mjs$/)[1];

function prefixFor(name) {
  const words = name
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .split(/[^A-Za-z0-9]+/)
    .filter(Boolean);
  return (words.length === 1 ? words[0].slice(0, 2) : words.map((word) => word[0]).join('')).toLowerCase();
}

function fixture(t, targetName = 'target') {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'da-sync-'));
  const source = path.join(root, 'source');
  const target = path.join(root, targetName);
  fs.mkdirSync(target, { recursive: true });
  write(source, `.claude/skills/${SOURCE_PREFIX}.alpha/SKILL.md`, 'alpha\n');
  write(source, `.claude/skills/${SOURCE_PREFIX}-beta/references/stack.md`, 'stack\n');
  write(source, `.claude/skills/${SOURCE_PREFIX}.sync/SKILL.md`,
    `# ${SOURCE_PREFIX.toUpperCase()} Sync\n${SOURCE_PREFIX}.sync ${SOURCE_PREFIX}.loop ${SOURCE_PREFIX}-micronaut the ${SOURCE_PREFIX} package\n`);
  write(source, `.claude/skills/${SOURCE_PREFIX}.sync/assets/lessons.template.md`,
    `${SOURCE_PREFIX}.loop target lessons\n`);
  write(source, `.claude/skills/${SOURCE_PREFIX}.sync/scripts/sync-${SOURCE_PREFIX}.mjs`, fs.readFileSync(SCRIPT));
  write(source, `.claude/skills/${SOURCE_PREFIX}.sync/scripts/sync-${SOURCE_PREFIX}.test.mjs`, fs.readFileSync(TEST));
  write(source, `.claude/skills/${SOURCE_PREFIX}.loop/SKILL.md`, 'loop\n');
  write(source, `.claude/skills/${SOURCE_PREFIX}.loop/scripts/learn.mjs`,
    [`const re = /loop: ${SOURCE_PREFIX}\\.loop@/;`, `copy the ${SOURCE_PREFIX} skills`, ''].join('\n'));
  write(source, `.claude/skills/${SOURCE_PREFIX}.loop/lessons.md`, 'SOURCE_SECRET_RUN_HISTORY\n');
  write(source, `.claude/skills/${SOURCE_PREFIX}.loop/references/upstream.json`, '{}\n');
  write(source, `.claude/skills/${SOURCE_PREFIX}.loop/references/example-origin/history.md`, 'local history\n');
  write(source, '.claude/skills/other/SKILL.md', 'not a package skill\n');
  write(source, `.claude/skills/${SOURCE_PREFIX}.alpha/goal-runs/private.md`, 'run history\n');
  write(source, `.claude/agents/${SOURCE_PREFIX}-alpha-writer.md`, 'agent\n');
  write(source, '.claude/agents/other.md', 'other\n');
  write(source, '.claude/hooks/path-guard.py', 'guard\n');
  write(source, '.codex/hooks.json', '{}\n');
  write(source, `.github/hooks/${SOURCE_PREFIX}-loop.json`, '{}\n');
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return { root, source, target, prefix: prefixFor(path.basename(target)) };
}

function write(root, relative, content) {
  const file = path.join(root, relative);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, content);
}

function run(source, target, apply = false, script = SCRIPT) {
  return spawnSync(process.execPath, [script, '--source', source, '--target', target, ...(apply ? ['--apply'] : [])], {
    encoding: 'utf8',
  });
}

function filesUnder(directory) {
  if (!fs.existsSync(directory)) return [];
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const file = path.join(directory, entry.name);
    return entry.isDirectory() ? filesUnder(file) : entry.isFile() ? [file] : [];
  });
}

function assertNoPrefix(target, prefix) {
  const roots = ['.claude/skills', '.claude/agents', '.github/hooks', '.codex'].map((dir) => path.join(target, dir));
  const stale = [`${prefix}.`, `${prefix}-`, `${prefix.toUpperCase()} `, `${prefix} skills`];
  for (const file of roots.flatMap(filesUnder)) {
    if (file.endsWith('.json') || file.endsWith('.md') || file.endsWith('.mjs') || file.endsWith('.py') || file.endsWith('.sh')) {
      const content = fs.readFileSync(file, 'utf8');
      for (const marker of stale) {
        assert.equal(content.includes(marker), false, `${path.relative(target, file)} still contains ${JSON.stringify(marker)}`);
      }
    }
  }
}

test('dry-run discovers every package skill and writes nothing', (t) => {
  const { source, target, prefix } = fixture(t);
  const result = run(source, target);
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, new RegExp(`SYNC=DRY_RUN prefix=${prefix} skills=4 `));
  assert.match(result.stdout, new RegExp(`\\.claude/skills/${prefix}-beta/references/stack\\.md`));
  assert.doesNotMatch(result.stdout, new RegExp(`\\.claude/skills/${SOURCE_PREFIX}\\.`));
  assert.doesNotMatch(result.stdout, /skills\/other/);
  assert.doesNotMatch(result.stdout, /goal-runs|upstream\.json|example-origin/);
  assert.equal(fs.existsSync(path.join(target, '.claude')), false);
  assert.equal(fs.existsSync(path.join(target, '.agents')), false);
});

test('apply renames paths and content, copies support files and creates relative links', (t) => {
  const { source, target, prefix } = fixture(t);
  const result = run(source, target, true);
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, new RegExp(`SYNC=OK prefix=${prefix} skills=4 `));
  assert.equal(fs.readFileSync(path.join(target, `.claude/skills/${prefix}.alpha/SKILL.md`), 'utf8'), 'alpha\n');
  assert.equal(fs.readFileSync(path.join(target, `.claude/agents/${prefix}-alpha-writer.md`), 'utf8'), 'agent\n');
  assert.equal(fs.readFileSync(path.join(target, `.claude/skills/${prefix}.sync/SKILL.md`), 'utf8'),
    `# ${prefix.toUpperCase()} Sync\n${prefix}.sync ${prefix}.loop ${prefix}-micronaut the ${prefix} package\n`);
  assertNoPrefix(target, SOURCE_PREFIX);
  assert.equal(fs.readFileSync(path.join(target, `.github/hooks/${prefix}-loop.json`), 'utf8'), '{}\n');
  assert.equal(fs.readFileSync(path.join(target, '.codex/hooks.json'), 'utf8'), '{}\n');
  assert.equal(fs.readlinkSync(path.join(target, `.agents/skills/${prefix}.sync`)), `../../.claude/skills/${prefix}.sync`);
  assert.equal(fs.existsSync(path.join(target, `.claude/skills/${prefix}.sync/scripts/sync-${prefix}.mjs`)), true);
  assert.equal(fs.existsSync(path.join(target, `.claude/skills/${prefix}.sync/scripts/sync-${prefix}.test.mjs`)), true);
  assert.equal(fs.readFileSync(path.join(target, `.claude/skills/${prefix}.loop/scripts/learn.mjs`), 'utf8'),
    [`const re = /loop: ${prefix}\\.loop@/;`, `copy the ${prefix} skills`, ''].join('\n'));
  assert.equal(fs.readFileSync(path.join(target, `.claude/skills/${prefix}.loop/lessons.md`), 'utf8'),
    `${prefix}.loop target lessons\n`);
  assert.equal(fs.readFileSync(path.join(target, `.claude/skills/${prefix}.loop/lessons.md`), 'utf8').includes('SOURCE_SECRET_RUN_HISTORY'), false);
  assert.equal(fs.existsSync(path.join(target, `.claude/skills/${prefix}.alpha/goal-runs`)), false);
  assert.equal(fs.existsSync(path.join(target, '.claude/skills/other')), false);
  assert.equal(fs.existsSync(path.join(target, '.claude/agents/other.md')), false);
  assert.equal(fs.existsSync(path.join(target, `.claude/skills/${SOURCE_PREFIX}.sync`)), false);
});

test('conflicts and existing configuration are preserved while missing files are installed', (t) => {
  const { source, target, prefix } = fixture(t);
  write(target, `.claude/skills/${prefix}.alpha/SKILL.md`, 'target adaptation\n');
  write(target, `.claude/agents/${prefix}-alpha-writer.md`, 'target agent\n');
  write(target, `.claude/skills/${prefix}.loop/lessons.md`, 'existing target ledger\n');
  write(target, '.codex/config.toml', '[mcp_servers.target]\nurl = "https://example.invalid"\n[providers.custom]\n');
  const preview = run(source, target);
  assert.equal(preview.status, 2);
  assert.match(preview.stdout, /SYNC=CONFLICTS /);
  const result = run(source, target, true);
  assert.equal(result.status, 2);
  assert.equal(fs.readFileSync(path.join(target, `.claude/skills/${prefix}.alpha/SKILL.md`), 'utf8'), 'target adaptation\n');
  assert.equal(fs.readFileSync(path.join(target, `.claude/agents/${prefix}-alpha-writer.md`), 'utf8'), 'target agent\n');
  assert.equal(fs.readFileSync(path.join(target, `.claude/skills/${prefix}.loop/lessons.md`), 'utf8'), 'existing target ledger\n');
  assert.equal(fs.readFileSync(path.join(target, '.codex/config.toml'), 'utf8'), '[mcp_servers.target]\nurl = "https://example.invalid"\n[providers.custom]\n');
  assert.doesNotMatch(result.stdout, /\.codex\/config\.toml/);
  assert.equal(fs.readFileSync(path.join(target, `.claude/skills/${prefix}.sync/SKILL.md`), 'utf8'),
    `# ${prefix.toUpperCase()} Sync\n${prefix}.sync ${prefix}.loop ${prefix}-micronaut the ${prefix} package\n`);
});

test('a second dry-run is idempotent and reports no conflicts', (t) => {
  const { source, target, prefix } = fixture(t);
  assert.equal(run(source, target, true).status, 0);
  const result = run(source, target);
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, new RegExp(`SYNC=DRY_RUN prefix=${prefix} skills=4 add=0 unchanged=17 conflicts=0`));
});

test('prefix derives from one-word, multi-word and camel-case project names', (t) => {
  const { root, source } = fixture(t);
  for (const [name, expected] of [['payments', 'pa'], ['sample-notes-app', 'sna'], ['fooBar', 'fb']]) {
    const target = path.join(root, name);
    fs.mkdirSync(target, { recursive: true });
    const result = run(source, target);
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, new RegExp(`SYNC=DRY_RUN prefix=${expected} skills=4 `));
  }
});

test('a ported package uses its own prefix as the next source and its tests remain runnable',
  { skip: process.env.KK_SYNC_SKIP_ROUNDTRIP === '1' }, (t) => {
    const first = fixture(t, 'alpha-project');
    assert.equal(run(first.source, first.target, true).status, 0);
    const secondTarget = path.join(first.root, 'beta-project');
    fs.mkdirSync(secondTarget);
    const portedScript = path.join(first.target, `.claude/skills/${first.prefix}.sync/scripts/sync-${first.prefix}.mjs`);
    const second = run(first.target, secondTarget, true, portedScript);
    assert.equal(second.status, 0, second.stderr);
    const secondPrefix = prefixFor(path.basename(secondTarget));
    assert.match(second.stdout, new RegExp(`SYNC=OK prefix=${secondPrefix} skills=4 `));
    assert.equal(fs.existsSync(path.join(secondTarget, `.claude/skills/${secondPrefix}.sync/SKILL.md`)), true);
    assert.equal(fs.existsSync(path.join(secondTarget, `.claude/skills/${first.prefix}.sync/SKILL.md`)), false);
    assertNoPrefix(secondTarget, first.prefix);

    const thirdTarget = path.join(first.root, 'gamma-project');
    fs.mkdirSync(thirdTarget);
    const portedSecondScript = path.join(secondTarget,
      `.claude/skills/${secondPrefix}.sync/scripts/sync-${secondPrefix}.mjs`);
    const third = run(secondTarget, thirdTarget, true, portedSecondScript);
    assert.equal(third.status, 0, third.stderr);
    const thirdPrefix = prefixFor(path.basename(thirdTarget));
    assert.match(third.stdout, new RegExp(`SYNC=OK prefix=${thirdPrefix} skills=4 `));
    assert.equal(fs.existsSync(path.join(thirdTarget, `.claude/skills/${thirdPrefix}.sync/SKILL.md`)), true);
    assertNoPrefix(thirdTarget, secondPrefix);

    const portedTest = path.join(secondTarget,
      `.claude/skills/${secondPrefix}.sync/scripts/sync-${secondPrefix}.test.mjs`);
    const tested = spawnSync(process.execPath, ['--test', portedTest], {
      encoding: 'utf8',
      env: { ...process.env, KK_SYNC_SKIP_ROUNDTRIP: '1' },
    });
    assert.equal(tested.status, 0, tested.stderr || tested.stdout);
  });

test('a symlinked destination ancestor is rejected without outside writes', (t) => {
  const { source, target } = fixture(t);
  const outside = path.join(path.dirname(target), 'outside');
  fs.mkdirSync(outside);
  fs.symlinkSync(outside, path.join(target, '.claude'), 'dir');
  const preview = run(source, target);
  assert.equal(preview.status, 1);
  assert.match(preview.stderr, /destination ancestor is a symlink: \.claude/);
  const applied = run(source, target, true);
  assert.equal(applied.status, 1);
  assert.equal(fs.readdirSync(outside).length, 0);
});

test('source and target must differ', (t) => {
  const { source } = fixture(t);
  const result = run(source, source);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /source and target must be different projects/);
});
