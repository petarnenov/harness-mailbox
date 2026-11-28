import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createPage, flush, okResponse } from './factory_doubles.mjs';

const APP_URL = new URL('../../static/app.js', import.meta.url);
const FACTORY_URL = new URL('../../static/factory.js', import.meta.url);
const KEYS = ['overview', 'dependencies', 'tasks', 'estimates', 'sources', 'factory'];
const EXISTING_TITLES = {
  overview: ['A clear view of the work.', 'Dependencies, verified progress and agent effort in one place.'],
  dependencies: ['See what depends on what.', 'Follow project, Epic, issue and local task relationships.'],
  tasks: ['From scope to verified work.', 'Jira snapshots and local execution, with evidence kept distinct.'],
  estimates: ['Plan the effort explicitly.', 'Explore agent machine-hours with transparent assumptions.'],
  sources: ['Know where the numbers come from.', 'Reported snapshots, provenance and data-quality warnings.']
};
const FACTORY_TITLE = ['Watch the work move.', 'Each task rides its worker session lane from station to station until it is delivered.'];
const LIBRARY_COUNT = 3;

function dashboardPayload() {
  return {
    mode: 'live', generated_at: '2026-01-01T00:00:00Z', warnings: [], reported_statistics: [], epics: [],
    totals: { scope_complete: true }, estimate_method: { low: 1, likely: 2, high: 3, review_multiplier: 1 },
    projects: [], issues: [], tasks: [], graph: { nodes: [], edges: [] }
  };
}

function newDashboard({ withFactory }) {
  const page = createPage();
  const make = (tag, properties) => Object.assign(page.document.createElement(tag), properties);
  const tabs = KEYS.map(key => make('a', { id: 'tab-' + key, tabIndex: 0 }));
  const views = KEYS.map(key => make('section', { id: 'view-' + key, hidden: false }));
  const heading = { title: make('h1'), subtitle: make('p'), eyebrow: make('p') };
  const headings = { '.page-heading h1': heading.title, '.page-heading .subtitle': heading.subtitle, '.page-heading .eyebrow': heading.eyebrow };
  page.document.querySelectorAll = selector => ({ '.view-tabs [role=tab]': tabs, '.page-view': views })[selector] || [];
  page.document.querySelector = selector => headings[selector] || page.sink;
  page.document.title = '';
  if (withFactory) page.runScript(FACTORY_URL);
  page.runScript(APP_URL);
  const show = hash => {
    page.location.hash = hash;
    page.fireWindow('hashchange');
  };
  const shownViews = () => views.filter(view => !view.hidden).map(view => view.id);
  const selectedTabs = () => tabs.filter(item => item.getAttribute('aria-selected') === 'true').map(item => item.id);
  return { page, show, shownViews, selectedTabs, heading, views, tabs };
}

async function loadDashboard(page, settle) {
  settle(page.requestTo('/api/dashboard')[0]);
  await flush();
}

async function openFactoryFromTab({ page, show }, factoryPayload = { lanes: [], boxes: [], events: [] }) {
  Object.assign(page.element('factory-stage'), { clientWidth: 900, clientHeight: 650 });
  show('#factory');
  for (let index = 0; index < LIBRARY_COUNT; index += 1) {
    await flush();
    page.scripts[index].onload();
  }
  await flush();
  page.requestTo('/api/factory')[0].resolve(okResponse(factoryPayload));
  await flush();
  return page.eval('factoryView');
}

test('factory is a known view and unknown hashes still fall back to overview', () => {
  for (const withFactory of [false, true]) {
    const dashboard = newDashboard({ withFactory });
    const { page, show, heading, shownViews, selectedTabs } = dashboard;

    show('#factory');
    assert.deepEqual([heading.title.textContent, heading.subtitle.textContent], FACTORY_TITLE);
    assert.equal(heading.eyebrow.textContent, 'PROJECT OPERATIONS / FACTORY');
    assert.equal(page.document.title, 'Mailbox · Factory');
    assert.deepEqual([shownViews(), selectedTabs()], [['view-factory'], ['tab-factory']]);

    for (const unknown of ['#nothing', '#', '', '#constructor', '#__proto__', '#Factory']) {
      show(unknown);
      assert.deepEqual([heading.title.textContent, shownViews(), selectedTabs()], [EXISTING_TITLES.overview[0], ['view-overview'], ['tab-overview']], JSON.stringify(unknown));
    }
  }
});

test('selecting another tab hides the factory and pauses it', async () => {
  const dashboard = newDashboard({ withFactory: true });
  const { page, show, shownViews } = dashboard;
  const factory = await openFactoryFromTab(dashboard);
  assert.deepEqual([factory.shown, factory.opened, factory.master.paused], [true, true, false]);

  show('#tasks');

  assert.deepEqual([factory.shown, factory.master.paused], [false, true]);
  assert.deepEqual(shownViews(), ['view-tasks']);
  assert.equal(dashboard.views.find(view => view.id === 'view-factory').hidden, true);
  assert.equal(page.element('factory-pause').textContent, 'Pause', 'the user pause state is not touched');
  show('#factory');
  assert.deepEqual([factory.shown, factory.master.paused], [true, false]);
});

test('the five existing views keep their titles and panels', () => {
  for (const withFactory of [false, true]) {
    const { page, show, heading, shownViews, selectedTabs, tabs } = newDashboard({ withFactory });

    for (const [key, [title, subtitle]] of Object.entries(EXISTING_TITLES)) {
      show('#' + key);
      assert.deepEqual([heading.title.textContent, heading.subtitle.textContent], [title, subtitle], key);
      assert.equal(heading.eyebrow.textContent, 'PROJECT OPERATIONS / ' + key.toUpperCase());
      assert.equal(page.document.title, 'Mailbox · ' + key[0].toUpperCase() + key.slice(1));
      assert.deepEqual([shownViews(), selectedTabs()], [['view-' + key], ['tab-' + key]], key);
      assert.deepEqual(tabs.map(item => item.tabIndex), KEYS.map(tabKey => (tabKey === key ? 0 : -1)));
    }
  }
});

test('load refreshes the factory only after a successful load', async () => {
  const failing = newDashboard({ withFactory: true });
  await openFactoryFromTab(failing);
  await loadDashboard(failing.page, request => request.reject(new TypeError('Failed to fetch')));
  assert.equal(failing.page.requestTo('/api/factory').length, 1);

  const succeeding = newDashboard({ withFactory: true });
  await openFactoryFromTab(succeeding);
  await loadDashboard(succeeding.page, request => request.resolve(okResponse(dashboardPayload())));
  assert.equal(succeeding.page.requestTo('/api/factory').length, 2);
  assert.equal(succeeding.page.requestTo('/api/factory')[1].url, '/api/factory?mode=live');
});

test('app.js alone still selects views and loads without error', async () => {
  const { page, show, heading, shownViews } = newDashboard({ withFactory: false });

  assert.equal(page.eval('typeof factorySetShown'), 'undefined');
  show('#factory');
  assert.deepEqual([heading.title.textContent, shownViews()], [FACTORY_TITLE[0], ['view-factory']]);
  show('#sources');
  assert.deepEqual([heading.title.textContent, shownViews()], [EXISTING_TITLES.sources[0], ['view-sources']]);

  await loadDashboard(page, request => request.resolve(okResponse(dashboardPayload())));

  assert.equal(page.element('error').hidden, true);
  assert.equal(page.eval('data.mode'), 'live');
  assert.equal(page.requestTo('/api/factory').length, 0);
  page.element('project').fire('change');
  assert.equal(page.element('error').hidden, true, 'a filter change renders without the factory');
});

const FACTORY_TASKS = [
  { id: 'ta', project: 'proj-a', teams: ['BE'] },
  { id: 'tb', project: 'proj-b', teams: ['FE'] },
  { id: 'tc', project: 'proj-a', teams: ['FE'] }
];

function filterableDashboard() {
  const tasks = FACTORY_TASKS.map(item => ({
    ...item, title: 'Title of ' + item.id, status: 'in_progress', mapping_kind: 'unmapped', epic_keys: [], issue_keys: [], unresolved_issue_keys: [], blockers: [],
    updated_at: '2020-01-01T00:00:00Z', evidenced: false
  }));
  return { ...dashboardPayload(), tasks, projects: [{ id: 'proj-a', name: 'A' }, { id: 'proj-b', name: 'B' }] };
}

function factoryOfTasks() {
  const at = index => '2026-11-28T10:0' + index + ':00Z';
  return {
    mode: 'live',
    lanes: [{ session_id: 's1', project: 'proj-a', session_name: 'Ada', last_status: null }],
    boxes: FACTORY_TASKS.map(item => ({ task_id: item.id, title: 'Title of ' + item.id, project: item.project, session_id: 's1', stage: 'investigating' })),
    events: FACTORY_TASKS.map((item, index) => ({ id: item.id + '/e', at: at(index), task_id: item.id, kind: 'enter', stage: 'investigating', gate: null }))
  };
}

async function openFilteredFactory(change) {
  const dashboard = newDashboard({ withFactory: true });
  const { page } = dashboard;
  await openFactoryFromTab(dashboard, factoryOfTasks());
  await loadDashboard(page, request => request.resolve(okResponse(filterableDashboard())));
  assert.deepEqual(page.eval('factoryView').plan.boxes.map(item => item.taskId), ['ta', 'tb', 'tc'], 'nothing is selected yet');
  change(page);
  return page.eval('factoryView');
}

test('changing the team, project or task filter limits the factory', async t => {
  const changes = {
    'the team filter': [page => { page.element('team').value = 'BE'; page.element('team').fire('change'); }, ['ta']],
    'the project filter': [page => { page.element('project').value = 'proj-a'; page.element('project').fire('change'); }, ['ta', 'tc']],
    'the task filter': [page => { page.element('epic').value = 'task:tb'; page.element('epic').fire('change'); }, ['tb']],
    'Find work': [page => { page.element('search').value = 'Title of TC'; page.element('search').fire('input'); }, ['tc']]
  };
  for (const [name, [change, expected]] of Object.entries(changes)) {
    await t.test(name, async () => {
      const current = await openFilteredFactory(change);

      assert.deepEqual(current.plan.boxes.map(item => item.taskId), expected);
      assert.deepEqual(Object.keys(current.scene.boxes), expected);
    });
  }
});

test('hide completed does not limit the factory', async () => {
  const current = await openFilteredFactory(page => {
    page.eval('data.tasks[0].evidenced = true');
    page.element('hide-completed').checked = true;
    page.element('hide-completed').fire('change');
    assert.deepEqual(page.eval('selection().tasks.map(task => task.id)').join(), 'tb,tc', 'the other views hide the settled task');
  });

  assert.deepEqual(current.plan.boxes.map(item => item.taskId), ['ta', 'tb', 'tc']);
  assert.deepEqual(Object.keys(current.scene.boxes), ['ta', 'tb', 'tc']);
});
