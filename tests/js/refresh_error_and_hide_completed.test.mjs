import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const APP_URL = new URL('../../static/app.js', import.meta.url);
const APP_SOURCE = readFileSync(APP_URL, 'utf8');
const LONG_AGO = '2020-01-01T00:00:00Z';
const MOUNT_HINT = 'Check the mailbox mount and try again.';
const LAST_SNAPSHOT_NOTICE = 'Last successful snapshot is still displayed.';
const NEW_BASELINE_MESSAGE = 'Saved comparison is unavailable or invalid. A new baseline is saved for the next refresh.';

// A small recording DOM: text reaches it only through textContent or text nodes; markup writes are logged.
class StubNode {
  constructor(page, tagName = 'div') {
    this.tagName = tagName;
    this.children = [];
    this.ownText = '';
    this.value = '';
    this.checked = false;
    this.dataset = {};
    this.style = {};
    this.attributes = new Map();
    this.listeners = new Map();
    this.classList = { add() {}, remove() {}, toggle() {}, contains: () => false };
    this.page = page;
  }

  get textContent() { return this.ownText; }
  set textContent(text) { this.ownText = String(text); this.children = []; }
  set innerHTML(markup) { this.page.markupWrites.push(markup); }
  get options() { return this.descendants().filter(node => node.tagName === 'option'); }

  descendants() { return this.children.flatMap(child => [child, ...child.descendants()]); }
  fullText() { return [this.ownText, ...this.children.map(child => child.fullText())].join(' '); }
  append(...items) { items.forEach(item => this.children.push(typeof item === 'string' ? this.page.textNode(item) : item)); }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  prepend(...items) { this.children.unshift(...items); }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name); }
  removeAttribute(name) { this.attributes.delete(name); }
  addEventListener(type, callback) { this.listeners.set(type, callback); }
  focus() {}
  showModal() {}
  close() {}
}

class StubPage {
  constructor() {
    this.markupWrites = [];
    this.byId = new Map();
    this.storage = new Map();
    this.fetchImplementation = () => new Promise(() => {});
    this.sink = new StubNode(this);
    const page = this;
    const document = {
      getElementById: id => page.element(id),
      createElement: tag => new StubNode(page, tag),
      createElementNS: (_namespace, tag) => new StubNode(page, tag),
      createTextNode: text => page.textNode(text),
      querySelector: () => page.sink,
      querySelectorAll: () => [],
      documentElement: page.sink,
      activeElement: page.sink,
    };
    this.context = vm.createContext({
      document,
      window: { addEventListener() {}, scrollTo() {} },
      history: { pushState() {} },
      location: { search: '', hash: '', href: 'http://dashboard.test/' },
      localStorage: {
        getItem: key => (page.storage.has(key) ? page.storage.get(key) : null),
        setItem: (key, value) => page.storage.set(key, String(value)),
      },
      fetch: (...args) => page.fetchImplementation(...args),
      setInterval: () => 0,
      setTimeout: callback => { queueMicrotask(callback); return 0; },
      clearInterval() {},
      clearTimeout() {},
      URL,
      URLSearchParams,
    });
    vm.runInContext(APP_SOURCE, this.context, { filename: APP_URL.href });
  }

  element(id) {
    if (!this.byId.has(id)) this.byId.set(id, new StubNode(this));
    return this.byId.get(id);
  }

  textNode(text) {
    const node = new StubNode(this, '#text');
    node.ownText = String(text);
    return node;
  }

  textOf(id) { return this.element(id).fullText(); }
  respond(response) { this.fetchImplementation = () => Promise.resolve(response); }
  reject(error) { this.fetchImplementation = () => Promise.reject(error); }
  respondWithPayload(payload) { this.respond(jsonResponse(payload)); }
  hideCompleted(on) { this.element('hide-completed').checked = on; }
  search(text) { this.element('search').value = text; }
  refresh() { return this.context.load(); }
  evaluate(code) { return vm.runInContext(code, this.context); }
  get errorLine() { return this.element('error').textContent; }
  get errorIsShown() { return this.element('error').hidden === false; }
}

function jsonResponse(body, ok = true) {
  const text = JSON.stringify(body);
  return { ok, status: ok ? 200 : 500, text: () => Promise.resolve(text), json: () => Promise.resolve(JSON.parse(text)) };
}

function unreadableResponse(ok = true) {
  const failure = () => Promise.reject(new SyntaxError('Unexpected token < in JSON at position 0'));
  return { ok, status: ok ? 200 : 502, text: () => Promise.resolve('<html>gateway</html>'), json: failure };
}

function issue(key, overrides = {}) {
  return {
    key, title: `${key} title`, verified: false, verified_at: null, updated_at: LONG_AGO,
    projects: ['alpha'], epic_keys: [], task_ids: [], teams: [], estimate: null, actual_machine_hours: null,
    source_status: 'Open', local_status: 'pending', recorded_local_status: 'pending',
    verification_reason: 'none', task_execution: { missing: [] }, sources: ['s'], original_estimate_hours: null,
    worked_hours: null, ...overrides,
  };
}

function freeTextTask(id, title, overrides = {}) {
  return {
    id, title, status: 'pending', project: 'alpha', epic_keys: [], epic_key: null, issue_keys: [], mapping_kind: 'unmapped',
    teams: [], blockers: [], unresolved_issue_keys: [], evidenced: false, source: 's', updated_at: LONG_AGO, ...overrides,
  };
}

function taskNode(task, label = task.title) {
  return { id: 'task:' + task.id, kind: 'task', label, status: task.status, mapping_kind: task.mapping_kind, ready: false };
}

function dashboardPayload({ issues = [], tasks = [], graphNodes = [], projects = [{ id: 'alpha', name: 'Alpha' }] } = {}) {
  return {
    mode: 'live', generated_at: '2026-01-01T00:00:00Z', warnings: [], reported_statistics: [], epics: [],
    totals: { scope_complete: true }, estimate_method: { low: 1, likely: 2, high: 3, review_multiplier: 1 },
    projects: projects.map(project => ({ kind: 'project', registered: true, reported_estimate: null, reported_actual_machine_hours: null, ...project })),
    issues, tasks, graph: { nodes: graphNodes, edges: [] },
  };
}

async function pageWithSnapshot(payload = dashboardPayload()) {
  const page = new StubPage();
  page.respondWithPayload(payload);
  await page.refresh();
  return page;
}

const issueNode = completeIssue => ({ id: 'issue:' + completeIssue.key, kind: 'issue', label: completeIssue.key, ready: completeIssue.verified });

test('a rejected fetch shows the fixed server-unreachable reason, not the browser text', async () => {
  const page = new StubPage();
  page.reject(new TypeError('Failed to fetch'));

  await page.refresh();

  assert.equal(page.errorLine, `Could not refresh: the dashboard server could not be reached. ${MOUNT_HINT}`);
  assert.equal(page.errorIsShown, true);
});

test('a rejected fetch after a successful load keeps the last-snapshot suffix', async () => {
  const page = await pageWithSnapshot();
  page.reject(new TypeError('NetworkError when attempting to fetch resource.'));

  await page.refresh();

  assert.equal(page.errorLine, `Could not refresh: the dashboard server could not be reached. ${LAST_SNAPSHOT_NOTICE}`);
});

for (const ok of [true, false]) {
  test(`a ${ok ? 'successful' : 'failing'} response whose body is not JSON hides the parser message`, async () => {
    const page = new StubPage();
    page.respond(unreadableResponse(ok));

    await page.refresh();

    assert.equal(page.errorLine, `Could not refresh: the server response could not be read. ${MOUNT_HINT}`);
    assert.doesNotMatch(page.errorLine, /Unexpected|JSON|position/);
  });
}

test('a non-OK response shows the structured server message', async () => {
  const page = new StubPage();
  page.respond(jsonResponse({ error: { message: 'Mailbox root is not mounted' } }, false));

  await page.refresh();

  assert.equal(page.errorLine, `Could not refresh: Mailbox root is not mounted. ${MOUNT_HINT}`);
});

test('a non-OK response without a structured message shows Unable to read mailbox', async () => {
  const page = new StubPage();
  page.respond(jsonResponse({ detail: 'ignored' }, false));

  await page.refresh();

  assert.equal(page.errorLine, `Could not refresh: Unable to read mailbox. ${MOUNT_HINT}`);
});

test('a non-OK response keeps the last-snapshot suffix after a successful load', async () => {
  const page = await pageWithSnapshot();
  page.respond(jsonResponse({}, false));

  await page.refresh();

  assert.equal(page.errorLine, `Could not refresh: Unable to read mailbox. ${LAST_SNAPSHOT_NOTICE}`);
});

test('a payload that cannot be displayed shows the fixed display reason, not the TypeError text', async () => {
  const page = await pageWithSnapshot();
  page.respondWithPayload({});

  await page.refresh();

  assert.equal(page.errorLine, `Could not refresh: the snapshot could not be displayed. ${LAST_SNAPSHOT_NOTICE}`);
  assert.doesNotMatch(page.errorLine, /undefined|Cannot read|TypeError/);
});

test('protect: a structured server message containing markup is written as text only', async () => {
  const page = new StubPage();
  page.respond(jsonResponse({ error: { message: '<img src=x onerror=alert(1)>' } }, false));

  await page.refresh();

  assert.equal(page.errorLine, `Could not refresh: <img src=x onerror=alert(1)>. ${MOUNT_HINT}`);
  assert.deepEqual(page.markupWrites, []);
});

test('protect: a successful refresh clears a previous error line', async () => {
  const page = new StubPage();
  page.reject(new TypeError('Failed to fetch'));
  await page.refresh();
  assert.equal(page.errorIsShown, true);

  page.respondWithPayload(dashboardPayload());
  await page.refresh();

  assert.equal(page.element('error').hidden, true);
});

test('numeric and null task titles do not abort rendering of the other records', async () => {
  const numeric = freeTextTask('11111111-0000-4000-8000-000000000001', 42);
  const untitled = freeTextTask('22222222-0000-4000-8000-000000000002', null);
  const named = freeTextTask('33333333-0000-4000-8000-000000000003', 'Named work');
  const trailingNumeric = freeTextTask('77777777-0000-4000-8000-000000000007', 7);
  const tasks = [numeric, untitled, named, trailingNumeric];
  const open = issue('ALPHA-1');
  const payload = dashboardPayload({
    issues: [open], tasks,
    graphNodes: [taskNode(numeric), taskNode(untitled, null), taskNode(named), taskNode(trailingNumeric), issueNode(open)],
  });

  const page = await pageWithSnapshot(payload);

  assert.equal(page.element('error').hidden, true);
  assert.match(page.textOf('tasks'), /Named work/);
  assert.match(page.textOf('tasks'), /\b42\b/);
  assert.match(page.textOf('issues'), /ALPHA-1 title/);
  assert.match(page.textOf('map'), /Named work/);
});

test('protect: a task title containing markup is rendered as text without markup writes', async () => {
  const markup = '<img src=x onerror=alert(1)>';
  const task = freeTextTask('44444444-0000-4000-8000-000000000004', markup);

  const page = await pageWithSnapshot(dashboardPayload({ tasks: [task], graphNodes: [taskNode(task)] }));

  assert.ok(page.textOf('tasks').includes(markup));
  assert.deepEqual(page.markupWrites, []);
});

function projectWithSettledIssue() {
  const done = issue('ALPHA-DONE', { verified: true, verified_at: LONG_AGO, local_status: 'verified_done' });
  const open = issue('ALPHA-OPEN');
  return dashboardPayload({ issues: [done, open], graphNodes: [issueNode(done), issueNode(open)] });
}

test('hide completed leaves project-card counts and percentage on the pre-hide scope', async () => {
  const page = new StubPage();
  page.hideCompleted(true);
  page.respondWithPayload(projectWithSettledIssue());

  await page.refresh();

  const card = page.textOf('projects');
  assert.match(card, /2 known issues/);
  assert.match(card, /50% verified/);
});

test('hide completed keeps the card of a project whose work is all completed', async () => {
  const done = issue('ALPHA-DONE', { verified: true, verified_at: LONG_AGO });
  const page = new StubPage();
  page.hideCompleted(true);
  page.search('ALPHA-DONE');
  page.respondWithPayload(dashboardPayload({ issues: [done], graphNodes: [issueNode(done)] }));

  await page.refresh();

  const card = page.textOf('projects');
  assert.match(card, /Alpha/);
  assert.match(card, /1 known issues/);
  assert.match(card, /100% verified/);
});

test('protect: hide completed still hides settled completed issues and tasks in the lists', async () => {
  const done = issue('ALPHA-DONE', { verified: true, verified_at: LONG_AGO });
  const open = issue('ALPHA-OPEN');
  const doneTask = freeTextTask('55555555-0000-4000-8000-000000000005', 'Finished chore', { status: 'done', evidenced: true });
  const openTask = freeTextTask('66666666-0000-4000-8000-000000000006', 'Pending chore');
  const payload = dashboardPayload({ issues: [done, open], tasks: [doneTask, openTask] });
  const page = new StubPage();
  page.hideCompleted(true);
  page.respondWithPayload(payload);

  await page.refresh();

  assert.doesNotMatch(page.textOf('issues'), /ALPHA-DONE/);
  assert.match(page.textOf('issues'), /ALPHA-OPEN/);
  assert.doesNotMatch(page.textOf('tasks'), /Finished chore/);
  assert.match(page.textOf('tasks'), /Pending chore/);
});

test('protect: with hide completed off the completed records stay listed', async () => {
  const page = new StubPage();
  page.hideCompleted(false);
  page.respondWithPayload(projectWithSettledIssue());

  await page.refresh();

  assert.match(page.textOf('issues'), /ALPHA-DONE/);
});

function snapshotOf(page) {
  const [stored] = [...page.storage.values()];
  return JSON.parse(stored);
}

function settle(payload, key) {
  const settled = structuredClone(payload);
  Object.assign(settled.issues.find(candidate => candidate.key === key), { verified: true, verified_at: LONG_AGO });
  return settled;
}

test('a record that settles as completed is reported as completed, not Removed', async () => {
  const open = issue('ALPHA-1');
  const before = dashboardPayload({ issues: [open], graphNodes: [issueNode(open)] });
  const page = new StubPage();
  page.hideCompleted(true);
  page.respondWithPayload(before);
  await page.refresh();

  page.respondWithPayload(settle(before, 'ALPHA-1'));
  await page.refresh();

  const changes = Array.from(page.evaluate('comparison.changes'));
  assert.deepEqual(changes.filter(change => change.startsWith('Removed')), []);
  assert.ok(changes.includes('Jira issue ALPHA-1 · ALPHA-1 title: verified completion recorded'), changes.join(' | '));
});

test('the stored comparison snapshot is version 4 and keeps settled completed records', async () => {
  const page = new StubPage();
  page.hideCompleted(true);
  page.respondWithPayload(projectWithSettledIssue());

  await page.refresh();

  const snapshot = snapshotOf(page);
  assert.equal(snapshot.version, 4);
  assert.deepEqual(Object.keys(snapshot.records).sort(), ['issue:ALPHA-DONE', 'issue:ALPHA-OPEN']);
});

test('a stored version 3 baseline is replaced by a new baseline without Added rows', async () => {
  const page = new StubPage();
  page.hideCompleted(true);
  page.respondWithPayload(projectWithSettledIssue());
  await page.refresh();
  const [key] = page.storage.keys();
  page.storage.set(key, JSON.stringify({ ...snapshotOf(page), version: 3, records: {} }));

  await page.refresh();

  assert.equal(page.evaluate('comparison.message'), NEW_BASELINE_MESSAGE);
  assert.equal(page.evaluate('comparison.changes.length'), 0);
  assert.equal(snapshotOf(page).version, 4);
  assert.doesNotMatch(page.textOf('changes-since-view'), /Added/);
});

function graphCard(page, id) {
  const card = page.element('map').descendants().find(node => node.getAttribute('data-node-id') === id);
  assert.ok(card, 'Expected dependency card ' + id);
  return card;
}

function cardTitle(card) {
  return card.children.find(node => node.tagName === 'title').textContent;
}

test('hover identifies the unmet prerequisite outside the search and its owner; clicking still opens the dependent', async () => {
  const dependent = issue('ALPHA-7', { delivery: { blocked: true, blockers: ['Awaiting contract approval'], owner: 'Dependent worker' } });
  const pending = freeTextTask('contract-task', 'Publish contract', { owner: 'Ada' });
  const complete = freeTextTask('finished-task', 'Old prerequisite', { status: 'done', evidenced: true, owner: 'Bob' });
  const payload = dashboardPayload({
    issues: [dependent], tasks: [pending, complete],
    graphNodes: [issueNode(dependent), taskNode(pending), { ...taskNode(complete), ready: true }],
  });
  payload.graph.edges = [pending, complete].map(task => ({ kind: 'requires', from: 'task:' + task.id, to: 'issue:ALPHA-7' }));
  const page = new StubPage();
  page.search('ALPHA-7');
  page.respondWithPayload(payload);
  await page.refresh();

  const card = graphCard(page, 'issue:ALPHA-7');
  const title = cardTitle(card);
  assert.match(title, /Awaiting contract approval/);
  assert.match(title, /Publish contract/);
  assert.match(title, /Ada/);
  assert.doesNotMatch(title, /Old prerequisite|Bob|Dependent worker/);
  assert.match(title, /Full record: ALPHA-7/);
  card.listeners.get('click')();
  assert.equal(page.element('issue-detail').dataset.issueKey, 'ALPHA-7');
  assert.match(page.textOf('issue-detail-body'), /Awaiting contract approval/);
});

test('blocked task hover shows its recorded reason and makes missing blocker ownership explicit', async () => {
  const task = freeTextTask('schema-task', 'Import schema', { status: 'blocked', blockers: ['Schema file missing'] });
  const page = await pageWithSnapshot(dashboardPayload({ tasks: [task], graphNodes: [taskNode(task)] }));
  const title = cardTitle(graphCard(page, 'task:schema-task'));
  assert.match(title, /Schema file missing/);
  assert.match(title, /(?:unknown|unavailable|not recorded|not specified)/i);
});

test('status-only blocked task hover discloses the missing reason; an open sibling keeps its normal hover', async () => {
  const blocked = freeTextTask('waiting', 'Wait for input', { status: 'blocked' });
  const open = freeTextTask('available', 'Prepare notes');
  const tasks = [blocked, open];
  const page = await pageWithSnapshot(dashboardPayload({ tasks, graphNodes: tasks.map(task => taskNode(task)) }));
  const blockedTitle = cardTitle(graphCard(page, 'task:waiting'));
  assert.match(blockedTitle, /blocked/i);
  assert.match(blockedTitle, /(?:not recorded|no .*reason|unknown|unspecified)/i);
  const openTitle = cardTitle(graphCard(page, 'task:available'));
  assert.match(openTitle, /Prepare notes/);
  assert.doesNotMatch(openTitle, /blocked|blocker/i);
});

test('inherited issue prerequisite is identified with its assignee even without a direct edge on the card', async () => {
  const dependent = issue('ALPHA-8', { delivery: { blocked: true, blockers: ['Unmet prerequisite: issue:ALPHA-2'] } });
  const prerequisite = issue('ALPHA-2', { delivery: { ownership: { jira: { status: 'assigned', name: 'Kim' } } } });
  const payload = dashboardPayload({ issues: [dependent, prerequisite], graphNodes: [issueNode(dependent), issueNode(prerequisite)] });
  const page = new StubPage();
  page.element('graph-hierarchy').setAttribute('aria-pressed', 'true');
  page.search('ALPHA-8');
  page.respondWithPayload(payload);
  await page.refresh();

  const title = cardTitle(graphCard(page, 'issue:ALPHA-8'));
  assert.match(title, /ALPHA-2/);
  assert.match(title, /Kim/);
});

test('verified dependent keeps its completion tooltip when an incoming prerequisite remains unfinished', async () => {
  const dependent = issue('ALPHA-9', { verified: true, verified_at: LONG_AGO });
  const pending = freeTextTask('pending-task', 'Pending task', { owner: 'Ada' });
  const payload = dashboardPayload({ issues: [dependent], tasks: [pending], graphNodes: [issueNode(dependent), taskNode(pending)] });
  payload.graph.edges = [{ kind: 'requires', from: 'task:pending-task', to: 'issue:ALPHA-9' }];
  const page = await pageWithSnapshot(payload);

  const title = cardTitle(graphCard(page, 'issue:ALPHA-9'));
  assert.match(title, /Verified complete/);
  assert.doesNotMatch(title, /blocked|awaiting prerequisite|Ada/i);
});

test('duplicate prerequisite task IDs with conflicting owners cannot attribute the blocker to either owner', async () => {
  const dependent = issue('ALPHA-10');
  const first = freeTextTask('shared-task', 'Prepare contract', { owner: 'Ada' });
  const second = freeTextTask('shared-task', 'Prepare contract', { owner: 'Bob', project: 'beta' });
  const payload = dashboardPayload({ issues: [dependent], tasks: [first, second], graphNodes: [issueNode(dependent), taskNode(first)] });
  payload.graph.edges = [{ kind: 'requires', from: 'task:shared-task', to: 'issue:ALPHA-10' }];
  const page = await pageWithSnapshot(payload);

  const title = cardTitle(graphCard(page, 'issue:ALPHA-10'));
  assert.match(title, /shared-task/);
  assert.match(title, /conflict|ambiguous|not recorded|unknown/i);
  assert.doesNotMatch(title, /Ada|Bob/);
});
