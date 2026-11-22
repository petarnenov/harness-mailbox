import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const APP_SOURCE = readFileSync(new URL('../../static/app.js', import.meta.url), 'utf8');
const LONG_AGO = '2020-01-01T00:00:00Z';

// app.js wires the page while it loads; every DOM lookup resolves to this inert element.
function inertElement() {
  const element = new Proxy(function () {}, {
    get: (_target, property) => {
      if (property === Symbol.toPrimitive) return () => '';
      if (property === Symbol.iterator) return function* () {};
      return element;
    },
    set: () => true,
    apply: () => element,
  });
  return element;
}

function loadDashboard(dashboardData) {
  const neverSettles = new Promise(() => {});
  const context = vm.createContext({
    document: inertElement(),
    window: inertElement(),
    history: inertElement(),
    location: { search: '', hash: '', href: 'http://dashboard.test/' },
    fetch: () => neverSettles,
    setInterval: () => 0,
    setTimeout: () => 0,
    clearInterval: () => {},
    clearTimeout: () => {},
    URL,
    URLSearchParams,
  });
  vm.runInContext(APP_SOURCE, context);
  vm.runInContext(`data = ${JSON.stringify(dashboardData)}`, context);
  return context;
}

function storyWithSubtasks() {
  const node = (id, kind, ready) => ({ id, kind, ready, label: id });
  const membership = (from, to) => ({ kind: 'membership', from, to });
  return {
    issues: [
      { key: 'STORY', verified: false, updated_at: LONG_AGO },
      { key: 'DONE', verified: true, verified_at: LONG_AGO },
      { key: 'OPEN', verified: false, updated_at: LONG_AGO },
      { key: 'JUST-DONE', verified: true, verified_at: new Date().toISOString() },
    ],
    tasks: [
      { id: 'done-task', evidenced: true, updated_at: LONG_AGO },
      { id: 'open-task', evidenced: false, updated_at: LONG_AGO },
    ],
    graph: {
      nodes: [
        node('issue:STORY', 'issue', false),
        node('issue:DONE', 'issue', true),
        node('issue:OPEN', 'issue', false),
        node('issue:JUST-DONE', 'issue', true),
        node('task:done-task', 'task', true),
        node('task:open-task', 'task', false),
      ],
      edges: [
        membership('issue:STORY', 'issue:DONE'),
        membership('issue:STORY', 'issue:OPEN'),
        membership('issue:STORY', 'issue:JUST-DONE'),
        membership('issue:DONE', 'task:done-task'),
        membership('issue:OPEN', 'task:open-task'),
      ],
    },
  };
}

const EMPTY_BASE_VIEW = { nodes: [], edges: [] };
// Arrays built inside the dashboard context are copied so they compare equal to the test's own.
const sortedIds = records => Array.from(records, record => record.id).sort();
const sortedEdgeTargets = edges => Array.from(edges, edge => edge.to).sort();

function focusedSubtree(dashboard, focusId, hiddenIds) {
  return dashboard.focusGraph(vm.runInContext('data.graph', dashboard), EMPTY_BASE_VIEW, focusId, 'hierarchy', true, hiddenIds);
}

const loadStoryDashboard = () => loadDashboard(storyWithSubtasks());

test('settled completed issues and tasks are the hidden records', () => {
  const dashboard = loadStoryDashboard();

  assert.deepEqual([...dashboard.settledCompletedNodeIds()].sort(), ['issue:DONE', 'task:done-task']);
});

test('a focused subtree leaves out the records hidden by Hide completed', () => {
  const dashboard = loadStoryDashboard();

  const view = focusedSubtree(dashboard, 'issue:STORY', dashboard.settledCompletedNodeIds());

  assert.deepEqual(sortedIds(view.nodes), ['issue:JUST-DONE', 'issue:OPEN', 'issue:STORY', 'task:open-task']);
  assert.deepEqual(sortedEdgeTargets(view.edges), ['issue:JUST-DONE', 'issue:OPEN', 'task:open-task']);
});

test('the focused record stays visible even when it is completed', () => {
  const dashboard = loadStoryDashboard();

  const view = focusedSubtree(dashboard, 'issue:DONE', dashboard.settledCompletedNodeIds());

  assert.deepEqual(sortedIds(view.nodes), ['issue:DONE']);
});

test('a focused subtree shows every descendant when nothing is hidden', () => {
  const dashboard = loadStoryDashboard();

  const view = focusedSubtree(dashboard, 'issue:STORY', undefined);

  assert.equal(view.nodes.length, 6);
});
