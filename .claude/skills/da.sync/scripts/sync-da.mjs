#!/usr/bin/env node
// Copy the canonical prefixed package without overwriting target-project changes.
// The source prefix is read from this skill's directory; the target prefix is derived from the target project name.
import fs from 'node:fs';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { fileURLToPath } from 'node:url';

const EXCLUDED_DIRS = new Set(['.git', 'node_modules', 'goal-runs', '__pycache__']);
const EXCLUDED_FILES = new Set(['.DS_Store', 'settings.local.json']);
const BRAND_NOUNS = 'agents|config|configs|delivery|entries|hooks?|implementation|package|packages|prefix|registrations|setup|skills?|source|target';
const VALID_PREFIX = /^[a-z][a-z0-9]*$/;

function usage() {
  console.log('Usage: node sync-<prefix>.mjs --target <project> [--source <project>] [--apply]\n\n'
    + 'Dry-run is the default. --apply adds missing files and Codex skill links only;\n'
    + 'existing differing files are reported as conflicts and are never overwritten.\n'
    + 'The target prefix is the first two letters of a one-word project name, or the first\n'
    + 'letter of every word, lowercased (document-knowledge-platform -> dkp).');
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function sourcePrefix() {
  const script = fileURLToPath(import.meta.url);
  const name = path.basename(path.resolve(path.dirname(script), '..'));
  const prefix = name.endsWith('.sync')
    ? name.slice(0, -'.sync'.length)
    : (path.basename(script).match(/^sync-(.+)\.test\.mjs$/) ?? path.basename(script).match(/^sync-(.+)\.mjs$/))?.[1];
  if (!prefix || !VALID_PREFIX.test(prefix)) {
    throw new Error(`cannot derive source prefix from skill directory or script name: ${name}`);
  }
  return prefix;
}

function targetPrefix(target) {
  const name = path.basename(target).replace(/\.git$/i, '');
  const words = name
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .split(/[^A-Za-z0-9]+/)
    .filter(Boolean);
  if (!words.length) throw new Error(`cannot derive a prefix from target project name: ${name}`);
  const prefix = (words.length === 1 ? words[0].slice(0, 2) : words.map((word) => word[0]).join('')).toLowerCase();
  if (!VALID_PREFIX.test(prefix)) {
    throw new Error(`derived prefix "${prefix}" is invalid; rename the target directory to start with a letter`);
  }
  return prefix;
}

function lstat(file) {
  try {
    return fs.lstatSync(file);
  } catch (error) {
    if (error.code === 'ENOENT') return null;
    throw error;
  }
}

function assertDirectory(directory, label) {
  const stat = lstat(directory);
  if (!stat?.isDirectory()) throw new Error(`${label} is not a directory: ${directory}`);
}

function portText(text, fromPrefix, toPrefix) {
  if (fromPrefix === toPrefix) return text;
  const token = escapeRegExp(fromPrefix);
  const ported = text
    .replace(new RegExp(`(?<![A-Za-z0-9])${token}(?=\\\\?[._@-])`, 'g'), toPrefix)
    .replace(new RegExp(`(?<![A-Za-z0-9])${token}(?= (?:${BRAND_NOUNS})(?![A-Za-z0-9]))`, 'g'), toPrefix);
  return ported.replace(new RegExp(`^(# )${escapeRegExp(fromPrefix.toUpperCase())}(?= )`, 'gm'), `$1${toPrefix.toUpperCase()}`);
}

function portPart(part, fromPrefix, toPrefix) {
  if (part.startsWith(`${fromPrefix}.`) || part.startsWith(`${fromPrefix}-`)) {
    return toPrefix + part.slice(fromPrefix.length);
  }
  return part.replace(`sync-${fromPrefix}.`, `sync-${toPrefix}.`);
}

function portRelative(relative, fromPrefix, toPrefix) {
  return relative.split(path.sep).map((part) => portPart(part, fromPrefix, toPrefix)).join(path.sep);
}

function portedBuffer(file, fromPrefix, toPrefix) {
  const buffer = fs.readFileSync(file);
  if (buffer.includes(0)) return buffer;
  return Buffer.from(portText(buffer.toString('utf8'), fromPrefix, toPrefix));
}

function walk(directory, relative, files, config) {
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    if (EXCLUDED_DIRS.has(entry.name) || EXCLUDED_FILES.has(entry.name) || entry.name.endsWith('.g1hook')) {
      continue;
    }
    const absolute = path.join(directory, entry.name);
    const rel = path.join(relative, entry.name);
    if (rel === config.lessons) continue;
    if (entry.isDirectory()) {
      walk(absolute, rel, files, config);
    } else if (entry.isFile()) {
      files.push({ sourceRelative: rel, targetRelative: portRelative(rel, config.fromPrefix, config.toPrefix) });
    } else {
      throw new Error(`unsupported symlink in canonical package: ${rel}`);
    }
  }
}

function inventory(config) {
  const skillsRoot = path.join(config.source, '.claude/skills');
  assertDirectory(skillsRoot, 'source skills root');
  const skillPattern = new RegExp(`^${escapeRegExp(config.fromPrefix)}[.-]`);
  const files = [];
  const skills = fs.readdirSync(skillsRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && skillPattern.test(entry.name))
    .map((entry) => ({
      sourceName: entry.name,
      targetName: portPart(entry.name, config.fromPrefix, config.toPrefix),
    }))
    .sort((a, b) => a.sourceName.localeCompare(b.sourceName));
  for (const skill of skills) {
    walk(path.join(skillsRoot, skill.sourceName), path.join('.claude/skills', skill.sourceName), files, config);
  }
  if (skills.some((skill) => skill.sourceName === `${config.fromPrefix}.loop`)) {
    if (!lstat(path.join(config.source, config.lessonsTemplate))?.isFile()) {
      throw new Error(`missing lessons template: ${config.lessonsTemplate}`);
    }
    files.push({
      sourceRelative: config.lessons,
      targetRelative: portRelative(config.lessons, config.fromPrefix, config.toPrefix),
    });
  }

  const agentsRoot = path.join(config.source, '.claude/agents');
  const agentPattern = new RegExp(`^${escapeRegExp(config.fromPrefix)}-.*\\.md$`);
  if (lstat(agentsRoot)?.isDirectory()) {
    for (const entry of fs.readdirSync(agentsRoot, { withFileTypes: true })) {
      if (entry.isFile() && agentPattern.test(entry.name)) {
        const rel = path.join('.claude/agents', entry.name);
        files.push({ sourceRelative: rel, targetRelative: portRelative(rel, config.fromPrefix, config.toPrefix) });
      }
    }
  }
  for (const file of config.hookFiles) {
    if (lstat(path.join(config.source, file))?.isFile()) {
      files.push({ sourceRelative: file, targetRelative: portRelative(file, config.fromPrefix, config.toPrefix) });
    }
  }
  const targetFiles = files.map((file) => file.targetRelative);
  if (new Set(targetFiles).size !== targetFiles.length) throw new Error('prefix mapping produced duplicate target paths');
  return {
    skills,
    files: files.sort((a, b) => a.targetRelative.localeCompare(b.targetRelative)),
  };
}

function sourcePath(config, item) {
  return path.join(config.source, config.overrides.get(item.sourceRelative) ?? item.sourceRelative);
}

function assertSafeDestination(target, relative) {
  const parts = path.dirname(relative).split(path.sep).filter((part) => part !== '.');
  let current = target;
  for (const part of parts) {
    current = path.join(current, part);
    const stat = lstat(current);
    if (stat?.isSymbolicLink()) {
      throw new Error(`destination ancestor is a symlink: ${path.relative(target, current)}`);
    }
    if (stat && !stat.isDirectory()) {
      throw new Error(`destination ancestor is not a directory: ${path.relative(target, current)}`);
    }
  }
}

function fileAction(config, item) {
  assertSafeDestination(config.target, item.targetRelative);
  const desired = portedBuffer(sourcePath(config, item), config.fromPrefix, config.toPrefix);
  const to = path.join(config.target, item.targetRelative);
  const targetStat = lstat(to);
  if (!targetStat) return 'add';
  if (!targetStat.isFile()) return 'conflict';
  return desired.equals(fs.readFileSync(to)) ? 'unchanged' : 'conflict';
}

function linkAction(config, skill) {
  const relative = path.join('.agents/skills', skill.targetName);
  assertSafeDestination(config.target, relative);
  const to = path.join(config.target, relative);
  const desired = path.join('../..', '.claude/skills', skill.targetName);
  const targetStat = lstat(to);
  if (!targetStat) return { relative, desired, action: 'add' };
  if (targetStat.isSymbolicLink() && fs.readlinkSync(to) === desired) {
    return { relative, desired, action: 'unchanged' };
  }
  return { relative, desired, action: 'conflict' };
}

function installFile(config, item) {
  const desired = portedBuffer(sourcePath(config, item), config.fromPrefix, config.toPrefix);
  const to = path.join(config.target, item.targetRelative);
  fs.mkdirSync(path.dirname(to), { recursive: true });
  fs.writeFileSync(to, desired);
  fs.chmodSync(to, fs.statSync(sourcePath(config, item)).mode & 0o777);
}

function installLink(target, link) {
  const to = path.join(target, link.relative);
  fs.mkdirSync(path.dirname(to), { recursive: true });
  fs.symlinkSync(link.desired, to);
}

function main() {
  const scriptRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..');
  const { values: args } = parseArgs({
    options: {
      source: { type: 'string' },
      target: { type: 'string' },
      apply: { type: 'boolean', default: false },
      help: { type: 'boolean', default: false },
    },
  });
  if (args.help) {
    usage();
    return 0;
  }
  if (!args.target) {
    usage();
    return 2;
  }
  const fromPrefix = sourcePrefix();
  const source = fs.realpathSync(path.resolve(args.source ?? scriptRoot));
  const target = fs.realpathSync(path.resolve(args.target));
  assertDirectory(source, 'source');
  assertDirectory(target, 'target');
  if (source === target) throw new Error('source and target must be different projects');
  const toPrefix = targetPrefix(target);
  const lessons = path.join('.claude/skills', `${fromPrefix}.loop/lessons.md`);
  const config = {
    fromPrefix,
    toPrefix,
    source,
    target,
    lessons,
    lessonsTemplate: path.join('.claude/skills', `${fromPrefix}.sync/assets/lessons.template.md`),
    overrides: new Map([[lessons, path.join('.claude/skills', `${fromPrefix}.sync/assets/lessons.template.md`)]]),
    hookFiles: [
      '.claude/hooks/path-guard.py',
      '.codex/hooks.json',
      `.github/hooks/${fromPrefix}-loop.json`,
    ],
  };

  const { skills, files } = inventory(config);
  if (!skills.length) throw new Error(`no ${fromPrefix} skills found in ${source}`);
  const actions = [
    ...files.map((item) => ({ ...item, relative: item.targetRelative, action: fileAction(config, item), file: true })),
    ...skills.map((skill) => ({ ...linkAction(config, skill), file: false })),
  ];
  for (const item of actions) {
    console.log(`${item.action.padEnd(9)} ${item.relative}`);
    if (args.apply && item.action === 'add') {
      if (item.file) installFile(config, item);
      else installLink(target, item);
    }
  }
  const counts = actions.reduce((result, item) => {
    result[item.action] += 1;
    return result;
  }, { add: 0, unchanged: 0, conflict: 0 });
  const status = counts.conflict ? 'CONFLICTS' : args.apply ? 'OK' : 'DRY_RUN';
  console.log(`SYNC=${status} prefix=${toPrefix} skills=${skills.length} add=${counts.add} unchanged=${counts.unchanged} conflicts=${counts.conflict}`);
  console.log(`Next: adapt stack-dependent content with ${toPrefix}.sync; review and merge every conflict manually.`);
  return counts.conflict ? 2 : 0;
}

try {
  process.exitCode = main();
} catch (error) {
  console.error(`SYNC=ERROR ${error.message}`);
  process.exitCode = 1;
}
