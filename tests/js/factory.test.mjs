import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createPage, flush, okResponse } from './factory_doubles.mjs';

const FACTORY_URL = new URL('../../static/factory.js', import.meta.url);
const LIBRARY_SOURCES = ['/vendor/pixi-8.23.0.min.js', '/vendor/pixi-unsafe-eval-8.23.0.min.js', '/vendor/gsap-3.15.1.min.js'];
const DATA_FAILED = 'Factory data could not be loaded.';
const LIBRARY_FAILED = 'Factory animation could not be loaded.';
const RAW_PATH = '/Users/someone/mailbox-data/deliveries/task-1.json';
const RAW_TRACEBACK = 'Traceback (most recent call last):\n  File "/srv/app/factory_floor.py", line 41, in build_factory';
const RAW_MESSAGE = 'ENOENT: no such file ' + RAW_PATH + '\n' + RAW_TRACEBACK;
const RAW_FRAGMENTS = [RAW_PATH, 'Traceback', 'ENOENT', 'factory_floor.py', 'SyntaxError'];
const ROLE_LABELS = ['worker', 'writer + tests', 'reviewer', 'verifier'];
const FIXED_SCENE_TEXTS = ['G1', 'G2', 'IN', 'OUT', 'OFFICE', '</>', '$', '$$$', '#@!', '!', 'X'];
const BRAND_NAMES = /pixi|gsap|greensock|webflow|dhl|fedex|usps|amazon|tesla|ups\b/i;
const MARKUP_TITLE = '<img src=x onerror=alert(1)><script>alert(2)</script>';
const MARKUP_NAME = '<b onmouseover=alert(3)>Ada</b>';
const MARKUP_PROJECT = '<i>alpha</i>';

const plain = value => JSON.parse(JSON.stringify(value));

function lane(sessionId, project, name, lastStatus) {
  return { session_id: sessionId, project, session_name: name, registered_status: 'idle', last_seen_at: null, last_status: lastStatus };
}

function box(taskId, sessionId, stage, title = 'Title of ' + taskId) {
  return { task_id: taskId, title, project: 'alpha', session_id: sessionId, stage };
}

function events(...specs) {
  return specs.map(([id, taskId, kind, stage, gate = null], index) => ({
    id, at: '2026-11-28T10:' + String(index).padStart(2, '0') + ':00Z', task_id: taskId, kind, stage, gate
  }));
}

function payloadOf(lanes, boxes, specs) {
  return { generated_at: '2026-11-28T11:00:00Z', mode: 'live', warnings: [], stations: [], lanes, boxes, events: events(...specs) };
}

const WORKING = { status: 'working', at: '2026-11-28T09:00:00Z' };

function fullPayload() {
  return payloadOf(
    [lane('s1', 'alpha', 'Ada', WORKING), lane('s2', 'beta', null, null)],
    [box('t1', 's1', 'implementation'), box('t2', 's1', 'complete'), box('t3', 's2', 'investigating'), box('t4', 's2', 'review')],
    [
      ['t1/stage/investigating', 't1', 'enter', 'investigating'],
      ['t2/stage/investigating', 't2', 'enter', 'investigating'],
      ['t3/stage/investigating', 't3', 'enter', 'investigating'],
      ['t4/stage/investigating', 't4', 'enter', 'investigating'],
      ['t1/stage/implementation', 't1', 'move', 'implementation'],
      ['t1/refuse/g1', 't1', 'refuse', 'implementation', 'g1'],
      ['t4/stage/implementation', 't4', 'move', 'implementation'],
      ['t4/refuse/report', 't4', 'refuse', 'implementation', 'report'],
      ['t2/stage/implementation', 't2', 'move', 'implementation'],
      ['t2/stage/review', 't2', 'move', 'review'],
      ['t2/refuse/g2', 't2', 'refuse', 'review', 'g2'],
      ['t2/stage/verification', 't2', 'move', 'verification'],
      ['t2/exit', 't2', 'exit', 'verification'],
      ['t3/refuse/report', 't3', 'refuse', 'investigating', 'report']
    ]
  );
}

function onePathPayload(stationIndex, gate) {
  const stages = ['investigating', 'implementation', 'review', 'verification'];
  const moves = stages.slice(1, stationIndex + 1).map(stage => ['t1/stage/' + stage, 't1', 'move', stage]);
  return payloadOf([lane('s1', 'alpha', 'Ada', null)], [box('t1', 's1', stages[stationIndex])], [
    ['t1/stage/investigating', 't1', 'enter', 'investigating'],
    ...moves,
    ['t1/refuse/x', 't1', 'refuse', stages[stationIndex], gate]
  ]);
}

function newFactory() {
  const page = createPage();
  page.runScript(FACTORY_URL);
  Object.assign(page.element('factory-stage'), { clientWidth: 900, clientHeight: 650 });
  Object.assign(page.element('factory-tooltip'), { offsetWidth: 120, offsetHeight: 48 });
  return page;
}

async function loadLibraries(page) {
  for (let index = 0; index < LIBRARY_SOURCES.length; index += 1) {
    await flush();
    page.scripts[index].onload();
  }
  await flush();
}

async function openUntilData(page) {
  page.eval('factorySetShown(true)');
  await loadLibraries(page);
}

async function openFactory(page, payload) {
  await openUntilData(page);
  page.requestTo('/api/factory').at(-1).resolve(okResponse(payload));
  await flush();
  return view(page);
}

const SELECTION = { p: '', e: '', taskId: '', team: '', q: '', invalid: [], scopedTasks: [] };
const scopeOf = (...taskIds) => ({ ...SELECTION, p: 'alpha', scopedTasks: taskIds.map(id => ({ id })) });
const setScope = (page, selection) => page.call('factorySetScope', selection);

async function chooseReplay(page, taskId) {
  const select = page.element('factory-replay');
  select.value = taskId;
  select.fire('change');
  await flush();
  return view(page);
}

async function openReplay(page, payload, taskId) {
  await openFactory(page, payload);
  return chooseReplay(page, taskId);
}

async function refreshWith(page, payload) {
  page.eval('factoryRefresh()');
  page.requestTo('/api/factory').at(-1).resolve(okResponse(payload));
  await flush();
}

const view = page => page.eval('factoryView');
const statusText = page => page.element('factory-status').textContent;
const stepIndex = (page, id) => view(page).plan.steps.findIndex(step => step.id === id);

// Plays the steps before the named one and returns its timeline unplayed.
function playUntil(page, id) {
  const timelines = view(page).master.children();
  const index = stepIndex(page, id);
  timelines.slice(0, index).forEach(timeline => timeline.finish());
  return timelines[index];
}

const toEntries = (timeline, target) => timeline.recorded('to').filter(entry => entry.target === target);
const pointOf = display => ({ x: display.x, y: display.y });
const slotPoint = (page, laneIndex, station, slot) => page.call('factorySlotPoint', view(page).layout, laneIndex, station, slot);

// ---- plan, state and rules ----

test('plan keeps valid items and drops malformed items, duplicates and steps that do not fit the box state', () => {
  const page = newFactory();
  const valid = fullPayload();
  const payload = {
    lanes: [...valid.lanes, null, 'text', lane('s1', 'dup', 'Duplicate', null), lane('', 'alpha', 'No id', null), lane('s9', '', 'No project', null)],
    boxes: [...valid.boxes, box('t1', 's1', 'x'), box('t5', 'unknown', 'x'), { task_id: 't6', session_id: 's1' }, 7],
    events: [
      ...valid.events,
      { id: 't1/stage/investigating', at: 'later', task_id: 't1', kind: 'enter', stage: 'investigating' },
      { id: 'e-unknown', at: 'x', task_id: 'nobody', kind: 'enter', stage: 'investigating' },
      { id: 'e-kind', at: 'x', task_id: 't1', kind: 'jump', stage: 'review' },
      { id: 'e-stage', at: 'x', task_id: 't1', kind: 'move', stage: 'attic' },
      { id: 'e-gate', at: 'x', task_id: 't1', kind: 'refuse', stage: 'review', gate: 'g3' },
      { id: 'e-twice', at: 'x', task_id: 't1', kind: 'enter', stage: 'investigating' },
      { id: 'e-after-exit', at: 'x', task_id: 't2', kind: 'move', stage: 'review' },
      { at: 'x', task_id: 't1', kind: 'move', stage: 'review' },
      null
    ]
  };

  const plan = page.call('factoryPlan', payload);

  assert.deepEqual(plan.lanes.map(item => item.sessionId), ['s1', 's2']);
  assert.deepEqual(plan.lanes[0], { sessionId: 's1', project: 'alpha', name: 'Ada', lastStatus: WORKING });
  assert.deepEqual(plan.lanes[1], { sessionId: 's2', project: 'beta', name: 's2', lastStatus: null });
  assert.deepEqual(plan.boxes.map(item => item.taskId), ['t1', 't2', 't3', 't4']);
  assert.deepEqual(plan.steps.map(step => step.id), valid.events.map(step => step.id));
  assert.deepEqual(plan.stations, ['investigating', 'implementation', 'review', 'verification']);
});

test('plan never throws and is empty for payloads that are not objects', () => {
  const page = newFactory();
  for (const payload of [undefined, null, 'text', 7, [], { lanes: 'x', boxes: 5, events: {} }]) {
    const plan = page.call('factoryPlan', payload);
    assert.deepEqual([plan.lanes, plan.boxes, plan.steps], [[], [], []]);
  }
});

test('plan drops a last status whose value is outside the protocol or has no time', () => {
  const page = newFactory();
  const lanes = [
    lane('a', 'p', 'A', { status: 'sleeping', at: '2026-11-28T09:00:00Z' }),
    lane('b', 'p', 'B', { status: 'idle' }),
    lane('c', 'p', 'C', 'idle'),
    lane('d', 'p', 'D', { status: 'blocked', at: '2026-11-28T09:00:00Z' })
  ];

  const plan = page.call('factoryPlan', { lanes });

  assert.deepEqual(plan.lanes.map(item => item.lastStatus), [null, null, null, { status: 'blocked', at: '2026-11-28T09:00:00Z' }]);
});

test('the slot rule gives boxes at one station different slots and different points', async () => {
  const page = newFactory();
  const payload = payloadOf([lane('s1', 'alpha', 'Ada', null)], [box('t1', 's1', 'a'), box('t2', 's1', 'a'), box('t3', 's1', 'a')], [
    ['t1/e', 't1', 'enter', 'investigating'],
    ['t2/e', 't2', 'enter', 'investigating'],
    ['t3/e', 't3', 'enter', 'investigating']
  ]);
  await openFactory(page, payload);

  const slots = page.call('factoryStateAfter', view(page).plan, 3).map(item => item.slot);
  const points = slots.map(slot => slotPoint(page, 0, 0, slot));

  assert.deepEqual(slots, [0, 1, 2]);
  assert.equal(new Set(points.map(point => point.x + ',' + point.y)).size, 3);
});

test('a box keeps its slot until it leaves the station and the lowest free slot is reused', () => {
  const page = newFactory();
  const plan = page.call('factoryPlan', payloadOf([lane('s1', 'alpha', 'Ada', null)], [box('t1', 's1', 'a'), box('t2', 's1', 'a'), box('t3', 's1', 'a')], [
    ['t1/e', 't1', 'enter', 'investigating'],
    ['t2/e', 't2', 'enter', 'investigating'],
    ['t1/m', 't1', 'move', 'implementation'],
    ['t3/e', 't3', 'enter', 'investigating']
  ]));

  const bySlot = count => Object.fromEntries(page.call('factoryStateAfter', plan, count).map(item => [item.taskId, [item.station, item.slot]]));

  assert.deepEqual(bySlot(2), { t1: [0, 0], t2: [0, 1], t3: [null, null] });
  assert.deepEqual(bySlot(3), { t1: [1, 0], t2: [0, 1], t3: [null, null] });
  assert.deepEqual(bySlot(4), { t1: [1, 0], t2: [0, 1], t3: [0, 0] });
  assert.equal(page.call('factoryFreeSlot', page.call('factoryStateAfter', plan, 2), 's1', 0), 2);
  assert.equal(page.call('factoryFreeSlot', page.call('factoryStateAfter', plan, 2), 's1', 3), 0);
});

test('state after a count of steps treats a missing or negative count as none and an exit as gone', () => {
  const page = newFactory();
  const plan = page.call('factoryPlan', fullPayload());

  const none = page.call('factoryStateAfter', plan, -3);
  const all = page.call('factoryStateAfter', plan, 99);

  assert.ok(none.every(item => item.station === null && !item.gone));
  assert.deepEqual(page.call('factoryStateAfter', plan, undefined), none);
  assert.deepEqual(all.find(item => item.taskId === 't2'), { taskId: 't2', laneId: 's1', station: null, slot: null, gone: true });
});

test('split steps separates the played ids from the fresh ones in payload order', () => {
  const page = newFactory();
  const plan = page.call('factoryPlan', fullPayload());
  page.context.playedIds = new Set(['t1/stage/investigating', 't3/refuse/report']);

  const split = JSON.parse(page.eval('JSON.stringify(factorySplitSteps(' + JSON.stringify(plan) + ', playedIds))'));

  assert.deepEqual(split.played.map(step => step.id), ['t1/stage/investigating', 't3/refuse/report']);
  assert.equal(split.fresh.length, plan.steps.length - 2);
  assert.ok(split.fresh.every(step => !split.played.some(played => played.id === step.id)));
});

test('the refusal table of gate and station names the checkpoint', () => {
  const page = newFactory();
  const expected = { g1: ['g1', 'g1', 'g1', 'g1'], g2: ['g2', 'g2', 'g2', 'g2'], report: ['g1', 'g1', 'g2', 'g2'] };
  for (const [gate, checkpoints] of Object.entries(expected)) {
    checkpoints.forEach((checkpoint, station) => {
      assert.equal(page.call('factoryRefusalCheckpoint', gate, station), checkpoint, gate + ' at station ' + station);
    });
  }
});

// ---- layout ----

test('layout has one lane per session with four stations and two checkpoints', () => {
  const page = newFactory();

  for (const count of [1, 2, 5]) {
    const layout = page.call('factoryLayout', count, 900, 650);
    assert.equal(layout.lanes.length, count);
    layout.lanes.forEach((item, index) => {
      const [first, second, third, fourth] = item.stations;
      assert.equal(item.stations.length, 4);
      assert.deepEqual(Object.keys(item.checkpoints), ['g1', 'g2']);
      assert.ok(first.x < item.checkpoints.g1.x && item.checkpoints.g1.x < second.x, 'G1 sits between the first two stations');
      assert.ok(second.x < third.x && third.x < item.checkpoints.g2.x && item.checkpoints.g2.x < fourth.x, 'G2 sits between the last two stations');
      if (index > 0) assert.ok(item.belt.y > layout.lanes[index - 1].belt.y, 'lanes stack downwards');
    });
  }
  assert.deepEqual(page.call('factoryLayout', 0, 900, 650).lanes, []);
  assert.deepEqual(page.call('factoryLayout', -2, 900, 650).lanes, []);
});

test('layout puts the office in the upper right corner', () => {
  const page = newFactory();

  const layout = page.call('factoryLayout', 3, 900, 650);
  const { office } = layout;

  assert.equal(office.x + office.width, layout.width - layout.margin);
  assert.equal(office.y, layout.margin);
  assert.ok(office.x > layout.width / 2 && office.y + office.height < layout.height / 4);
  for (const spot of [office.owner, office.moneyBox]) {
    assert.ok(spot.x > office.x && spot.x < office.x + office.width && spot.y > office.y && spot.y < office.y + office.height);
  }
});

test('layout fits the given width and keeps every station inside it', () => {
  const page = newFactory();

  for (const width of [320, 480, 800, 1440]) {
    for (const count of [0, 1, 3, 6]) {
      const layout = page.call('factoryLayout', count, width, 650);
      const inside = point => point.x >= 0 && point.x <= width;
      assert.ok(layout.lanes.every(item => item.belt.x2 <= width && item.stations.every(inside) && inside(item.checkpoints.g1) && inside(item.checkpoints.g2)), width + ' px, ' + count + ' lanes');
      assert.ok(inside(layout.entrance) && inside(layout.door) && layout.car.x + layout.car.width / 2 <= width);
      assert.ok(layout.office.x >= 0 && layout.office.x + layout.office.width <= width);
      layout.lanes.forEach((item, index) => {
        for (let slot = 0; slot < 12; slot += 1) {
          const point = page.call('factorySlotPoint', layout, index, 3, slot);
          assert.ok(point.x >= 0 && point.x <= width && point.y <= 650, 'slot ' + slot);
        }
      });
    }
  }
  assert.equal(page.call('factoryLayout', 4, 320, 650).compact, true);
  assert.equal(page.call('factoryLayout', 2, 1440, 650).compact, false);
});

test('slot points of two slots at one station differ and the label sits above the robot', () => {
  const page = newFactory();
  const layout = page.call('factoryLayout', 2, 900, 650);

  const first = page.call('factorySlotPoint', layout, 1, 2, 0);
  const second = page.call('factorySlotPoint', layout, 1, 2, 1);

  assert.notDeepEqual(first, second);
  assert.equal(page.call('factoryLabelAbove', 34, 11), -34 * 0.69 - 11 * 0.7);
});

test('scene colours follow the light and the dark theme', async () => {
  const backgroundOf = scene => scene.root.children[0].ops.find(entry => entry.op === 'fill').args[0];
  const expected = { light: 0xf9fbfd, dark: 0x101c28 };
  for (const [theme, background] of Object.entries(expected)) {
    const page = newFactory();
    page.document.documentElement.dataset.theme = theme;
    await openFactory(page, fullPayload());
    assert.equal(backgroundOf(view(page).scene), background, theme + ' scene background');
    assert.equal(page.pixi.applications[0].initOptions.background, background, theme + ' application background');
  }
  const page = newFactory();
  assert.notDeepEqual(page.call('factoryPalette', 'light'), page.call('factoryPalette', 'dark'));
  assert.deepEqual(page.call('factoryPalette', 'unknown'), page.call('factoryPalette', 'dark'));
});

test('the theme button redraws the scene in the new colours', async () => {
  const page = newFactory();
  await openFactory(page, fullPayload());
  const before = view(page).scene;

  page.document.documentElement.dataset.theme = 'light';
  page.element('theme-toggle').fire('click');

  const after = view(page).scene;
  assert.notEqual(after, before);
  assert.equal(before.root.destroyed, true);
  assert.equal(after.root.children[0].ops.find(entry => entry.op === 'fill').args[0], 0xf9fbfd);
});

test('resize redraws the scene to the new width', async () => {
  const page = newFactory();
  await openFactory(page, fullPayload());
  const before = view(page).scene;

  Object.assign(page.element('factory-stage'), { clientWidth: 500, clientHeight: 650 });
  page.fireWindow('resize');

  const after = view(page);
  assert.deepEqual(page.pixi.applications[0].resizes, [{ width: 500, height: 650 }]);
  assert.equal(after.layout.width, 500);
  assert.notEqual(after.scene, before);
  assert.equal(before.root.destroyed, true);

  page.fireWindow('resize');
  Object.assign(page.element('factory-stage'), { clientWidth: 0 });
  page.fireWindow('resize');
  assert.equal(page.pixi.applications[0].resizes.length, 1, 'same or empty size redraws nothing');
});

// ---- tooltips, text and speed ----

test('box tooltip names title, project and stage', async () => {
  const page = newFactory();
  const current = await openFactory(page, fullPayload());
  const tooltip = page.element('factory-tooltip');

  assert.equal(page.call('factoryBoxTooltip', { title: 'Fix it', project: 'alpha' }, 'review'), 'Fix it\nProject: alpha\nStage: review');
  current.scene.boxes.t3.emit('pointerover', { clientX: 100, clientY: 120 });
  assert.equal(tooltip.textContent, 'Title of t3\nProject: alpha\nStage: investigating');
  assert.equal(tooltip.hidden, false);
  assert.deepEqual([tooltip.style.left, tooltip.style.top], ['114px', '134px']);

  current.scene.boxes.t4.emit('pointerover', { clientX: 100, clientY: 120 });
  assert.equal(tooltip.textContent, 'Title of t4\nProject: alpha\nStage: implementation');
  current.scene.boxes.t2.emit('pointerover', { clientX: 100, clientY: 120 });
  assert.equal(tooltip.textContent, 'Title of t2\nProject: alpha\nStage: complete');
  current.scene.boxes.t3.emit('pointermove', { clientX: 300, clientY: 200 });
  assert.deepEqual([tooltip.style.left, tooltip.style.top], ['314px', '214px']);
  current.scene.boxes.t3.emit('pointerout', {});
  assert.equal(tooltip.hidden, true);
});

test('robot tooltip names the worker session and its last status', async () => {
  const page = newFactory();
  const current = await openFactory(page, fullPayload());
  const expected = 'Session: Ada\nProject: alpha\nLast status: working (2026-11-28T09:00:00Z)';

  for (const robot of [...current.scene.lanes[0].robots, current.scene.lanes[0].inspectors.g1, current.scene.lanes[0].inspectors.g2]) {
    robot.emit('pointerover', { clientX: 10, clientY: 10 });
    assert.equal(page.element('factory-tooltip').textContent, expected);
  }
  assert.equal(current.scene.lanes[0].robots.length, 4);
});

test('robot tooltip says not recorded when no status exists', async () => {
  const page = newFactory();
  const current = await openFactory(page, fullPayload());

  current.scene.lanes[1].robots[2].emit('pointerover', { clientX: 10, clientY: 10 });

  assert.equal(page.element('factory-tooltip').textContent, 'Session: s2\nProject: beta\nLast status: not recorded');
});

test('tooltip position flips to the other side of the pointer near the right and bottom edges', () => {
  const page = newFactory();

  assert.deepEqual(page.call('factoryTooltipPosition', 100, 100, 120, 48, 1000, 800), { left: 114, top: 114 });
  assert.deepEqual(page.call('factoryTooltipPosition', 950, 100, 120, 48, 1000, 800), { left: 816, top: 114 });
  assert.deepEqual(page.call('factoryTooltipPosition', 100, 780, 120, 48, 1000, 800), { left: 114, top: 718 });
  assert.deepEqual(page.call('factoryTooltipPosition', 10, 10, 120, 48, 100, 40), { left: 0, top: 0 });
});

test('status text of the current state counts the tasks on the floor', async () => {
  const page = newFactory();

  assert.equal(page.call('factoryFloorStatusText', 0), 'Current state · 0 tasks on the floor');
  assert.equal(page.call('factoryFloorStatusText', 1), 'Current state · 1 task on the floor');
  assert.equal(page.call('factoryFloorStatusText', 3), 'Current state · 3 tasks on the floor');
  await openFactory(page, fullPayload());
  assert.equal(statusText(page), 'Current state · 3 tasks on the floor');
});

test('status text of a replay names the task and counts its events', async () => {
  const page = newFactory();
  const current = await openReplay(page, fullPayload(), 't1');

  assert.equal(page.call('factoryReplayStatusText', 'Fix it', 2, 5, false), 'Replay · Fix it · event 2 of 5');
  assert.equal(page.call('factoryReplayStatusText', 'Fix it', 2, 5, true), 'Paused · Replay · Fix it · event 2 of 5');
  assert.equal(statusText(page), 'Replay · Title of t1 · event 0 of 3');
  current.master.children()[0].finish();
  assert.equal(statusText(page), 'Replay · Title of t1 · event 1 of 3');
  page.element('factory-pause').fire('click');
  assert.equal(statusText(page), 'Paused · Replay · Title of t1 · event 1 of 3');
});

test('titles and names are passed as text only', async () => {
  const page = newFactory();
  const payload = payloadOf([lane('s1', MARKUP_PROJECT, MARKUP_NAME, WORKING)], [box('t1', 's1', 'review', MARKUP_TITLE)], [
    ['t1/e', 't1', 'enter', 'investigating']
  ]);
  payload.boxes[0].project = MARKUP_PROJECT;
  const current = await openFactory(page, payload);
  current.master.finish();

  current.scene.boxes.t1.emit('pointerover', { clientX: 1, clientY: 1 });
  assert.equal(page.element('factory-tooltip').textContent, MARKUP_TITLE + '\nProject: ' + MARKUP_PROJECT + '\nStage: investigating');
  current.scene.lanes[0].robots[0].emit('pointerover', { clientX: 1, clientY: 1 });
  assert.match(page.element('factory-tooltip').textContent, /^Session: <b onmouseover=alert\(3\)>Ada<\/b>\nProject: <i>alpha<\/i>/);
  assert.ok(page.pixi.texts.some(text => text.text === MARKUP_NAME + ' · ' + MARKUP_PROJECT), 'the lane label reaches the Text double unchanged');
  assert.deepEqual(page.markupWrites, []);
});

test('scene texts contain no brand name', async () => {
  const page = newFactory();
  const current = await openFactory(page, fullPayload());
  current.master.finish();
  const laneTexts = current.plan.lanes.map(item => item.name + ' · ' + item.project);

  const drawn = page.pixi.texts.map(text => text.text);

  const allowed = new Set([...ROLE_LABELS, ...FIXED_SCENE_TEXTS, ...laneTexts]);
  assert.deepEqual(drawn.filter(text => !allowed.has(text)), []);
  assert.ok(ROLE_LABELS.every(label => drawn.includes(label)) && laneTexts.every(label => drawn.includes(label)));
  assert.deepEqual(drawn.filter(text => BRAND_NAMES.test(text)), []);
});

test('compact layouts leave the role labels out and keep the gate labels', async () => {
  const page = newFactory();
  Object.assign(page.element('factory-stage'), { clientWidth: 320 });

  await openFactory(page, fullPayload());

  const drawn = page.pixi.texts.map(text => text.text);
  assert.equal(view(page).layout.compact, true);
  assert.ok(ROLE_LABELS.every(label => !drawn.includes(label)));
  assert.ok(drawn.includes('G1') && drawn.includes('G2'));
});

test('pause stops the timeline and play resumes it', async () => {
  const page = newFactory();
  const current = await openReplay(page, fullPayload(), 't1');
  current.master.finish();
  const clocked = () => [current.master, ...current.scene.ambient, ...Object.values(current.scene.work).map(work => work.tween)];
  const pause = page.element('factory-pause');

  assert.ok(clocked().every(tween => tween.paused === false));
  assert.equal(pause.textContent, 'Pause');
  pause.fire('click');
  assert.ok(Object.keys(current.scene.work).length > 0 && clocked().every(tween => tween.paused === true));
  assert.equal(pause.textContent, 'Play');
  assert.equal(statusText(page), 'Paused · Replay · Title of t1 · event 3 of 3');
  pause.fire('click');
  assert.ok(clocked().every(tween => tween.paused === false));
  assert.equal(pause.textContent, 'Pause');
  assert.equal(statusText(page), 'Replay · Title of t1 · event 3 of 3');
});

test('speed sets the time scale', async () => {
  const page = newFactory();
  const current = await openFactory(page, fullPayload());
  current.master.finish();
  const speed = page.element('factory-speed');
  const scaled = () => [current.master, ...current.scene.ambient, ...Object.values(current.scene.work).map(work => work.tween)];

  for (const [value, expected] of [['2', 2], ['4', 4], ['0.5', 0.5], ['1', 1], ['3', 1], ['fast', 1], ['', 1]]) {
    speed.value = value;
    speed.fire('change');
    assert.equal(current.speed, expected, 'value ' + JSON.stringify(value));
    assert.ok(scaled().every(tween => tween.timeScales.at(-1) === expected));
  }
  assert.deepEqual([0.5, 1, 2, 4, 3, 'x'].map(value => page.call('factorySpeedFrom', value)), [0.5, 1, 2, 4, 1, 1]);
});

// ---- scene and steps ----

test('enter step brings the box with the product owner to its first station', async () => {
  const page = newFactory();
  const current = await openReplay(page, payloadOf([lane('s1', 'alpha', 'Ada', null)], [box('t1', 's1', 'investigating')], [['t1/e', 't1', 'enter', 'investigating']]), 't1');
  const [timeline] = current.master.children();
  const target = slotPoint(page, 0, 0, 0);
  const entrance = plain(current.layout.entrance);
  const display = current.scene.boxes.t1;

  assert.equal(display.visible, false);
  assert.deepEqual(plain(toEntries(timeline, display).map(entry => [entry.vars.x, entry.vars.y, entry.position])), [[target.x, target.y, 0]]);
  assert.deepEqual(plain(toEntries(timeline, current.scene.productOwner).map(entry => [entry.vars.x, entry.vars.y, entry.position])), [
    [target.x - current.layout.hold, target.y, 0],
    [entrance.x, entrance.y, 0.5]
  ]);
  assert.deepEqual(timeline.recorded('call').map(entry => entry.position), [0, 0.5, 1]);
  timeline.finish();
  assert.equal(display.visible, true);
  assert.deepEqual(pointOf(display), target);
  assert.deepEqual(pointOf(current.scene.productOwner), entrance);
  assert.equal(statusText(page), 'Replay · Title of t1 · event 1 of 1');
  assert.deepEqual([...current.playedIds], ['t1/e']);
});

test('a box at a station makes that station robot work on it', async () => {
  const page = newFactory();
  const current = await openReplay(page, fullPayload(), 't1');
  playUntil(page, 't1/stage/investigating').finish();

  const work = current.scene.work.t1;
  const post = current.scene.lanes[0].robots[0];
  const spot = slotPoint(page, 0, 0, 0);

  assert.equal(work.glyph.text, '</>');
  assert.deepEqual(pointOf(work.glyph), pointOf(post));
  assert.deepEqual([work.tween.target === work.glyph, work.tween.vars.x, work.tween.vars.y, work.tween.vars.repeat, work.tween.vars.yoyo], [true, spot.x, spot.y, -1, true]);

  page.eval('factoryStopWork(factoryView.scene, "t1")');
  assert.deepEqual([work.tween.killed, work.glyph.destroyed, current.scene.work.t1], [true, true, undefined]);
  page.eval('factoryStopWork(factoryView.scene, "t1")');
});

test('starting work on a box that already has a work loop replaces the old loop', async () => {
  const page = newFactory();
  const current = await openReplay(page, fullPayload(), 't1');
  playUntil(page, 't1/stage/implementation');
  const first = current.scene.work.t1;

  page.eval('factoryStartWork(factoryView.scene, factoryView.layout, "t1", { laneIndex: 0, station: 0, slot: 0 })');

  assert.equal(first.tween.killed, true);
  assert.notEqual(current.scene.work.t1, first);
});

test('move step rides the conveyor to the named station', async () => {
  const page = newFactory();
  const current = await openReplay(page, fullPayload(), 't1');
  const timeline = playUntil(page, 't1/stage/implementation');
  const target = slotPoint(page, 0, 1, 0);
  const display = current.scene.boxes.t1;

  assert.deepEqual(plain(toEntries(timeline, display).map(entry => [entry.vars.x, entry.vars.y, entry.vars.duration, entry.position])), [[target.x, target.y, 0.85, 0]]);
  timeline.finish();
  assert.deepEqual(pointOf(display), target);
  assert.equal(current.state.find(item => item.taskId === 't1').station, 1);
  assert.equal(current.scene.work.t1.glyph.text, '</>');
  assert.deepEqual(pointOf(current.scene.work.t1.glyph), pointOf(current.scene.lanes[0].robots[1]));
});

test('refuse step rides to the checkpoint of its gate and back to the robot it came from', async t => {
  const cases = [
    ['g1', 0, 'g1'], ['g1', 1, 'g1'], ['g1', 2, 'g1'], ['g1', 3, 'g1'],
    ['g2', 0, 'g2'], ['g2', 1, 'g2'], ['g2', 2, 'g2'], ['g2', 3, 'g2'],
    ['report', 0, 'g1'], ['report', 1, 'g1'], ['report', 2, 'g2'], ['report', 3, 'g2']
  ];
  for (const [gate, station, checkpoint] of cases) {
    await t.test(gate + ' refusal at station ' + station + ' goes to ' + checkpoint, async () => {
      const page = newFactory();
      const current = await openReplay(page, onePathPayload(station, gate), 't1');
      const timeline = playUntil(page, 't1/refuse/x');
      const display = current.scene.boxes.t1;
      const stop = plain(current.layout.lanes[0].checkpoints[checkpoint]);
      const home = slotPoint(page, 0, station, 0);

      const rides = toEntries(timeline, display);
      assert.deepEqual(plain(rides.map(entry => [entry.vars.x, entry.vars.y, entry.position])), [[stop.x, stop.y, 0], [home.x, home.y, 0.55]]);
      assert.equal(toEntries(timeline, current.scene.lanes[0].inspectors[checkpoint]).length, 1);
      const other = checkpoint === 'g1' ? 'g2' : 'g1';
      assert.equal(toEntries(timeline, current.scene.lanes[0].inspectors[other]).length, 0);
      timeline.finish();
      assert.deepEqual(pointOf(display), home);
      assert.equal(current.scene.work.t1.tween.vars.x, home.x);
      assert.equal(current.state.find(item => item.taskId === 't1').station, station);
    });
  }
});

test('the box stays at its station after a refusal and returns to its own slot', async () => {
  const page = newFactory();
  const lanes = [lane('s1', 'alpha', 'Ada', null)];
  const boxes = [box('t1', 's1', 'a'), box('t2', 's1', 'a'), box('t3', 's1', 'a')];
  const placed = [['t1/e', 't1', 'enter', 'investigating'], ['t2/e', 't2', 'enter', 'investigating']];
  const current = await openFactory(page, payloadOf(lanes, boxes, placed));
  const ownSlot = slotPoint(page, 0, 0, 1);

  await refreshWith(page, payloadOf(lanes, boxes, [...placed, ['t2/refuse/x', 't2', 'refuse', 'investigating', 'report'], ['t3/e', 't3', 'enter', 'investigating']]));
  const [refusal, entry] = current.master.children();

  refusal.finish();
  assert.deepEqual(pointOf(current.scene.boxes.t2), ownSlot);
  assert.deepEqual(current.state.filter(item => item.station !== null).map(item => [item.taskId, item.station, item.slot]), [['t1', 0, 0], ['t2', 0, 1]]);
  entry.finish();
  assert.deepEqual(pointOf(current.scene.boxes.t3), slotPoint(page, 0, 0, 2));
  assert.deepEqual(pointOf(current.scene.boxes.t2), ownSlot);
});

test('exit step carries the box through the door, the car leaves and the box is removed', async () => {
  const page = newFactory();
  const current = await openReplay(page, fullPayload(), 't2');
  const timeline = playUntil(page, 't2/exit');
  const { scene, layout } = current;
  const display = scene.boxes.t2;
  const robot = scene.lanes[0].robots[3];
  const home = pointOf(robot);
  const door = plain(layout.door);
  const carStop = plain(layout.car);

  assert.equal(scene.car.x > layout.width, true, 'the car waits off screen');
  assert.deepEqual(plain(toEntries(timeline, robot).map(entry => [entry.vars.x, entry.vars.y, entry.position])), [[door.x - layout.hold, door.y, 0], [home.x, home.y, 0.5]]);
  assert.deepEqual(plain(toEntries(timeline, display).map(entry => [entry.vars.x, entry.position])), [[door.x, 0], [carStop.x, 0.5], [layout.width + layout.car.width * 1.5, 0.65]]);
  timeline.finish();
  assert.equal(display.visible, false);
  assert.ok(scene.car.x > layout.width, 'the car left the screen');
  assert.deepEqual(pointOf(robot), home);
  assert.equal(scene.work.t2, undefined);
  assert.equal(current.state.find(item => item.taskId === 't2').gone, true);
});

test('exit makes the owner rub his hands and adds dollars to the money box', async () => {
  const page = newFactory();
  const current = await openReplay(page, fullPayload(), 't2');
  const timeline = playUntil(page, 't2/exit');
  const { hands, cheer, rage, dollars, moneyBox } = current.scene.office;

  hands.forEach(hand => {
    const [rub] = toEntries(timeline, hand);
    assert.deepEqual([rub.vars.repeat, rub.vars.yoyo, rub.position], [5, true, 0.5]);
  });
  assert.equal(toEntries(timeline, cheer)[0].vars.alpha, 1);
  assert.equal(toEntries(timeline, rage).length, 0);
  dollars.forEach(dollar => {
    const [start] = timeline.recorded('set').filter(entry => entry.target === dollar);
    const [fall] = toEntries(timeline, dollar);
    assert.deepEqual([start.vars.y, start.vars.alpha, fall.vars.y, fall.vars.alpha], [moneyBox.y - 48, 1, moneyBox.y, 0]);
  });
});

test('refuse makes the owner rage and removes dollars', async () => {
  const page = newFactory();
  const current = await openReplay(page, fullPayload(), 't1');
  const timeline = playUntil(page, 't1/refuse/g1');
  const { owner, hands, cheer, rage, dollars, moneyBox } = current.scene.office;
  const home = owner.x;

  const [shake] = toEntries(timeline, owner);
  assert.deepEqual([shake.vars.x, shake.vars.repeat, shake.vars.yoyo], [home + 3, 7, true]);
  assert.equal(toEntries(timeline, rage)[0].vars.alpha, 1);
  assert.equal(toEntries(timeline, cheer).length, 0);
  assert.ok(hands.every(hand => toEntries(timeline, hand).length === 0));
  dollars.forEach(dollar => {
    const [start] = timeline.recorded('set').filter(entry => entry.target === dollar);
    const [rise] = toEntries(timeline, dollar);
    assert.deepEqual([start.vars.y, start.vars.alpha, rise.vars.y, rise.vars.alpha], [moneyBox.y, 1, moneyBox.y - 48, 0]);
  });
  timeline.finish();
  assert.equal(owner.x, home);
});

test('every step timeline marks its event as played at one second', async () => {
  const page = newFactory();
  const current = await openReplay(page, fullPayload(), 't2');

  assert.equal(statusText(page), 'Replay · Title of t2 · event 0 of 6');
  current.master.children().forEach(timeline => assert.equal(timeline.recorded('call').at(-1).position, 1));
  current.master.finish();
  assert.equal(current.playedCount, 6);
  assert.equal(current.playedIds.size, 6);
});

// ---- data, refresh and the fixed failure texts ----

test('refresh in the current state animates only the events recorded since the previous load', async () => {
  const page = newFactory();
  const payload = fullPayload();
  const first = { ...payload, events: payload.events.slice(0, 5) };
  await openFactory(page, first);
  const oldMaster = view(page).master;
  await refreshWith(page, payload);

  const current = view(page);
  assert.equal(oldMaster.killed, true);
  assert.equal(current.master.children().length, 9, 'only the events recorded since the previous load are queued');
  assert.equal(statusText(page), 'Current state · 4 tasks on the floor');
  const shown = Object.fromEntries(Object.entries(current.scene.boxes).map(([id, display]) => [id, display.visible]));
  assert.deepEqual(shown, { t1: true, t2: true, t3: true, t4: true });
  assert.deepEqual(pointOf(current.scene.boxes.t1), slotPoint(page, 0, 1, 0));
  assert.deepEqual(pointOf(current.scene.boxes.t3), slotPoint(page, 1, 0, 0));
  assert.equal(Object.keys(current.scene.work).sort().join(), 't1,t2', 'only the boxes on the lane that reports working are worked on');
  const animatedTargets = new Set(current.master.children().flatMap(timeline => timeline.recorded('to').map(entry => entry.target)));
  assert.equal(animatedTargets.has(current.scene.productOwner), false, 'no played enter event is brought in again');
  current.master.finish();
  assert.equal(statusText(page), 'Current state · 3 tasks on the floor');
});

test('refresh removes a lane that left the payload together with its boxes', async () => {
  const page = newFactory();
  const payload = fullPayload();
  await openFactory(page, payload);
  const before = view(page).scene;

  const remaining = {
    ...payload,
    lanes: payload.lanes.slice(0, 1),
    boxes: payload.boxes.filter(item => item.session_id === 's1'),
    events: payload.events.filter(item => item.task_id === 't1' || item.task_id === 't2')
  };
  await refreshWith(page, remaining);

  const current = view(page);
  assert.equal(before.root.destroyed, true);
  assert.equal(current.scene.lanes.length, 1);
  assert.deepEqual(Object.keys(current.scene.boxes), ['t1', 't2']);
  assert.deepEqual(current.plan.boxes.map(item => item.taskId), ['t1', 't2']);
  assert.equal(current.layout.lanes.length, 1);
  assert.ok(!page.pixi.texts.filter(text => !text.destroyed).some(text => text.text.startsWith('s2 · ')));
});

test('refresh adds a lane that is new in the payload', async () => {
  const page = newFactory();
  const payload = fullPayload();
  const first = {
    ...payload,
    lanes: payload.lanes.slice(0, 1),
    boxes: payload.boxes.filter(item => item.session_id === 's1'),
    events: payload.events.filter(item => item.task_id === 't1' || item.task_id === 't2')
  };
  await openFactory(page, first);
  assert.equal(view(page).scene.lanes.length, 1);

  await refreshWith(page, payload);

  const current = view(page);
  assert.equal(current.scene.lanes.length, 2);
  assert.deepEqual(Object.keys(current.scene.boxes), ['t1', 't2', 't3', 't4']);
  assert.equal(current.master.children().length, 5, 'only the events of the new lane are queued');
  assert.equal(current.layout.lanes.length, 2);
  assert.ok(page.pixi.texts.some(text => text.text === 's2 · beta' && !text.destroyed));
});

test('failed fetch shows the fixed text and not the error', async t => {
  const failures = {
    'a rejected request': request => request.reject(new Error(RAW_MESSAGE)),
    'a non-200 answer': request => request.resolve({ ok: false, status: 500, text: () => Promise.resolve(RAW_MESSAGE), json: () => Promise.resolve({ error: { message: RAW_MESSAGE } }) }),
    'an unreadable body': request => request.resolve({ ok: true, status: 200, text: () => Promise.resolve(RAW_MESSAGE), json: () => Promise.reject(new SyntaxError(RAW_MESSAGE)) })
  };
  for (const [name, settle] of Object.entries(failures)) {
    await t.test(name, async () => {
      const page = newFactory();
      await openUntilData(page);

      settle(page.requestTo('/api/factory').at(-1));
      await flush();

      assert.equal(statusText(page), DATA_FAILED);
      assert.equal(view(page).scene, null);
      const texts = [...page.elementTexts(), ...page.pixi.texts.map(text => text.text)].join('\n');
      assert.deepEqual(RAW_FRAGMENTS.filter(fragment => texts.includes(fragment)), []);
    });
  }
});

test('a failed refresh keeps the fixed text and a later good answer clears it', async () => {
  const page = newFactory();
  await openFactory(page, fullPayload());

  page.eval('factoryRefresh()');
  page.requestTo('/api/factory').at(-1).reject(new Error(RAW_MESSAGE));
  await flush();
  assert.equal(statusText(page), DATA_FAILED);
  assert.equal(page.element('factory-pause').textContent, 'Pause');

  await refreshWith(page, fullPayload());
  assert.equal(statusText(page), 'Current state · 3 tasks on the floor');
});

test('a load that succeeds after a failed first load shows the current state and queues no step', async () => {
  const page = newFactory();
  await openUntilData(page);
  page.requestTo('/api/factory').at(-1).reject(new Error(RAW_MESSAGE));
  await flush();
  assert.equal(statusText(page), DATA_FAILED);

  await refreshWith(page, fullPayload());

  const current = view(page);
  assert.equal(statusText(page), 'Current state · 3 tasks on the floor');
  assert.equal(current.master.children().length, 0);
  assert.equal(current.playedIds.size, 14);
  assert.deepEqual(pointOf(current.scene.boxes.t1), slotPoint(page, 0, 1, 0));
});

test('a failed refresh during a replay shows the fixed text and keeps the replay', async () => {
  const page = newFactory();
  await openReplay(page, fullPayload(), 't1');

  page.eval('factoryRefresh()');
  page.requestTo('/api/factory').at(-1).reject(new Error(RAW_MESSAGE));
  await flush();

  assert.equal(statusText(page), DATA_FAILED);
  assert.equal(view(page).replayId, 't1');
  assert.deepEqual(RAW_FRAGMENTS.filter(fragment => page.elementTexts().join('\n').includes(fragment)), []);
});

test('an older answer that arrives after a newer one is ignored', async () => {
  const page = newFactory();
  await openFactory(page, fullPayload());
  page.eval('factoryRefresh()');
  page.eval('factoryRefresh()');
  const [older, newer] = page.requestTo('/api/factory').slice(-2);

  newer.resolve(okResponse(onePathPayload(1, 'g1')));
  await flush();
  older.reject(new Error(RAW_MESSAGE));
  await flush();

  assert.equal(statusText(page), 'Current state · 1 task on the floor');
  assert.deepEqual(view(page).plan.boxes.map(item => item.taskId), ['t1']);
});

test('failed library load shows the fixed text', async () => {
  const page = newFactory();

  page.eval('factorySetShown(true)');
  await flush();
  const [script] = page.scripts;
  script.onerror(new Error(RAW_MESSAGE));
  await flush();

  assert.equal(statusText(page), LIBRARY_FAILED);
  assert.equal(script.removed, true);
  assert.equal(view(page).opened, false);
  assert.deepEqual(RAW_FRAGMENTS.filter(fragment => page.elementTexts().join('\n').includes(fragment)), []);
  assert.equal(page.requestTo('/api/factory').length, 0);

  page.eval('factorySetShown(true)');
  await flush();
  assert.equal(page.scripts.length, 2);
  assert.equal(page.scripts[1].src, script.src);
});

test('the libraries load one after the other in the listed order and only once', async () => {
  const page = newFactory();

  page.eval('factorySetShown(true)');
  page.eval('factorySetShown(true)');
  await flush();
  assert.deepEqual(page.scripts.map(script => script.src), [LIBRARY_SOURCES[0]]);
  assert.equal(statusText(page), 'Loading factory…');
  await loadLibraries(page);

  assert.deepEqual(page.scripts.map(script => script.src), LIBRARY_SOURCES);
  assert.equal(page.requestTo('/api/factory').length, 1);
  assert.deepEqual(plain(page.pixi.applications[0].initOptions), { width: 900, height: 650, background: 0x101c28, preference: 'webgl', antialias: true });
  assert.deepEqual(page.element('factory-stage').children, [page.pixi.applications[0].canvas]);

  page.eval('factorySetShown(true)');
  await flush();
  assert.equal(page.scripts.length, 3);
  assert.equal(page.pixi.applications.length, 1);
});

test('hiding the tab pauses the clock and showing it resumes it', async () => {
  const page = newFactory();
  const current = await openFactory(page, fullPayload());
  current.master.finish();

  page.eval('factorySetShown(false)');
  assert.equal(current.shown, false);
  assert.equal(current.master.paused, true);
  assert.equal(page.element('factory-pause').textContent, 'Pause');
  page.eval('factorySetShown(true)');
  assert.equal(current.master.paused, false);
});

test('hiding the tab stops the ticker and showing it starts it', async () => {
  const page = newFactory();
  await openFactory(page, fullPayload());
  const { ticker } = page.pixi.applications[0];
  assert.deepEqual([ticker.running, ticker.calls], [true, ['start']]);

  page.eval('factorySetShown(false)');
  assert.deepEqual([ticker.running, ticker.calls], [false, ['start', 'stop']]);
  page.eval('factorySetShown(true)');
  assert.deepEqual([ticker.running, ticker.calls], [true, ['start', 'stop', 'start']]);
});

test('a tab hidden before the libraries finished loading has its ticker stopped once opened', async () => {
  const page = newFactory();

  page.eval('factorySetShown(true)');
  await flush();
  page.eval('factorySetShown(false)');
  await loadLibraries(page);

  const { ticker } = page.pixi.applications[0];
  assert.equal(view(page).opened, true);
  assert.deepEqual([ticker.running, ticker.calls], [false, ['stop']]);
});

test('Pause and Play do not call the ticker', async () => {
  const page = newFactory();
  await openReplay(page, fullPayload(), 't1');
  const { ticker } = page.pixi.applications[0];
  const before = [...ticker.calls];

  page.element('factory-pause').fire('click');
  page.element('factory-pause').fire('click');

  assert.deepEqual(ticker.calls, before);
  assert.equal(ticker.running, true);
});

test('the hooks do nothing before the factory was opened', () => {
  const page = newFactory();

  page.eval('factoryRefresh()');
  page.eval('factorySetShown(false)');
  page.eval('factoryResize()');
  page.eval('factoryRedraw()');
  page.eval('factoryChooseReplay()');
  setScope(page, scopeOf('t1'));

  assert.equal(page.requests.length, 0);
  assert.equal(page.scripts.length, 0);
  assert.equal(page.pixi.applications.length, 0);
  assert.deepEqual(page.markupWrites, []);
});

// ---- current state, replay, scope and work animation ----

const optionsOf = page => page.element('factory-replay').children.map(option => [option.value, option.textContent]);
const workOf = page => Object.keys(view(page).scene.work).sort();
const pauseButton = page => page.element('factory-pause');
const sceneBoxIds = page => Object.keys(view(page).scene.boxes);
const without = (payload, taskId) => ({
  ...payload,
  boxes: payload.boxes.filter(item => item.task_id !== taskId),
  events: payload.events.filter(item => item.task_id !== taskId)
});

function scopeFrom(page, selection) {
  page.context.selection = selection;
  return JSON.parse(page.eval('JSON.stringify({ limited: factoryScopeFrom(selection).limited, key: factoryScopeFrom(selection).key, taskIds: [...factoryScopeFrom(selection).taskIds] })'));
}

function boxVisible(page, selection, taskId, replayId = '') {
  page.context.selection = selection;
  page.context.candidate = { taskId };
  page.context.replayId = replayId;
  return page.eval('factoryBoxVisible(candidate, factoryScopeFrom(selection), replayId)');
}

test('the first load shows every box at its current station and queues no step', async () => {
  const page = newFactory();

  const current = await openFactory(page, fullPayload());

  assert.equal(current.master.children().length, 0);
  assert.equal(current.playedIds.size, 14);
  assert.equal(current.replayId, '');
  assert.deepEqual(pointOf(current.scene.boxes.t1), slotPoint(page, 0, 1, 0));
  assert.deepEqual(pointOf(current.scene.boxes.t3), slotPoint(page, 1, 0, 0));
  assert.deepEqual(pointOf(current.scene.boxes.t4), slotPoint(page, 1, 1, 0));
  assert.deepEqual(current.scene.lanes.length, 2);
  assert.equal(statusText(page), 'Current state · 3 tasks on the floor');
});

test('a task that has left is not on the floor in the current state', async () => {
  const page = newFactory();

  const current = await openFactory(page, fullPayload());

  assert.equal(current.scene.boxes.t2.visible, false);
  assert.deepEqual(['t1', 't3', 't4'].map(id => current.scene.boxes[id].visible), [true, true, true]);
  assert.equal(current.state.find(item => item.taskId === 't2').gone, true);
  assert.equal(current.scene.work.t2, undefined);
});

test('a switch of the mode shows the current state of the new mode and queues no step', async () => {
  const page = newFactory();
  const payload = fullPayload();
  await openFactory(page, { ...payload, events: payload.events.slice(0, 5) });

  page.element('demo').checked = true;
  await refreshWith(page, { ...payload, mode: 'demo' });

  assert.equal(page.requestTo('/api/factory').at(-1).url, '/api/factory?mode=demo');
  const current = view(page);
  assert.equal(current.master.children().length, 0);
  assert.equal(current.playedIds.size, 14);
  assert.equal(statusText(page), 'Current state · 3 tasks on the floor');
  assert.deepEqual(pointOf(current.scene.boxes.t4), slotPoint(page, 1, 1, 0));
});

test('a resize or theme change in the current state restarts nothing', async () => {
  const page = newFactory();
  const payload = fullPayload();
  await openFactory(page, { ...payload, events: payload.events.slice(0, 5) });
  await refreshWith(page, payload);
  view(page).master.children().slice(0, 2).forEach(timeline => timeline.finish());
  const before = view(page).scene;

  page.document.documentElement.dataset.theme = 'light';
  page.element('theme-toggle').fire('click');

  assert.notEqual(view(page).scene, before);
  assert.equal(view(page).master.children().length, 7, 'the events already played are not queued again');
  assert.equal(view(page).playedIds.size, 7);
  assert.equal(view(page).replayId, '');

  Object.assign(page.element('factory-stage'), { clientWidth: 500, clientHeight: 650 });
  page.fireWindow('resize');

  assert.equal(view(page).master.children().length, 7);
  assert.equal(view(page).playedIds.size, 7);
  assert.equal(statusText(page), 'Current state · 4 tasks on the floor');
});

test('the replay list offers every task with a box by title and short id, newest activity first', async () => {
  const page = newFactory();
  const [first, second, third] = ['1234abcd-0001-aaaa', '9876fedc-0002-bbbb', '5555eeee-0003-cccc'];
  const payload = payloadOf([lane('s1', 'alpha', 'Ada', null)], [box(first, 's1', 'a', 'First job'), box(second, 's1', 'a', 'Second job'), box(third, 's1', 'a', 'Third job')], [
    [third + '/e', third, 'enter', 'investigating'],
    [first + '/e', first, 'enter', 'investigating'],
    [second + '/e', second, 'enter', 'investigating'],
    [first + '/m', first, 'move', 'implementation'],
    [second + '/x', second, 'exit', 'investigating']
  ]);

  await openFactory(page, payload);

  assert.deepEqual(optionsOf(page), [
    ['', 'Current state'],
    [second, 'Second job · 9876fedc'],
    [first, 'First job · 1234abcd'],
    [third, 'Third job · 5555eeee']
  ]);
  assert.equal(page.element('factory-replay').value, '');
});

test('the replay list writes a title with markup characters as text', async () => {
  const page = newFactory();
  const payload = payloadOf([lane('s1', 'alpha', 'Ada', null)], [box('abcdef12-0001', 's1', 'a', MARKUP_TITLE)], [
    ['t/e', 'abcdef12-0001', 'enter', 'investigating']
  ]);

  await openFactory(page, payload);

  const [, option] = page.element('factory-replay').children;
  assert.equal(option.textContent, MARKUP_TITLE + ' · abcdef12');
  assert.equal(option.value, 'abcdef12-0001');
  assert.deepEqual(page.markupWrites, []);
});

test('choosing a task replays only that task\'s events from its first one', async () => {
  const page = newFactory();

  const current = await openReplay(page, fullPayload(), 't1');

  assert.equal(current.replayId, 't1');
  assert.deepEqual(current.plan.steps.map(step => step.id), ['t1/stage/investigating', 't1/stage/implementation', 't1/refuse/g1']);
  assert.equal(current.playedIds.size, 0);
  assert.equal(current.master.children().length, 3);
  assert.equal(current.scene.boxes.t1.visible, false, 'the box has not entered yet');
  assert.equal(toEntries(current.master.children()[0], current.scene.productOwner).length > 0, true, 'the first event is the enter');
  current.master.finish();
  assert.deepEqual(pointOf(current.scene.boxes.t1), slotPoint(page, 0, 1, 0));
});

test('a replay starts from the first event even when every event was already played', async () => {
  const page = newFactory();
  await openFactory(page, fullPayload());
  assert.equal(view(page).playedIds.size, 14);

  const current = await chooseReplay(page, 't2');

  assert.equal(current.playedIds.size, 0);
  assert.equal(current.master.children().length, 6);
  assert.equal(current.state.find(item => item.taskId === 't2').station, null);
});

test('a replay shows only the chosen task\'s lane and box', async () => {
  const page = newFactory();

  const current = await openReplay(page, fullPayload(), 't3');

  assert.deepEqual(current.plan.lanes.map(item => item.sessionId), ['s2']);
  assert.deepEqual(sceneBoxIds(page), ['t3']);
  assert.equal(current.scene.lanes.length, 1);
  const labels = page.pixi.texts.filter(text => !text.destroyed).map(text => text.text);
  assert.ok(labels.includes('s2 · beta'));
  assert.ok(!labels.includes('Ada · alpha'));
});

test('choosing the current state again shows it without animation', async () => {
  const page = newFactory();
  const replay = await openReplay(page, fullPayload(), 't1');
  replay.master.children()[0].finish();

  const current = await chooseReplay(page, '');

  assert.equal(current.replayId, '');
  assert.equal(current.master.children().length, 0);
  assert.equal(current.playedIds.size, 14);
  assert.equal(current.scene.lanes.length, 2);
  assert.equal(statusText(page), 'Current state · 3 tasks on the floor');
  assert.equal(page.element('factory-replay').value, '');
});

test('refresh during a replay keeps the played events and animates the rest', async () => {
  const page = newFactory();
  const payload = fullPayload();
  const shorter = { ...payload, events: payload.events.filter(item => item.id !== 't1/refuse/g1') };
  const replay = await openReplay(page, shorter, 't1');
  replay.master.finish();
  assert.equal(statusText(page), 'Replay · Title of t1 · event 2 of 2');

  await refreshWith(page, payload);

  const current = view(page);
  assert.equal(current.replayId, 't1');
  assert.equal(current.playedIds.size, 2);
  assert.equal(current.master.children().length, 1);
  assert.equal(statusText(page), 'Replay · Title of t1 · event 2 of 3');
  assert.deepEqual(pointOf(current.scene.boxes.t1), slotPoint(page, 0, 1, 0));
  assert.equal(page.element('factory-replay').value, 't1');
});

test('a replayed task missing after a refresh returns to the current state', async () => {
  const page = newFactory();
  const replay = await openReplay(page, fullPayload(), 't1');
  replay.master.children()[0].finish();

  await refreshWith(page, without(fullPayload(), 't1'));

  const current = view(page);
  assert.equal(current.replayId, '');
  assert.equal(current.master.children().length, 0);
  assert.equal(statusText(page), 'Current state · 2 tasks on the floor');
  assert.equal(page.element('factory-replay').value, '');
  assert.ok(optionsOf(page).every(([value]) => value !== 't1'));
  assert.equal(pauseButton(page).disabled, true);
});

test('a resize or theme change during a replay keeps the replay and its played events', async () => {
  const page = newFactory();
  const replay = await openReplay(page, fullPayload(), 't2');
  replay.master.children().slice(0, 3).forEach(timeline => timeline.finish());
  const before = view(page).scene;

  page.document.documentElement.dataset.theme = 'light';
  page.element('theme-toggle').fire('click');

  assert.notEqual(view(page).scene, before);
  assert.equal(view(page).replayId, 't2');
  assert.equal(view(page).playedIds.size, 3);
  assert.equal(view(page).master.children().length, 3);
  assert.equal(statusText(page), 'Replay · Title of t2 · event 3 of 6');

  Object.assign(page.element('factory-stage'), { clientWidth: 500, clientHeight: 650 });
  page.fireWindow('resize');

  assert.equal(view(page).replayId, 't2');
  assert.equal(view(page).playedIds.size, 3);
  assert.equal(view(page).master.children().length, 3);
  assert.equal(page.element('factory-replay').value, 't2');
});

test('hiding and showing the tab during a replay pauses and resumes it', async () => {
  const page = newFactory();
  const current = await openReplay(page, fullPayload(), 't1');

  page.eval('factorySetShown(false)');
  assert.equal(current.master.paused, true);
  page.eval('factorySetShown(true)');
  assert.equal(current.master.paused, false);
  assert.equal(current.replayId, 't1');
});

// ---- pause and speed ----

test('pause is disabled in the current state and enabled in a replay', async () => {
  const page = newFactory();
  await openFactory(page, fullPayload());
  assert.equal(pauseButton(page).disabled, true);
  pauseButton(page).fire('click');
  assert.deepEqual([view(page).userPaused, pauseButton(page).textContent, view(page).master.paused], [false, 'Pause', false]);

  await chooseReplay(page, 't1');
  assert.equal(pauseButton(page).disabled, false);

  await chooseReplay(page, '');
  assert.equal(pauseButton(page).disabled, true);
});

test('leaving a paused replay resumes the clock and disables pause', async () => {
  const page = newFactory();
  await openReplay(page, fullPayload(), 't1');
  pauseButton(page).fire('click');
  assert.deepEqual([view(page).userPaused, pauseButton(page).textContent, view(page).master.paused], [true, 'Play', true]);

  const current = await chooseReplay(page, '');

  assert.equal(current.userPaused, false);
  assert.equal(pauseButton(page).textContent, 'Pause');
  assert.equal(pauseButton(page).disabled, true);
  assert.deepEqual([current.master, ...current.scene.ambient, ...Object.values(current.scene.work).map(work => work.tween)].map(tween => tween.paused), [false, false, false]);
  assert.equal(statusText(page), 'Current state · 3 tasks on the floor');
});

test('speed stays enabled in both views', async () => {
  const page = newFactory();
  const speed = page.element('factory-speed');
  await openFactory(page, fullPayload());
  assert.equal(speed.disabled, false);
  speed.value = '2';
  speed.fire('change');
  assert.equal(view(page).speed, 2);

  const replay = await chooseReplay(page, 't1');
  assert.equal(speed.disabled, false);
  speed.value = '4';
  speed.fire('change');
  assert.equal(replay.speed, 4);
  assert.equal(replay.master.timeScales.at(-1), 4);
});

// ---- scope ----

test('a selection with no filter limits nothing', () => {
  const page = newFactory();

  for (const selection of [SELECTION, undefined, null, 'text', { ...SELECTION, scopedTasks: [{ id: 't1' }], hideCompleted: true }]) {
    const scope = scopeFrom(page, selection);
    assert.deepEqual([scope.limited, scope.key], [false, 'open'], JSON.stringify(selection));
  }
});

test('a filtered selection keeps only its tasks', () => {
  const page = newFactory();
  const filters = { p: 'proj', e: 'EPIC-1', taskId: 't1', team: 'BE', q: 'find' };

  for (const [name, value] of Object.entries(filters)) {
    const scope = scopeFrom(page, { ...SELECTION, [name]: value, scopedTasks: [{ id: 'b' }, { id: 'a' }, { id: '' }, { id: 7 }, null] });
    assert.deepEqual([scope.limited, scope.taskIds.sort()], [true, ['a', 'b']], name);
  }
  assert.equal(scopeFrom(page, { ...SELECTION, invalid: ['Project: x'] }).limited, true);
  const first = scopeFrom(page, scopeOf('b', 'a'));
  assert.equal(first.key, scopeFrom(page, scopeOf('a', 'b')).key, 'the key does not depend on the order');
  assert.notEqual(first.key, scopeFrom(page, scopeOf('a')).key);
});

test('with a filter set, a box whose task is missing from the selection is hidden', () => {
  const page = newFactory();

  assert.equal(boxVisible(page, scopeOf('t1'), 't1'), true);
  assert.equal(boxVisible(page, scopeOf('t1'), 't9'), false);
  assert.equal(boxVisible(page, SELECTION, 't9'), true);
  assert.equal(boxVisible(page, SELECTION, 't1', 't1'), true);
  assert.equal(boxVisible(page, SELECTION, 't9', 't1'), false);
});

test('a limited scope hides the boxes out of scope and the lanes without a task in scope', async () => {
  const page = newFactory();
  await openFactory(page, fullPayload());

  setScope(page, scopeOf('t3'));

  const current = view(page);
  assert.deepEqual(current.plan.boxes.map(item => item.taskId), ['t3']);
  assert.deepEqual(current.plan.lanes.map(item => item.sessionId), ['s2']);
  assert.deepEqual(sceneBoxIds(page), ['t3']);
  assert.equal(current.scene.lanes.length, 1);
  assert.equal(statusText(page), 'Current state · 1 task on the floor');
});

test('the replay list offers only the tasks in scope', async () => {
  const page = newFactory();
  await openFactory(page, fullPayload());

  setScope(page, scopeOf('t1', 't4'));

  assert.deepEqual(optionsOf(page).map(([value]) => value), ['', 't4', 't1']);
  setScope(page, SELECTION);
  assert.deepEqual(optionsOf(page).map(([value]) => value), ['', 't3', 't2', 't4', 't1']);
});

test('an invalid selection empties the floor and the replay list', async () => {
  const page = newFactory();
  await openFactory(page, fullPayload());

  setScope(page, { ...SELECTION, invalid: ['Project: gone'], scopedTasks: [] });

  const current = view(page);
  assert.deepEqual([current.plan.boxes.length, current.plan.lanes.length, current.scene.lanes.length], [0, 0, 0]);
  assert.deepEqual(optionsOf(page), [['', 'Current state']]);
  assert.equal(statusText(page), 'Current state · 0 tasks on the floor');
});

test('a scope set before the factory opens limits the first floor and the replay list', async () => {
  const page = newFactory();
  setScope(page, scopeOf('t3'));
  assert.equal(page.requests.length, 0);

  await openFactory(page, fullPayload());

  assert.deepEqual(sceneBoxIds(page), ['t3']);
  assert.equal(view(page).scene.lanes.length, 1);
  assert.deepEqual(optionsOf(page).map(([value]) => value), ['', 't3']);
  assert.equal(statusText(page), 'Current state · 1 task on the floor');
});

test('a scope change shows the current state without animation', async () => {
  const page = newFactory();
  const payload = fullPayload();
  await openFactory(page, { ...payload, events: payload.events.slice(0, 5) });
  await refreshWith(page, payload);
  assert.equal(view(page).master.children().length, 9);

  setScope(page, scopeOf('t1', 't2', 't3', 't4'));

  const current = view(page);
  assert.equal(current.master.children().length, 0);
  assert.equal(current.playedIds.size, 14);
  assert.equal(statusText(page), 'Current state · 3 tasks on the floor');
});

test('an unchanged scope does not rebuild the scene', async () => {
  const page = newFactory();
  await openFactory(page, fullPayload());
  const open = view(page).scene;

  setScope(page, SELECTION);
  assert.equal(view(page).scene, open);
  setScope(page, scopeOf('t1', 't3'));
  const limited = view(page).scene;
  assert.notEqual(limited, open);
  setScope(page, scopeOf('t3', 't1'));
  assert.equal(view(page).scene, limited);
});

test('a scope change that excludes the replayed task returns to the current state', async () => {
  const page = newFactory();
  await openReplay(page, fullPayload(), 't1');

  setScope(page, scopeOf('t3'));

  const current = view(page);
  assert.equal(current.replayId, '');
  assert.equal(current.master.children().length, 0);
  assert.equal(statusText(page), 'Current state · 1 task on the floor');
  assert.equal(page.element('factory-replay').value, '');
  assert.deepEqual(optionsOf(page).map(([value]) => value), ['', 't3']);
  assert.equal(pauseButton(page).disabled, true);
});

test('a scope change during a replay that keeps its task lets the replay go on', async () => {
  const page = newFactory();
  const replay = await openReplay(page, fullPayload(), 't1');
  replay.master.children()[0].finish();
  const { scene, master } = replay;

  setScope(page, scopeOf('t1', 't3'));

  const current = view(page);
  assert.deepEqual([current.scene === scene, current.master === master, master.killed], [true, true, false]);
  assert.equal(current.replayId, 't1');
  assert.equal(current.playedIds.size, 1);
  assert.equal(page.element('factory-replay').value, 't1');
  assert.deepEqual(optionsOf(page).map(([value]) => value), ['', 't3', 't1']);
  assert.equal(statusText(page), 'Replay · Title of t1 · event 1 of 3');
});

test('a refresh in a limited scope animates only the events of the tasks in scope', async () => {
  const page = newFactory();
  const payload = fullPayload();
  await openFactory(page, { ...payload, events: payload.events.slice(0, 5) });
  setScope(page, scopeOf('t3'));

  await refreshWith(page, payload);

  const current = view(page);
  assert.equal(current.master.children().length, 1, 'only the refusal of t3 is recorded for a task in scope');
  assert.equal(current.playedIds.has('t4/refuse/report'), true, 'the events of the tasks out of scope count as played');
  assert.equal(current.playedIds.has('t3/refuse/report'), false);
  assert.equal(toEntries(current.master.children()[0], current.scene.office.owner).length > 0, true, 'the owner rages at the refusal');
});

// ---- work animation follows the worker's recorded status ----

const NOT_WORKING = [['waiting', { status: 'waiting', at: '2026-11-28T09:00:00Z' }], ['idle', { status: 'idle', at: '2026-11-28T09:00:00Z' }], ['blocked', { status: 'blocked', at: '2026-11-28T09:00:00Z' }], ['nothing', null]];

function oneBoxPayload(lastStatus, specs) {
  return payloadOf([lane('s1', 'alpha', 'Ada', lastStatus)], [box('t1', 's1', 'implementation')], specs);
}

const PATH = [
  ['t1/stage/investigating', 't1', 'enter', 'investigating'],
  ['t1/stage/implementation', 't1', 'move', 'implementation'],
  ['t1/refuse/x', 't1', 'refuse', 'implementation', 'g1']
];

test('a box on a lane that reports working shows the work animation', async () => {
  const page = newFactory();

  const current = await openFactory(page, oneBoxPayload(WORKING, PATH.slice(0, 2)));

  const post = current.scene.lanes[0].robots[1];
  assert.deepEqual(workOf(page), ['t1']);
  assert.equal(current.scene.work.t1.glyph.text, '</>');
  assert.deepEqual(pointOf(current.scene.work.t1.glyph), pointOf(post));
});

test('a box on a lane that reports waiting, idle, blocked or nothing stands without the work animation', async t => {
  for (const [name, lastStatus] of NOT_WORKING) {
    await t.test(name, async () => {
      const page = newFactory();
      const current = await openFactory(page, oneBoxPayload(lastStatus, PATH.slice(0, 2)));
      current.master.finish();

      assert.deepEqual(pointOf(current.scene.boxes.t1), slotPoint(page, 0, 1, 0));
      assert.deepEqual(workOf(page), []);
    });
  }
});

test('a refresh in the current state that plays an enter, move or refuse step on a lane that does not report working starts no work animation', async t => {
  for (const [name, lastStatus] of [['working', WORKING], ...NOT_WORKING]) {
    await t.test(name, async () => {
      const page = newFactory();
      await openFactory(page, oneBoxPayload(lastStatus, []));
      await refreshWith(page, oneBoxPayload(lastStatus, PATH));
      const steps = view(page).master.children();
      assert.equal(steps.length, 3);
      const expected = lastStatus && lastStatus.status === 'working' ? ['t1'] : [];

      for (const step of steps) {
        step.finish();
        assert.deepEqual(workOf(page), expected);
      }
    });
  }
});

test('a replay shows the work animation whatever the lane reports', async t => {
  for (const [name, lastStatus] of NOT_WORKING) {
    await t.test(name, async () => {
      const page = newFactory();
      const replay = await openReplay(page, oneBoxPayload(lastStatus, PATH), 't1');

      for (const step of replay.master.children()) {
        step.finish();
        assert.deepEqual(workOf(page), ['t1']);
      }
    });
  }
});
