import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, readdir, readFile, realpath, rename, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

const WORKER = '10000000-0000-4000-8000-000000000001';
const COORDINATOR = '10000000-0000-4000-8000-000000000002';
const REQUEST = '20000000-0000-4000-8000-000000000001';
const DISPATCH = '20000000-0000-4000-8000-000000000002';
const REPLY = '20000000-0000-4000-8000-000000000003';
const TASK = '30000000-0000-4000-8000-000000000001';
const RECEIPT = '40000000-0000-4000-8000-000000000001';
const TIME = '2026-01-01T00:00:00.000Z';
const UNTRUSTED = 'synthetic message content outside the notification';

function registration(overrides = {}) {
  return {
    id: REQUEST, type: 'registration_request', created_at: TIME,
    from_project: 'orchestrator', to_project: 'sample', intended_endpoint: WORKER,
    subject: UNTRUSTED, body: UNTRUSTED, ...overrides,
  };
}

function dispatch(overrides = {}) {
  return {
    schema_version: 2, id: DISPATCH, type: 'execution_request', created_at: TIME,
    sender: 'orchestrator', recipient: 'sample', project: 'sample', intended_endpoint: WORKER,
    task_id: TASK, task_path: `tasks/sample/${TASK}.json`,
    receipt_path: `dispatch-gates/${TASK}/${RECEIPT}.json`, body: UNTRUSTED, ...overrides,
  };
}

function result(overrides = {}) {
  return {
    id: REPLY, created_at: TIME, in_reply_to: DISPATCH,
    from_project: 'sample', to_project: 'orchestrator', status: 'done', body: UNTRUSTED,
    ...overrides,
  };
}

async function publishReceipt(box, envelope = dispatch(), overrides = {}) {
  const canonical = JSON.stringify(envelope, Object.keys(envelope).sort());
  const receipt = {
    id: RECEIPT, schema_version: 2, operation: 'dispatch', allowed: true, root: box.root,
    project: 'sample', endpoint: WORKER, created_at: TIME, task_id: TASK,
    task_path: `tasks/sample/${TASK}.json`, message_id: DISPATCH, audit_path: `dispatch-gates/${TASK}/${RECEIPT}.json`,
    message_path: `inbox/sample/${DISPATCH}.json`, message_hash: createHash('sha256').update(canonical).digest('hex'),
    fingerprint: '0'.repeat(64), worker_fingerprint: 'a'.repeat(64), prerequisites: [], reasons: [], ...overrides,
  };
  await mkdir(join(box.root, 'dispatch-gates', TASK), { recursive: true });
  const path = join(box.root, 'dispatch-gates', TASK, `${RECEIPT}.json`);
  await writeFile(path, JSON.stringify(receipt));
  return path;
}

async function mailbox(t) {
  const directory = await realpath(await mkdtemp(join(tmpdir(), 'copilot-mailbox-test-')));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const root = join(directory, 'mailbox');
  const cwd = join(directory, 'checkout');
  await Promise.all([cwd, join(root, 'outbox', 'sample'), ...['sample', 'orchestrator'].map(route => join(root, 'inbox', route))]
    .map(path => mkdir(path, { recursive: true })));
  await writeFile(join(root, 'projects.json'), JSON.stringify({
    projects: [{ id: 'sample', repository_path: cwd, working_directory: cwd }],
  }));
  async function publish(route, message, filename = `${message.id}.json`) {
    const path = join(root, 'inbox', route, filename);
    await writeFile(path, JSON.stringify(message));
    return path;
  }
  return { root, cwd, publish };
}

async function relay(box, options = {}) {
  const { createRelay } = await import('../../workspace/copilot-mailbox/extensions/mailbox/transport.mjs');
  const notifications = [];
  const receiver = await createRelay({
    root: box.root, cwd: box.cwd, route: 'sample', sessionId: WORKER,
    startedAt: Date.parse(TIME), send: async notification => notifications.push(notification), ...options,
  });
  return { receiver, notifications };
}

function assertPointer(notifications, route, id) {
  assert.equal(notifications.length, 1);
  const [{ prompt, mode }] = notifications;
  assert.equal(mode, 'enqueue');
  assert.ok(prompt.includes(`inbox/${route}/${id}.json`), 'must point to the published envelope');
  assert.ok(!prompt.includes(UNTRUSTED), 'message content must stay out of the prompt');
}

test('a worker receives a pending registration at startup and a later dispatch once each', async t => {
  const box = await mailbox(t);
  await box.publish('sample', registration());
  const { receiver, notifications } = await relay(box);
  await receiver.poll();
  assertPointer(notifications, 'sample', REQUEST);
  await receiver.poll();
  assert.equal(notifications.length, 1);
  await publishReceipt(box);
  const path = await box.publish('sample', dispatch());
  await receiver.poll();
  assertPointer(notifications.slice(1), 'sample', DISPATCH);
  assert.deepEqual(JSON.parse(await readFile(path, 'utf8')), dispatch());
});

test('two workers sharing a project inbox receive only their own addressed messages', async t => {
  const box = await mailbox(t);
  const secondId = '10000000-0000-4000-8000-000000000003';
  const first = await relay(box);
  const second = await relay(box, { sessionId: secondId });
  const assignment = dispatch({ intended_endpoint: secondId });
  await publishReceipt(box, assignment, { endpoint: secondId });
  await box.publish('sample', assignment);
  await box.publish('sample', registration());
  await Promise.all([first.receiver.poll(), second.receiver.poll()]);
  assertPointer(first.notifications, 'sample', REQUEST);
  assertPointer(second.notifications, 'sample', DISPATCH);
});

test('a reopened relay recovers a pending envelope, while an archived envelope stays processed', async t => {
  const box = await mailbox(t);
  const path = await box.publish('sample', registration());
  const first = await relay(box);
  await first.receiver.poll();
  assertPointer(first.notifications, 'sample', REQUEST);
  const resumed = await relay(box);
  await resumed.receiver.poll();
  assertPointer(resumed.notifications, 'sample', REQUEST);
  await rm(path);
  const next = await relay(box);
  await next.receiver.poll();
  assert.deepEqual(next.notifications, []);
});

test('the coordinator receives the unaddressed-to-session reply in its separate inbox', async t => {
  const box = await mailbox(t);
  await box.publish('orchestrator', result());
  await box.publish('sample', dispatch());
  const { receiver, notifications } = await relay(box, {
    cwd: box.root, route: 'orchestrator', sessionId: COORDINATOR,
  });
  await receiver.poll();
  assertPointer(notifications, 'orchestrator', REPLY);
  await receiver.poll();
  assert.equal(notifications.length, 1);
});

test('the worker rejects unaddressed and contradictory routing, even in its own inbox', async t => {
  const box = await mailbox(t);
  const { receiver, notifications } = await relay(box);
  for (const input of [
    dispatch({ intended_endpoint: COORDINATOR }),
    dispatch({ intended_endpoint: undefined }),
    dispatch({ recipient: 'another-project' }),
    dispatch({ project: 'another-project' }),
    registration({ to_project: 'another-project' }),
    registration({ intended_endpoint: COORDINATOR }),
    registration({ intended_endpoint: undefined }),
    registration({ to: { project: 'sample', session_id: COORDINATOR } }),
    registration({ to: { project: 'another-project', session_id: WORKER } }),
    registration({ to_endpoint: COORDINATOR }),
    registration({ to_session_id: COORDINATOR }),
  ]) {
    await box.publish('sample', input);
    await receiver.poll();
    assert.deepEqual(notifications, [], JSON.stringify(input));
  }
  await box.publish('sample', registration({ intended_endpoint: undefined, to_endpoint: WORKER }));
  await receiver.poll();
  assertPointer(notifications, 'sample', REQUEST);
});

test('nested recipient and session fields route to the intended worker', async t => {
  const box = await mailbox(t);
  await box.publish('sample', registration({ intended_endpoint: undefined, to_project: undefined,
    to: { project: 'sample', session_id: WORKER } }));
  const { receiver, notifications } = await relay(box);
  await receiver.poll();
  assertPointer(notifications, 'sample', REQUEST);
});

test('the coordinator respects an endpoint on nested reply envelopes', async t => {
  const box = await mailbox(t);
  const { receiver, notifications } = await relay(box, {
    cwd: box.root, route: 'orchestrator', sessionId: COORDINATOR,
  });
  const nested = result({ to_project: undefined, to: { role: 'orchestrator', session_id: WORKER } });
  await box.publish('orchestrator', nested);
  await receiver.poll();
  assert.deepEqual(notifications, []);
  nested.to.session_id = COORDINATOR;
  await box.publish('orchestrator', nested);
  await receiver.poll();
  assertPointer(notifications, 'orchestrator', REPLY);
});

test('malformed, oversized, symlinked and temporary records cannot wake the worker', async t => {
  const box = await mailbox(t);
  const { receiver, notifications } = await relay(box);
  const inbox = join(box.root, 'inbox', 'sample');
  const outside = join(box.cwd, 'external.json');
  await writeFile(outside, JSON.stringify(registration()));
  const path = join(inbox, `${REQUEST}.json`);
  await symlink(outside, path);
  await receiver.poll();
  assert.deepEqual(notifications, []);
  await rm(path);
  await box.publish('sample', registration(), 'pending.tmp');
  await box.publish('sample', registration(), `${DISPATCH}.json`);
  await box.publish('sample', registration(), 'message.json');
  for (const contents of ['{', 'null', '[]', JSON.stringify(registration({ body: 'x'.repeat(4 * 1024 * 1024) }))]) {
    await writeFile(path, contents);
    await receiver.poll();
    assert.deepEqual(notifications, []);
  }
  await box.publish('sample', registration());
  await receiver.poll();
  assertPointer(notifications, 'sample', REQUEST);
});

test('routes and identities bind to the registered directory before notifications begin', async t => {
  const box = await mailbox(t);
  for (const options of [
    { route: '../sample' }, { route: 'missing' }, { cwd: box.root },
    { cwd: box.cwd, route: 'orchestrator' }, { sessionId: '../elsewhere' },
  ]) {
    await assert.rejects(relay(box, options));
  }
});

test('an SDK delivery failure can be retried and its raw message stays private', async t => {
  const box = await mailbox(t);
  await box.publish('sample', registration());
  const accepted = [];
  let failed = false;
  const { receiver } = await relay(box, { send: async message => {
    if (!failed) {
      failed = true;
      throw new Error(UNTRUSTED);
    }
    accepted.push(message);
  } });
  await assert.rejects(receiver.poll(), error => !error.message.includes(UNTRUSTED));
  await receiver.poll();
  assertPointer(accepted, 'sample', REQUEST);
  await receiver.poll();
  assert.equal(accepted.length, 1);
});

test('overlapping inbox scans enqueue a pending envelope only once', async t => {
  const box = await mailbox(t);
  await box.publish('sample', registration());
  let accept, started;
  const start = new Promise(resolve => { started = resolve; });
  const release = new Promise(resolve => { accept = resolve; });
  const accepted = [];
  const { receiver } = await relay(box, { send: async notification => {
    started();
    await release;
    accepted.push(notification);
  } });
  const first = receiver.poll();
  await start;
  const second = receiver.poll();
  accept();
  await Promise.all([first, second]);
  assertPointer(accepted, 'sample', REQUEST);
});

test('a coordinator wakes for a worker discovery offer left while it was offline', async t => {
  const box = await mailbox(t);
  const { receiver, notifications } = await relay(box, {
    cwd: box.root, route: 'orchestrator', sessionId: COORDINATOR, startedAt: Date.parse(TIME) + 1,
  });
  const offer = { id: REQUEST, type: 'registration_offer', executable: false, created_at: TIME,
    to: { role: 'orchestrator' }, from: { project: 'sample', session_id: WORKER } };
  await box.publish('orchestrator', { ...offer, from: { project: 'unregistered', session_id: WORKER } });
  await receiver.poll();
  assert.deepEqual(notifications, []);
  await box.publish('orchestrator', offer);
  await receiver.poll();
  assertPointer(notifications, 'orchestrator', REQUEST);
});

test('an offline coordinator recovers its own correlated reply and leaves other sessions alone', async t => {
  const box = await mailbox(t);
  const { receiver, notifications } = await relay(box, {
    cwd: box.root, route: 'orchestrator', sessionId: COORDINATOR, startedAt: Date.parse(TIME) + 1,
  });
  await box.publish('orchestrator', result({ in_reply_to: REQUEST }));
  await receiver.poll();
  assert.deepEqual(notifications, []);
  const archive = join(box.root, 'archive', 'sample');
  await mkdir(archive, { recursive: true });
  const original = join(archive, `${REQUEST}.json`);
  await writeFile(original, JSON.stringify(registration({ from_session_id: WORKER })));
  await receiver.poll();
  assert.deepEqual(notifications, []);
  await writeFile(original, JSON.stringify(registration({ from_session_id: COORDINATOR })));
  await receiver.poll();
  assertPointer(notifications, 'orchestrator', REPLY);
});

test('the receive loop retries after an SDK error and stops when its session closes', async t => {
  const box = await mailbox(t);
  await box.publish('sample', registration());
  const stop = new AbortController();
  const warnings = [];
  const accepted = [];
  let attempts = 0;
  const { receiver } = await relay(box, { send: async notification => {
    if (++attempts === 1) throw new Error(UNTRUSTED);
    accepted.push(notification);
    stop.abort();
  } });
  const guard = setTimeout(() => stop.abort(), 5000);
  t.after(() => clearTimeout(guard));
  await receiver.run({ signal: stop.signal, intervalMs: 1, warn: message => warnings.push(message) });
  assertPointer(accepted, 'sample', REQUEST);
  assert.equal(attempts, 2);
  assert.equal(warnings.length, 1);
  assert.ok(!JSON.stringify(warnings).includes(UNTRUSTED));
});

test('only a canonical dispatch receipt pointer is eligible for an execution notification', async t => {
  const box = await mailbox(t);
  const { receiver, notifications } = await relay(box);
  for (const message of [
    dispatch({ receipt_path: `dispatch-gates/${REQUEST}/${RECEIPT}.json` }),
    dispatch({ receipt_path: `dispatch-gates/${TASK}/../${RECEIPT}.json` }),
    dispatch({ receipt_path: ['invalid'] }), dispatch({ task_id: 'no-uuid' }),
    dispatch({ task_path: `tasks/elsewhere/${TASK}.json` }), dispatch({ sender: 'sample' }),
    registration({ type: 'other', executable: true }), registration({ in_reply_to: 'invalid' }),
    registration({ created_at: '2026-01-01T00:00:00' }), registration({ created_at: 'badZ' }),
  ]) {
    await box.publish('sample', message);
    await receiver.poll();
    assert.deepEqual(notifications, [], JSON.stringify(message));
  }
});

test('malformed registry entries and symlinked inbox directories fail with fixed errors', async t => {
  const box = await mailbox(t);
  const { receiver } = await relay(box);
  const inbox = join(box.root, 'inbox', 'sample');
  await rm(inbox, { recursive: true });
  await writeFile(join(box.cwd, `${REQUEST}.json`), JSON.stringify(registration()));
  await symlink(box.cwd, inbox);
  await assert.rejects(receiver.poll(), error => !error.message.includes(box.cwd));
  for (const invalid of [{ projects: [{ id: 'sample' }, { id: 'sample' }] }, { projects: { '../x': {} } }]) {
    await writeFile(join(box.root, 'projects.json'), JSON.stringify(invalid));
    await assert.rejects(relay(box), error => !error.message.includes(box.root));
  }
});

test('the installed entrypoint joins its session, uses its SDK identity and rejects a wrong working directory', async t => {
  const box = await mailbox(t);
  await box.publish('sample', registration());
  const sdk = `export async function joinSession() {
    return { sessionId: process.env.TEST_SDK_SESSION, send: async notification => {
      process.stdout.write(JSON.stringify(notification) + '\\n', () => process.exit(0));
    } };
  }`;
  const asModule = source => `data:text/javascript,${encodeURIComponent(source)}`;
  const testLoader = asModule(`export function resolve(id, context, next) {
    if (id === '@microsoft/copilot-sdk/extension') return { shortCircuit: true, url: ${JSON.stringify(asModule(sdk))} };
    return next(id, context);
  }`);
  const preload = asModule(`import {register} from 'node:module'; register(${JSON.stringify(testLoader)});`);
  const entrypoint = fileURLToPath(new URL('../../workspace/copilot-mailbox/extensions/mailbox/extension.mjs', import.meta.url));
  const args = ['--import', preload, entrypoint];
  const env = { ...process.env, HARNESS_MAILBOX_ROOT: box.root, HARNESS_MAILBOX_CWD: box.cwd,
    HARNESS_MAILBOX_ROUTE: 'sample', TEST_SDK_SESSION: WORKER };
  const execute = promisify(execFile);
  const { stdout } = await execute(process.execPath, args, { cwd: box.cwd, env, timeout: 5000 });
  assertPointer([JSON.parse(stdout)], 'sample', REQUEST);
  const offers = await readdir(join(box.root, 'outbox', 'sample'));
  assert.equal(offers.length, 1);
  const offerPath = join(box.root, 'inbox', 'orchestrator', offers[0]);
  const offer = JSON.parse(await readFile(offerPath, 'utf8'));
  assert.equal(offer.from.session_id, WORKER);
  await rm(offerPath);
  await mkdir(join(box.root, 'outbox', 'orchestrator'));
  await box.publish('orchestrator', result({ to_session_id: COORDINATOR }));
  const coordinatorEnv = { ...env, HARNESS_MAILBOX_ROUTE: 'orchestrator', HARNESS_MAILBOX_CWD: box.root,
    TEST_SDK_SESSION: COORDINATOR };
  const accepted = await execute(process.execPath, args, { cwd: box.root, env: coordinatorEnv, timeout: 5000 });
  assertPointer([JSON.parse(accepted.stdout)], 'orchestrator', REPLY);
  assert.deepEqual(await readdir(join(box.root, 'outbox', 'orchestrator')), []);
  assert.deepEqual(await readdir(join(box.root, 'inbox', 'orchestrator')), [`${REPLY}.json`]);

  await assert.rejects(execute(process.execPath, args, { cwd: box.root, env, timeout: 5000 }), error => {
    assert.equal(error.code, 1);
    assert.match(error.stderr, /Copilot mailbox notifications could not start/);
    assert.ok(!error.stderr.includes(box.root));
    return true;
  });
});

test('a uniquely registered mixed-case project with dots keeps working alongside ambiguous entries', async t => {
  const box = await mailbox(t);
  const route = 'Sample.v2';
  await mkdir(join(box.root, 'inbox', route));
  const project = { id: route, working_directory: box.cwd };
  await writeFile(join(box.root, 'projects.json'), JSON.stringify({ projects: [
    null, { id: '../invalid' }, { id: 'other' }, { id: 'other' }, project,
  ] }));
  await box.publish(route, registration({ to_project: route }));
  const { receiver, notifications } = await relay(box, { route });
  await receiver.poll();
  assertPointer(notifications, route, REQUEST);
});

test('array values that stringify to a UUID do not authorize a notification', async t => {
  const box = await mailbox(t);
  const worker = await relay(box);
  const coordinator = await relay(box, { route: 'orchestrator', cwd: box.root, sessionId: COORDINATOR });
  await box.publish('sample', dispatch({ task_id: [TASK] }));
  await box.publish('orchestrator', result({ in_reply_to: [DISPATCH] }));
  await box.publish('orchestrator', { id: REQUEST, type: 'registration_offer', created_at: TIME,
    executable: false, from: { project: 'sample', session_id: [WORKER] }, to: { role: 'orchestrator' } });
  await worker.receiver.poll();
  await coordinator.receiver.poll();
  assert.deepEqual(worker.notifications, []);
  assert.deepEqual(coordinator.notifications, []);
  await assert.rejects(relay(box, { sessionId: [WORKER] }));
});

test('a duplicate inbox copy of an archived request never produces a new notification', async t => {
  const box = await mailbox(t);
  const request = registration();
  const archive = join(box.root, 'archive', 'sample');
  await mkdir(archive, { recursive: true });
  await writeFile(join(archive, `${REQUEST}.json`), JSON.stringify(request));
  const copy = await box.publish('sample', request);
  const { receiver, notifications } = await relay(box);
  await receiver.poll();
  assert.deepEqual(notifications, []);
  assert.deepEqual(JSON.parse(await readFile(copy, 'utf8')), request);
});

test('a structural dispatch alone stays pending until a matching verified dispatch receipt exists', async t => {
  const box = await mailbox(t);
  const envelope = dispatch();
  await box.publish('sample', envelope);
  const { receiver, notifications } = await relay(box);
  await receiver.poll();
  assert.deepEqual(notifications, []);
  for (const invalid of [{ operation: 'check' }, { allowed: false }, { endpoint: COORDINATOR },
    { message_hash: '0'.repeat(64) }, { message_id: REQUEST }]) {
    await publishReceipt(box, envelope, invalid);
    await receiver.poll();
    assert.deepEqual(notifications, [], JSON.stringify(invalid));
  }
  await publishReceipt(box, envelope);
  await box.publish('sample', dispatch({ body: 'changed after gate publication' }));
  await receiver.poll();
  assert.deepEqual(notifications, []);
  await box.publish('sample', envelope);
  await receiver.poll();
  assertPointer(notifications, 'sample', DISPATCH);
});

test('a flat session recipient on a coordinator reply takes precedence over freshness', async t => {
  const box = await mailbox(t);
  const { receiver, notifications } = await relay(box, {
    route: 'orchestrator', cwd: box.root, sessionId: COORDINATOR,
  });
  await box.publish('orchestrator', result({ to_session_id: WORKER }));
  await receiver.poll();
  assert.deepEqual(notifications, []);
  await box.publish('orchestrator', result({ to_session_id: COORDINATOR }));
  await receiver.poll();
  assertPointer(notifications, 'orchestrator', REPLY);
});

async function offerPublisher(box, options = {}) {
  const { createOfferPublisher } = await import('../../workspace/copilot-mailbox/extensions/mailbox/transport.mjs');
  return createOfferPublisher({ root: box.root, cwd: box.cwd, route: 'sample', sessionId: WORKER,
    createdAt: TIME, ...options });
}

test('the worker publishes one byte-identical UUID offer naming its actual SDK session in both mailboxes', async t => {
  const box = await mailbox(t);
  const publish = await offerPublisher(box);
  const id = await publish();
  assert.match(id, /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/);
  assert.equal(await publish(), id);
  const bytes = [];
  for (const directory of [join(box.root, 'outbox', 'sample'), join(box.root, 'inbox', 'orchestrator')]) {
    assert.deepEqual(await readdir(directory), [`${id}.json`]);
    bytes.push(await readFile(join(directory, `${id}.json`)));
  }
  assert.deepEqual(bytes[0], bytes[1]);
  const offer = JSON.parse(bytes[0]);
  assert.equal(offer.id, id);
  assert.equal(offer.type, 'registration_offer');
  assert.equal(offer.executable, false);
  assert.equal(offer.created_at, TIME);
  assert.equal(offer.from.session_id, WORKER);
  assert.equal(offer.from.project, 'sample');
  assert.equal(offer.to.role, 'orchestrator');
  assert.equal(offer.working_directory, box.cwd);
});

test('a conflicting destination is preserved and the same worker publication repairs a partial delivery', async t => {
  const box = await mailbox(t);
  const publish = await offerPublisher(box, { id: REQUEST });
  const outbox = join(box.root, 'outbox', 'sample');
  const inbox = join(box.root, 'inbox', 'orchestrator');
  const name = `${REQUEST}.json`;
  await writeFile(join(inbox, name), 'existing immutable record');
  await assert.rejects(publish(), error => !error.message.includes('existing immutable record'));
  assert.equal(await readFile(join(inbox, name), 'utf8'), 'existing immutable record');
  const firstBytes = await readFile(join(outbox, name));
  assert.deepEqual(await readdir(outbox), [name]);
  assert.deepEqual(await readdir(inbox), [name]);
  await rm(join(inbox, name));
  assert.equal(await publish(), REQUEST);
  assert.deepEqual(await readFile(join(inbox, name)), firstBytes);
  assert.deepEqual(await readFile(join(outbox, name)), firstBytes);
});

test('a symlinked publication directory cannot write outside the mailbox', async t => {
  const box = await mailbox(t);
  const publish = await offerPublisher(box, { id: REQUEST });
  const outbox = join(box.root, 'outbox', 'sample');
  const preserved = join(box.root, 'saved-outbox');
  await rename(outbox, preserved);
  await symlink(box.cwd, outbox);
  await assert.rejects(publish(), error => !error.message.includes(box.cwd));
  assert.deepEqual(await readdir(box.cwd), []);
  assert.deepEqual(await readdir(join(box.root, 'inbox', 'orchestrator')), []);
  await rm(outbox);
  await rename(preserved, outbox);
  assert.equal(await publish(), REQUEST);
});

test('a running coordinator checks pending offers against the current project registry', async t => {
  const box = await mailbox(t);
  const { receiver, notifications } = await relay(box, { route: 'orchestrator', cwd: box.root, sessionId: COORDINATOR });
  const registry = join(box.root, 'projects.json');
  const offer = { id: REQUEST, type: 'registration_offer', executable: false, created_at: TIME,
    from: { project: 'new-app', session_id: WORKER }, to: { role: 'orchestrator' } };
  await box.publish('orchestrator', offer);
  await receiver.poll();
  assert.deepEqual(notifications, []);

  await writeFile(registry, JSON.stringify({ projects: [{ id: 'new-app', working_directory: box.cwd }] }));
  await receiver.poll();
  assertPointer(notifications, 'orchestrator', REQUEST);

  await box.publish('orchestrator', { ...offer, id: REPLY, from: { project: 'sample', session_id: WORKER } });
  await receiver.poll();
  assert.equal(notifications.length, 1);
});

test('a pending worker notification waits until its exact project directory is registered again', async t => {
  const box = await mailbox(t);
  const registry = join(box.root, 'projects.json');
  const original = await readFile(registry);
  const { receiver, notifications } = await relay(box);
  await box.publish('sample', registration());
  await writeFile(registry, JSON.stringify({ projects: [{ id: 'sample', working_directory: box.root }] }));
  await assert.rejects(receiver.poll());
  assert.deepEqual(notifications, []);
  await writeFile(registry, original);
  await receiver.poll();
  assertPointer(notifications, 'sample', REQUEST);
});

test('a case-ambiguous project cannot receive mailbox notifications', async t => {
  const box = await mailbox(t);
  await writeFile(join(box.root, 'projects.json'), JSON.stringify({ projects: [
    { id: 'Sample', working_directory: box.root }, { id: 'sample', working_directory: box.cwd },
  ] }));
  await assert.rejects(relay(box));
});

test('a failed discovery offer does not suppress a pending addressed notification', async t => {
  const box = await mailbox(t);
  await box.publish('sample', registration());
  const stop = new AbortController();
  const accepted = [];
  const { receiver } = await relay(box, { send: async notification => {
    accepted.push(notification);
    stop.abort();
  } });
  let offers = 0;
  await receiver.run({ intervalMs: 1, signal: stop.signal, beforePoll() {
    if (++offers === 2) stop.abort();
    throw new Error('temporary publication failure');
  } });
  assertPointer(accepted, 'sample', REQUEST);
  assert.equal(offers, 1);
});

test('a fresh role-only reply stays with the coordinator that sent its request', async t => {
  const box = await mailbox(t);
  const archived = join(box.root, 'archive', 'sample');
  await mkdir(archived, { recursive: true });
  await writeFile(join(archived, REQUEST + '.json'), JSON.stringify(
    registration({ from: { project: 'orchestrator', session_id: COORDINATOR } })));
  await box.publish('orchestrator', result({ in_reply_to: REQUEST }));
  const other = await relay(box, { cwd: box.root, route: 'orchestrator', sessionId: TASK });
  await other.receiver.poll();
  assert.deepEqual(other.notifications, []);
  const owner = await relay(box, { cwd: box.root, route: 'orchestrator', sessionId: COORDINATOR });
  await owner.receiver.poll();
  assertPointer(owner.notifications, 'orchestrator', REPLY);
});

test('a batch rechecks queued records after the preceding notification is delivered', async t => {
  for (const change of ['archive', 'recipient', 'registry']) {
    await t.test(change, async t => {
      const box = await mailbox(t);
      await box.publish('sample', registration());
      const second = registration({ id: REPLY, created_at: '2026-01-02T00:00:00Z' });
      const path = await box.publish('sample', second);
      const notifications = [];
      const { receiver } = await relay(box, { send: async notification => {
        notifications.push(notification);
        if (notifications.length !== 1) return;
        if (change === 'archive') {
          const dir = join(box.root, 'archive', 'sample');
          await mkdir(dir, { recursive: true });
          await rename(path, join(dir, REPLY + '.json'));
        } else if (change === 'recipient') {
          await box.publish('sample', { ...second, intended_endpoint: COORDINATOR });
        } else {
          await writeFile(join(box.root, 'projects.json'), JSON.stringify({ projects: [] }));
        }
      } });
      try {
        await receiver.poll();
      } catch (error) {
        if (change !== 'registry') throw error;
        // Revoking the worker's route can also abort the rest of the poll.
        assert.equal(error.message, 'Copilot could not deliver a mailbox notification. It will retry.');
      }
      assertPointer(notifications, 'sample', REQUEST);
    });
  }
});
