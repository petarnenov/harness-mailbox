import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const APP_URL = new URL('../../static/app.js', import.meta.url);
const APP_SOURCE = readFileSync(APP_URL, 'utf8');
const LONG_AGO = '2020-01-01T00:00:00Z';
const SECTION_TITLE = 'Why · recorded context';
const LOADING = 'Loading recorded context…';
const UNAVAILABLE = 'Recorded context is unavailable.';
const GATE_NOTICE = 'Recorded context only · lists direct and inherited prerequisites, not prerequisites of prerequisites · eligibility is decided by the dependency gate.';
const RAW_ERROR = "EACCES: permission denied, open '/Users/someone/mailbox-data/secret.json'";
const RAW_TRACEBACK = 'Traceback (most recent call last): File "/srv/app/context_graph.py", line 9, in build';

// A recording DOM: text reaches it only through textContent or text nodes; markup writes are logged.
class StubNode {
  constructor(page, tagName = 'div') {
    this.tagName = tagName;
    this.children = [];
    this.ownText = '';
    this.value = '';
    this.checked = false;
    this.open = false;
    this.dataset = {};
    this.style = {};
    this.attributes = new Map();
    this.classList = { add() {}, remove() {}, toggle() {}, contains: () => false };
    this.page = page;
  }

  get textContent() { return this.ownText; }
  set textContent(text) { this.ownText = String(text); this.children = []; }
  set innerHTML(markup) { this.page.markupWrites.push(markup); }

  descendants() { return this.children.flatMap(child => [child, ...child.descendants()]); }
  fullText() { return [this.ownText, ...this.children.map(child => child.fullText())].join(' '); }
  append(...items) { items.forEach(item => this.children.push(typeof item === 'string' ? this.page.textNode(item) : item)); }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  prepend(...items) { this.children.unshift(...items); }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name); }
  removeAttribute(name) { this.attributes.delete(name); }
  addEventListener() {}
  focus() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
}

class StubPage {
  constructor() {
    this.markupWrites = [];
    this.byId = new Map();
    this.requests = [];
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
      localStorage: { getItem: () => null, setItem() {} },
      fetch: url => page.fetch(url),
      setInterval: () => 0,
      setTimeout: callback => { queueMicrotask(callback); return 0; },
      clearInterval() {},
      clearTimeout() {},
      URL,
      URLSearchParams,
    });
    vm.runInContext(APP_SOURCE, this.context, { filename: APP_URL.href });
  }

  // The context container is created by the app inside the dialog body; resolve it there first.
  element(id) {
    if (id === 'issue-context-content') {
      const created = this.element('issue-detail-body').descendants().find(node => node.id === id);
      if (created) return created;
    }
    if (!this.byId.has(id)) this.byId.set(id, new StubNode(this));
    return this.byId.get(id);
  }

  textNode(text) {
    const node = new StubNode(this, '#text');
    node.ownText = String(text);
    return node;
  }

  // Every fetch waits until the test settles it; returns the pending request.
  fetch(url) {
    return new Promise((resolve, reject) => this.requests.push({ url: String(url), resolve, reject }));
  }

  evaluate(code) { return vm.runInContext(code, this.context); }
  setData(...keys) {
    this.evaluate('data=' + JSON.stringify({ issues: keys.map(issue), tasks: [], graph: { nodes: [], edges: [] }, projects: [] }));
  }
  open(key) { this.evaluate('openIssue(' + JSON.stringify(key) + ')'); return this.requests.at(-1); }
  toggleDemo(on) { this.element('demo').checked = on; }
  get dialogText() { return this.element('issue-detail-body').fullText(); }
  get contextText() { return this.element('issue-context-content').fullText(); }
  get sectionTitles() { return this.element('issue-detail-body').descendants().filter(node => node.tagName === 'h3').map(node => node.textContent); }
}

function issue(key) {
  return {
    key, title: key + ' title', verified: false, verified_at: null, updated_at: LONG_AGO,
    projects: ['alpha'], epic_keys: [], task_ids: [], teams: [], estimate: null, actual_machine_hours: null,
    source_status: 'Open', local_status: 'pending', recorded_local_status: 'pending',
    verification_reason: 'none', task_execution: { missing: [] }, sources: ['s'], original_estimate_hours: null,
    worked_hours: null,
  };
}

function ok(body) { return { ok: true, status: 200, json: () => Promise.resolve(body) }; }
function unparseable() { return { ok: true, status: 200, json: () => Promise.reject(new SyntaxError(RAW_ERROR)) }; }
const flush = () => new Promise(resolve => setImmediate(resolve));

async function settle(request, response) { request.resolve(response); await flush(); }
async function fail(request, error) { request.reject(error); await flush(); }

function fullPack(overrides = {}) {
  return {
    decisions: [{
      decision_type: 'verdict', verdict: 'approved', decided_at: LONG_AGO, rationale: 'Safer to split the work',
      rationale_truncated: true, conditions: ['tests green', 'docs updated'], supersedes: 'decision-1', source: 'decisions/d-2.json',
    }],
    open_requests: [{ type: 'question', subject: 'Which schema', from_project: 'alpha', to_project: 'beta', created_at: LONG_AGO, source: 'inbox/r-1.json' }],
    evidence: [{ kind: 'report', label: 'Unit report', at: LONG_AGO, source: 'reports/e-1.json' }],
    unmet_prerequisites: [{
      label: 'ALPHA-2 prerequisite', status: 'pending', inherited_from: 'issue:EPIC-1',
      owners: [{ owner: 'Ada', task_id: 'task-7', session_id: 'sess-1', project: 'alpha', last_seen_at: LONG_AGO }],
    }],
    omitted: { decisions: 0, evidence: 0 },
    ...overrides,
  };
}

async function openWithPack(pack, key = 'ALPHA-1') {
  const page = new StubPage();
  page.setData(key);
  await settle(page.open(key), ok(pack));
  return page;
}

test('the issue detail has a Why recorded context section showing the loading text while the request is open', () => {
  const page = new StubPage();
  page.setData('ALPHA-1');
  page.open('ALPHA-1');

  assert.ok(page.sectionTitles.includes(SECTION_TITLE), page.sectionTitles.join(' | '));
  assert.equal(page.contextText.trim(), LOADING);
});

test('the request carries the issue node and the page mode', () => {
  const page = new StubPage();
  page.setData('ALPHA-1');
  const live = page.open('ALPHA-1');
  page.toggleDemo(true);
  const demo = page.open('ALPHA-1');

  assert.equal(live.url, '/api/context?node=issue%3AALPHA-1&mode=live');
  assert.equal(demo.url, '/api/context?node=issue%3AALPHA-1&mode=demo');
});

test('a pack renders decisions with rationale, truncation marker, conditions, supersedes and source', async () => {
  const text = (await openWithPack(fullPack())).contextText;

  assert.match(text, /verdict · approved/);
  assert.match(text, /Rationale: Safer to split the work … \[rationale truncated\]/);
  assert.match(text, /Conditions: tests green; docs updated/);
  assert.match(text, /Supersedes: decision-1/);
  assert.match(text, /Source: decisions\/d-2\.json/);
});

test('a decision whose rationale is not truncated shows no truncation marker', async () => {
  const pack = fullPack({ decisions: [{ decision_type: 'verdict', verdict: 'approved', rationale: 'Short', rationale_truncated: false }] });

  assert.doesNotMatch((await openWithPack(pack)).contextText, /rationale truncated|Conditions:|Supersedes:/);
});

test('a pack renders open requests and latest evidence', async () => {
  const text = (await openWithPack(fullPack())).contextText;

  assert.match(text, /question · Which schema/);
  assert.match(text, /From alpha to beta/);
  assert.match(text, /Source: inbox\/r-1\.json/);
  assert.match(text, /report · Unit report/);
  assert.match(text, /Source: reports\/e-1\.json/);
});

test('a pack renders unmet prerequisites with the inherited origin and every owner field', async () => {
  const text = (await openWithPack(fullPack())).contextText;

  assert.match(text, /ALPHA-2 prerequisite · pending/);
  assert.match(text, /Inherited from issue:EPIC-1/);
  assert.match(text, /Owner: Ada · task task-7 · session sess-1 · project alpha · last seen/);
});

test('a direct prerequisite without owners says so and names the owner as unknown', async () => {
  const pack = fullPack({ unmet_prerequisites: [{ label: 'ALPHA-3', status: 'pending', inherited_from: null, owners: [] }] });
  const text = (await openWithPack(pack)).contextText;

  assert.match(text, /Direct prerequisite/);
  assert.match(text, /Owner: unknown/);
  assert.doesNotMatch(text, /Inherited from/);
});

test('empty groups say None recorded and the four group headings are present', async () => {
  const page = await openWithPack({ omitted: {} });

  ['Decisions', 'Open requests', 'Latest evidence', 'Unmet prerequisites and owners'].forEach(title => assert.match(page.contextText, new RegExp(title)));
  assert.equal(page.contextText.split('None recorded.').length - 1, 4);
});

test('null and missing values are shown as unknown, not as null or undefined', async () => {
  const pack = fullPack({ decisions: [{ decision_type: null, verdict: null, rationale: null }], evidence: [{ kind: 'report' }] });
  const text = (await openWithPack(pack)).contextText;

  assert.match(text, /unknown · unknown/);
  assert.match(text, /Rationale: unknown/);
  assert.match(text, /report · unknown/);
  assert.doesNotMatch(text, /\bnull\b|undefined/);
});

test('the omitted line lists only non-zero counts', async () => {
  const pack = fullPack({ omitted: { decisions: 3, evidence: 0, unmet_prerequisites: 2, warnings: 1 } });

  assert.match((await openWithPack(pack)).contextText, /Not shown because of list limits: prerequisites 2, decisions 3, warnings 1\./);
});

test('the omitted line is absent when all counts are zero or the field is missing', async () => {
  for (const omitted of [{ decisions: 0, evidence: 0 }, undefined, null]) {
    const text = (await openWithPack(fullPack({ omitted }))).contextText;
    assert.doesNotMatch(text, /Not shown because of list limits/, JSON.stringify(omitted));
  }
});

test('a rendered pack ends with the gate notice', async () => {
  assert.match((await openWithPack(fullPack())).contextText, new RegExp(GATE_NOTICE.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
});

test('hostile strings stay text: no markup is written and the strings appear verbatim', async () => {
  const hostile = '<img src=x onerror=alert(1)><script>alert(2)</script>';
  const pack = fullPack({
    decisions: [{ decision_type: hostile, verdict: hostile, rationale: hostile, conditions: [hostile], source: hostile }],
    open_requests: [{ type: hostile, subject: hostile, source: hostile }],
    evidence: [{ kind: hostile, label: hostile, source: hostile }],
    unmet_prerequisites: [{ label: hostile, status: hostile, inherited_from: hostile, owners: [{ owner: hostile }] }],
  });
  const page = await openWithPack(pack);

  assert.deepEqual(page.markupWrites, []);
  assert.ok(page.contextText.includes(hostile));
  assert.ok(page.element('issue-context-content').descendants().every(node => node.tagName !== 'script' && node.tagName !== 'img'));
});

test('a rejected fetch shows only the unavailable text and never the raw error', async () => {
  const page = new StubPage();
  page.setData('ALPHA-1');
  await fail(page.open('ALPHA-1'), new Error(RAW_ERROR + ' ' + RAW_TRACEBACK));

  assert.equal(page.contextText.trim(), UNAVAILABLE);
  assert.ok(!page.dialogText.includes('/Users/someone'));
  assert.ok(!page.dialogText.includes('Traceback'));
  assert.ok(!page.dialogText.includes('EACCES'));
});

test('a non-OK response shows only the unavailable text, ignoring a body that carries raw text', async () => {
  for (const status of [400, 404, 500]) {
    const page = new StubPage();
    page.setData('ALPHA-1');
    await settle(page.open('ALPHA-1'), { ok: false, status, json: () => Promise.resolve({ error: RAW_ERROR }) });

    assert.equal(page.contextText.trim(), UNAVAILABLE, String(status));
    assert.ok(!page.dialogText.includes('EACCES'), String(status));
  }
});

test('a non-object or unreadable body shows only the unavailable text', async () => {
  const bodies = [['array', ok([1, 2])], ['string', ok(RAW_TRACEBACK)], ['null', ok(null)], ['number', ok(7)], ['unparseable', unparseable()]];
  for (const [name, response] of bodies) {
    const page = new StubPage();
    page.setData('ALPHA-1');
    await settle(page.open('ALPHA-1'), response);

    assert.equal(page.contextText.trim(), UNAVAILABLE, name);
    assert.ok(!page.dialogText.includes('EACCES') && !page.dialogText.includes('Traceback'), name);
  }
});

test('a response for a previously opened issue is ignored', async () => {
  const page = new StubPage();
  page.setData('ALPHA-1', 'ALPHA-2');
  const first = page.open('ALPHA-1');
  const second = page.open('ALPHA-2');

  await settle(first, ok(fullPack({ decisions: [{ decision_type: 'stale', verdict: 'old' }] })));
  assert.equal(page.contextText.trim(), LOADING);
  assert.equal(page.evaluate('contextCache.size'), 0);

  await settle(second, ok(fullPack({ decisions: [{ decision_type: 'fresh', verdict: 'new' }] })));
  assert.match(page.contextText, /fresh · new/);
  assert.doesNotMatch(page.contextText, /stale/);
});

test('a response for another mode is ignored when demo is toggled while the request is in flight', async () => {
  const page = new StubPage();
  page.setData('ALPHA-1');
  const liveRequest = page.open('ALPHA-1');
  page.toggleDemo(true);

  await settle(liveRequest, ok(fullPack()));

  assert.equal(page.evaluate('contextCache.size'), 0);
  assert.equal(page.contextText.trim(), LOADING);
});

test('the same issue in both modes keeps a separate cached pack for each mode', async () => {
  const page = new StubPage();
  page.setData('ALPHA-1');
  await settle(page.open('ALPHA-1'), ok(fullPack({ decisions: [{ decision_type: 'live-decision', verdict: 'a' }] })));
  page.toggleDemo(true);
  await settle(page.open('ALPHA-1'), ok(fullPack({ decisions: [{ decision_type: 'demo-decision', verdict: 'b' }] })));

  assert.deepEqual(Array.from(page.evaluate('[...contextCache.keys()].sort()')), ['demo:ALPHA-1', 'live:ALPHA-1']);
  assert.match(page.contextText, /demo-decision/);
  assert.doesNotMatch(page.contextText, /live-decision/);
  page.toggleDemo(false);
  page.evaluate('showContext("ALPHA-1")');
  assert.match(page.contextText, /live-decision/);
});

test('a refresh re-renders the dialog from the cache without a new loading state', async () => {
  const page = await openWithPack(fullPack());
  const before = page.requests.length;

  page.evaluate('renderIssueDetail("ALPHA-1");requestContext("ALPHA-1")');

  assert.equal(page.requests.length, before + 1);
  assert.match(page.contextText, /verdict · approved/);
  assert.doesNotMatch(page.contextText, new RegExp(LOADING));
});

test('a cached unavailable result is shown again after a refresh without a loading state', async () => {
  const page = new StubPage();
  page.setData('ALPHA-1');
  await fail(page.open('ALPHA-1'), new Error(RAW_ERROR));

  page.evaluate('renderIssueDetail("ALPHA-1")');

  assert.equal(page.contextText.trim(), UNAVAILABLE);
});

test('a refresh whose request succeeds replaces the cached pack with the new one', async () => {
  const page = await openWithPack(fullPack());
  page.evaluate('requestContext("ALPHA-1")');
  await settle(page.requests.at(-1), ok(fullPack({ decisions: [{ decision_type: 'newer', verdict: 'later' }] })));

  assert.match(page.contextText, /newer · later/);
});

test('an issue missing from the snapshot shows no context section', () => {
  const page = new StubPage();
  page.setData('ALPHA-1');
  page.evaluate('renderIssueDetail("GONE-1")');

  assert.ok(!page.sectionTitles.includes(SECTION_TITLE));
  assert.match(page.dialogText, /unavailable in the current snapshot/);
});
