'use strict';
// Factory tab. Classic script loaded before app.js: at load time it only declares.
// PIXI and gsap are reached through globals at call time, after loadFactoryLibraries().

const factoryStations = ['investigating', 'implementation', 'review', 'verification'];
const factoryKinds = ['enter', 'move', 'refuse', 'exit'];
const factoryGates = ['g1', 'g2', 'report'];
const factoryLastStatuses = ['working', 'idle', 'waiting', 'blocked'];
const factorySpeeds = [0.5, 1, 2, 4];
const factoryStepSeconds = 1;
const factoryLibraries = ['/vendor/pixi-8.23.0.min.js', '/vendor/pixi-unsafe-eval-8.23.0.min.js', '/vendor/gsap-3.15.1.min.js'];
const factoryTexts = {
  loading: 'Loading factory…',
  dataFailed: 'Factory data could not be loaded.',
  libraryFailed: 'Factory animation could not be loaded.',
  notRecorded: 'not recorded',
  pausedPrefix: 'Paused · ',
  currentState: 'Current state',
  replay: 'Replay',
  taskOnFloor: 'task on the floor',
  tasksOnFloor: 'tasks on the floor',
  pauseButton: 'Pause',
  playButton: 'Play',
  roles: ['worker', 'writer + tests', 'reviewer', 'verifier']
};
const factoryPalettes = {
  light: {
    background: 0xf9fbfd, belt: 0x9aa8b6, beltEdge: 0x6b7b8c, pad: 0xdde6ee, text: 0x253e51, box: 0xd9a441, boxEdge: 0x7a5a16,
    code: 0x1f6f43, robots: [0x5b8def, 0x4fb286, 0x9a7bd8, 0xe28a4f, 0x3aa3b8], inspector: 0xc94f4f, owner: 0x7a5ea8,
    productOwner: 0xe0b13a, driver: 0x5b6b7c, edge: 0x243d52, eye: 0xffffff, accent: 0xf2c230, car: 0x4f7fd6, carWindow: 0xcfe3f7,
    door: 0xb7c4d1, office: 0xe9eef4, officeEdge: 0x8a9bad, money: 0x2f8f5b, dollar: 0x1f7a46, rage: 0xc23b2d
  },
  dark: {
    background: 0x101c28, belt: 0x4b6581, beltEdge: 0x7f9bb8, pad: 0x1b3045, text: 0xcee1f2, box: 0xe0a94a, boxEdge: 0x8a6a22,
    code: 0x7ee2a8, robots: [0x75a4ff, 0x64c9a0, 0xb59bf0, 0xf0a066, 0x59c0d4], inspector: 0xe66b6b, owner: 0xa58ad6,
    productOwner: 0xf0c75a, driver: 0x8fa2b6, edge: 0x0b1520, eye: 0xffffff, accent: 0xf2c230, car: 0x6c9bf0, carWindow: 0x1b3045,
    door: 0x3a4f66, office: 0x1b3045, officeEdge: 0x4b6581, money: 0x58c58a, dollar: 0x7ee2a8, rage: 0xff7a6b
  }
};
const factoryView = {
  shown: false, opening: false, opened: false, userPaused: false, speed: 1, ticket: 0,
  app: null, full: null, scope: { limited: false, taskIds: new Set(), key: 'open' }, replayId: '', plan: null, scene: null, layout: null, state: [], master: null,
  playedIds: new Set(), playedCount: 0, width: 0, height: 0, failure: ''
};
const factoryScriptLoads = {};

// ---- pure: plan, state, rules, texts, layout, colours ----

function factoryText(value) {
  return typeof value === 'string' && value.trim() !== '' ? value : null;
}

function factoryList(value) {
  return Array.isArray(value) ? value : [];
}

function factoryClamp(value, low, high) {
  return Math.min(high, Math.max(low, value));
}

function factoryUniqueBy(items, keyOf) {
  const seen = new Set();
  return items.filter(item => {
    const key = keyOf(item);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function factoryLastStatus(value) {
  if (!value || typeof value !== 'object') return null;
  const at = factoryText(value.at);
  return factoryLastStatuses.includes(value.status) && at ? { status: value.status, at } : null;
}

function factoryLane(item) {
  if (!item || typeof item !== 'object') return null;
  const sessionId = factoryText(item.session_id);
  const project = factoryText(item.project);
  if (!sessionId || !project) return null;
  return { sessionId, project, name: factoryText(item.session_name) || sessionId, lastStatus: factoryLastStatus(item.last_status) };
}

function factoryBox(item, laneIds) {
  if (!item || typeof item !== 'object') return null;
  const taskId = factoryText(item.task_id);
  const title = factoryText(item.title);
  const project = factoryText(item.project);
  const laneId = factoryText(item.session_id);
  if (!taskId || !title || !project || !laneIds.has(laneId)) return null;
  return { taskId, title, project, laneId, stage: factoryText(item.stage) || '' };
}

function factoryStep(item, taskIds) {
  if (!item || typeof item !== 'object') return null;
  const id = factoryText(item.id);
  const at = factoryText(item.at);
  const taskId = factoryText(item.task_id);
  const gate = item.kind === 'refuse' ? item.gate : null;
  const valid = id && at && taskIds.has(taskId) && factoryKinds.includes(item.kind) && factoryStations.includes(item.stage)
    && (item.kind !== 'refuse' || factoryGates.includes(gate));
  return valid ? { id, at, taskId, kind: item.kind, stage: item.stage, gate } : null;
}

function factoryInitialState(boxes) {
  return boxes.map(box => ({ taskId: box.taskId, laneId: box.laneId, station: null, slot: null, gone: false }));
}

function factoryFindBox(state, taskId) {
  return state.find(box => box.taskId === taskId) || null;
}

function factoryFreeSlot(state, laneId, station) {
  const taken = new Set(state.filter(box => box.laneId === laneId && box.station === station).map(box => box.slot));
  let slot = 0;
  while (taken.has(slot)) slot += 1;
  return slot;
}

function factoryBoxAfter(box, step, state) {
  if (step.kind === 'exit') return { ...box, station: null, slot: null, gone: true };
  if (step.kind === 'refuse') return box;
  const station = factoryStations.indexOf(step.stage);
  const others = state.filter(other => other.taskId !== box.taskId);
  return { ...box, station, slot: factoryFreeSlot(others, box.laneId, station) };
}

function factoryAdvance(state, step) {
  return state.map(box => (box.taskId === step.taskId ? factoryBoxAfter(box, step, state) : box));
}

function factorySlotPoint(layout, laneIndex, station, slot) {
  const spot = layout.lanes[laneIndex].stations[station];
  const { columns, stepX, stepY } = layout.queue;
  const column = slot % columns;
  const row = Math.floor(slot / columns);
  return { x: spot.x + (column - (columns - 1) / 2) * stepX, y: Math.min(spot.y + row * stepY, layout.height - layout.margin) };
}

function factoryStepApplies(state, step) {
  const box = factoryFindBox(state, step.taskId);
  if (!box || box.gone) return false;
  return step.kind === 'enter' ? box.station === null : box.station !== null;
}

function factoryConsistentSteps(candidates, boxes) {
  let state = factoryInitialState(boxes);
  return candidates.filter(step => {
    if (!factoryStepApplies(state, step)) return false;
    state = factoryAdvance(state, step);
    return true;
  });
}

function factoryPlan(payload) {
  const source = payload && typeof payload === 'object' ? payload : {};
  const lanes = factoryUniqueBy(factoryList(source.lanes).map(factoryLane).filter(Boolean), lane => lane.sessionId);
  const laneIds = new Set(lanes.map(lane => lane.sessionId));
  const boxes = factoryUniqueBy(factoryList(source.boxes).map(item => factoryBox(item, laneIds)).filter(Boolean), box => box.taskId);
  const taskIds = new Set(boxes.map(box => box.taskId));
  const candidates = factoryUniqueBy(factoryList(source.events).map(item => factoryStep(item, taskIds)).filter(Boolean), step => step.id);
  return { mode: factoryText(source.mode) || '', stations: factoryStations, lanes, boxes, steps: factoryConsistentSteps(candidates, boxes) };
}

function factoryScopeFrom(selection) {
  const source = selection && typeof selection === 'object' ? selection : {};
  const taskIds = new Set(factoryList(source.scopedTasks).map(task => factoryText(task && task.id)).filter(Boolean));
  const valued = [source.p, source.e, source.taskId, source.team, source.q].some(value => typeof value === 'string' && value !== '');
  const limited = valued || factoryList(source.invalid).length > 0;
  return { limited, taskIds, key: limited ? JSON.stringify([...taskIds].sort()) : 'open' };
}

function factoryBoxVisible(box, scope, replayId) {
  if (replayId) return box.taskId === replayId;
  return !scope.limited || scope.taskIds.has(box.taskId);
}

function factoryVisiblePlan(full, scope, replayId) {
  const boxes = full.boxes.filter(box => factoryBoxVisible(box, scope, replayId));
  const taskIds = new Set(boxes.map(box => box.taskId));
  const laneIds = new Set(boxes.map(box => box.laneId));
  const narrowed = scope.limited || replayId !== '';
  return {
    stations: full.stations,
    lanes: narrowed ? full.lanes.filter(lane => laneIds.has(lane.sessionId)) : full.lanes,
    boxes,
    steps: full.steps.filter(step => taskIds.has(step.taskId))
  };
}

function factoryLastStepIndex(full, taskId) {
  return full.steps.reduce((found, step, index) => (step.taskId === taskId ? index : found), -1);
}

function factoryReplayOptions(full, scope) {
  return full.boxes
    .filter(box => factoryBoxVisible(box, scope, ''))
    .map(box => ({ value: box.taskId, label: box.title + ' · ' + box.taskId.slice(0, 8), recency: factoryLastStepIndex(full, box.taskId) }))
    .sort((first, second) => second.recency - first.recency);
}

function factoryReplayAvailable(full, scope, taskId) {
  return factoryReplayOptions(full, scope).some(option => option.value === taskId);
}

function factoryIsFirstLoad(floorPlan, payloadPlan) {
  return !floorPlan || floorPlan.mode !== payloadPlan.mode;
}

function factoryWorkLanes(plan, replayId) {
  const working = lane => replayId !== '' || (lane.lastStatus !== null && lane.lastStatus.status === 'working');
  return new Set(plan.lanes.flatMap((lane, index) => (working(lane) ? [index] : [])));
}

function factoryOnFloor(state) {
  return state.filter(box => box.station !== null).length;
}

function factoryStateAfter(plan, count) {
  const taken = plan.steps.slice(0, Math.max(0, Math.floor(count) || 0));
  return taken.reduce(factoryAdvance, factoryInitialState(plan.boxes));
}

function factorySplitSteps(plan, playedIds) {
  return {
    played: plan.steps.filter(step => playedIds.has(step.id)),
    fresh: plan.steps.filter(step => !playedIds.has(step.id))
  };
}

function factoryRefusalCheckpoint(gate, station) {
  if (gate === 'g1' || gate === 'g2') return gate;
  return station <= 1 ? 'g1' : 'g2';
}

function factoryFloorStatusText(onFloor) {
  const unit = onFloor === 1 ? factoryTexts.taskOnFloor : factoryTexts.tasksOnFloor;
  return factoryTexts.currentState + ' · ' + onFloor + ' ' + unit;
}

function factoryReplayStatusText(title, played, total, paused) {
  const progress = factoryTexts.replay + ' · ' + title + ' · event ' + played + ' of ' + total;
  return paused ? factoryTexts.pausedPrefix + progress : progress;
}

function factoryBoxTooltip(box, stage) {
  return box.title + '\nProject: ' + box.project + '\nStage: ' + stage;
}

function factoryRobotTooltip(lane) {
  const last = lane.lastStatus ? lane.lastStatus.status + ' (' + lane.lastStatus.at + ')' : factoryTexts.notRecorded;
  return 'Session: ' + lane.name + '\nProject: ' + lane.project + '\nLast status: ' + last;
}

function factoryTooltipPosition(clientX, clientY, tipWidth, tipHeight, viewWidth, viewHeight) {
  const gap = 14;
  const left = clientX + gap + tipWidth > viewWidth ? clientX - gap - tipWidth : clientX + gap;
  const top = clientY + gap + tipHeight > viewHeight ? clientY - gap - tipHeight : clientY + gap;
  return { left: Math.max(0, left), top: Math.max(0, top) };
}

function factoryPalette(theme) {
  return theme === 'light' ? factoryPalettes.light : factoryPalettes.dark;
}

function factorySpeedFrom(value) {
  const speed = Number(value);
  return factorySpeeds.includes(speed) ? speed : 1;
}

function factoryLaneSlots(frame, index) {
  const { beltStart, slot, lanesTop, pitch, scale, font } = frame;
  const laneTop = lanesTop + pitch * index;
  const y = laneTop + pitch * 0.74;
  const point = k => ({ x: beltStart + (k + 0.5) * slot, y, robotY: y - 18 * scale });
  return {
    belt: { x1: beltStart, x2: beltStart + 6 * slot, y, height: 6 * scale },
    label: { x: beltStart, y: laneTop + font * 0.5 + 4 },
    stations: [0, 2, 3, 5].map(point),
    checkpoints: { g1: point(1), g2: point(4) }
  };
}

function factoryLayout(laneCount, width, height) {
  const margin = factoryClamp(width * 0.02, 6, 16);
  const top = Math.min(110, height * 0.2);
  const lanes = Math.max(0, Math.floor(laneCount) || 0);
  const entranceWidth = factoryClamp(width * 0.08, 28, 64);
  const doorWidth = factoryClamp(width * 0.16, 44, 110);
  const beltStart = margin + entranceWidth;
  const beltEnd = width - margin - doorWidth;
  const slot = Math.max(1, (beltEnd - beltStart) / 6);
  const pitch = lanes ? Math.min(300, (height - margin - top) / lanes) : 0;
  const scale = factoryClamp(Math.min(slot / 70, lanes ? pitch / 110 : 1), 0.3, 2.2);
  const font = factoryClamp(6 * scale, 11, 14);
  const boxSize = 16 * scale;
  const queueStep = boxSize * 1.25;
  const frame = { beltStart, slot, lanesTop: top, pitch, scale, font };
  const middle = lanes ? top + (pitch * lanes) / 2 : (top + height - margin) / 2;
  const carWidth = factoryClamp(doorWidth * 0.55, 24, 64);
  const officeWidth = Math.min(200, width * 0.46);
  const officeHeight = top - margin * 2;
  const officeX = width - margin - officeWidth;
  return {
    width, height, scale, font, boxSize, margin, hold: 14 * scale,
    compact: slot < 80 || (lanes > 0 && pitch < 80),
    queue: { columns: Math.max(1, Math.floor(slot / queueStep)), stepX: queueStep, stepY: queueStep },
    lanes: Array.from({ length: lanes }, (_, index) => factoryLaneSlots(frame, index)),
    entrance: { x: margin + entranceWidth / 2, y: middle },
    door: { x: beltEnd + doorWidth * 0.18, y: middle, width: doorWidth * 0.2, height: 56 * scale },
    car: { x: width - margin - carWidth / 2, y: middle, width: carWidth, height: carWidth * 0.5 },
    office: {
      x: officeX, y: margin, width: officeWidth, height: officeHeight,
      owner: { x: officeX + officeWidth * 0.3, y: margin + officeHeight * 0.62 },
      moneyBox: { x: officeX + officeWidth * 0.74, y: margin + officeHeight * 0.72 }
    }
  };
}

// ---- libraries ----

function factoryLoadScript(src) {
  if (!factoryScriptLoads[src]) {
    factoryScriptLoads[src] = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = src;
      script.onload = resolve;
      script.onerror = () => {
        delete factoryScriptLoads[src];
        script.remove();
        reject(new Error('script'));
      };
      document.head.appendChild(script);
    });
  }
  return factoryScriptLoads[src];
}

function loadFactoryLibraries() {
  return factoryLibraries.reduce((previous, src) => previous.then(() => factoryLoadScript(src)), Promise.resolve());
}

// ---- drawing primitives: each takes what it draws ----

function factoryNewText(content, size, color) {
  const text = new PIXI.Text({ text: content, style: { fontFamily: 'sans-serif', fontSize: size, fill: color } });
  text.anchor.set(0.5);
  return text;
}

function factoryPlace(display, x, y) {
  display.x = x;
  display.y = y;
  return display;
}

function factoryContainerAt(parent, x, y) {
  const container = new PIXI.Container();
  factoryPlace(container, x, y);
  parent.addChild(container);
  return container;
}

function factoryGraphicsIn(parent) {
  const graphics = new PIXI.Graphics();
  parent.addChild(graphics);
  return graphics;
}

function factoryLabelAt(parent, content, x, y, size, color) {
  const text = factoryNewText(content, size, color);
  parent.addChild(factoryPlace(text, x, y));
  return text;
}

function factoryDrawRobot(graphics, size, body, palette) {
  graphics.roundRect(-size * 0.28, -size * 0.08, size * 0.56, size * 0.46, size * 0.08).fill(body).stroke({ width: 1, color: palette.edge });
  graphics.roundRect(-size * 0.3, -size * 0.5, size * 0.6, size * 0.36, size * 0.1).fill(body).stroke({ width: 1, color: palette.edge });
  graphics.circle(-size * 0.12, -size * 0.32, size * 0.06).fill(palette.eye);
  graphics.circle(size * 0.12, -size * 0.32, size * 0.06).fill(palette.eye);
  graphics.moveTo(0, -size * 0.5).lineTo(0, -size * 0.62).stroke({ width: 1, color: palette.edge });
  graphics.circle(0, -size * 0.64, size * 0.05).fill(palette.accent);
}

function factoryDrawBadge(graphics, size, palette) {
  graphics.circle(0, size * 0.1, size * 0.08).fill(palette.accent).stroke({ width: 1, color: palette.edge });
}

function factoryDrawClipboard(graphics, size, palette) {
  graphics.rect(size * 0.3, -size * 0.05, size * 0.3, size * 0.4).fill(palette.pad).stroke({ width: 1, color: palette.edge });
}

function factoryDrawBox(graphics, size, palette) {
  graphics.rect(-size / 2, -size / 2, size, size).fill(palette.box).stroke({ width: 1, color: palette.boxEdge });
  graphics.moveTo(-size / 2, 0).lineTo(size / 2, 0).stroke({ width: 1, color: palette.boxEdge });
}

function factoryDrawBelt(graphics, belt, palette) {
  graphics.rect(belt.x1, belt.y - belt.height / 2, belt.x2 - belt.x1, belt.height).fill(palette.belt).stroke({ width: 1, color: palette.beltEdge });
}

function factoryDrawPad(graphics, point, size, palette) {
  graphics.roundRect(point.x - size * 0.5, point.y - size * 0.1, size, size * 0.2, 3).fill(palette.pad).stroke({ width: 1, color: palette.beltEdge });
}

function factoryDrawPanel(graphics, area, fill, edge) {
  graphics.roundRect(area.x, area.y, area.width, area.height, 6).fill(fill).stroke({ width: 1, color: edge });
}

function factoryRobotIn(parent, x, y, size, body, palette) {
  const holder = factoryContainerAt(parent, x, y);
  factoryDrawRobot(factoryGraphicsIn(holder), size, body, palette);
  return holder;
}

// ---- scene construction ----

function factoryBindTooltip(display, textOf) {
  display.eventMode = 'static';
  display.on('pointerover', event => factoryShowTooltip(textOf(), event));
  display.on('pointermove', factoryMoveTooltip);
  display.on('pointerout', factoryHideTooltip);
}

function factoryLabelAbove(robotSize, font) {
  return -robotSize * 0.69 - font * 0.7;
}

function factoryStationRobot(parent, point, layout, role, palette) {
  const size = 34 * layout.scale;
  const holder = factoryContainerAt(parent, point.x, point.robotY);
  const colors = palette.robots;
  const bodies = role === 1 ? [colors[1], colors[2]] : [colors[role === 0 ? 0 : role + 1]];
  bodies.forEach((body, index) => {
    const shift = bodies.length === 1 ? 0 : (index - 0.5) * size * 0.6;
    const graphics = factoryGraphicsIn(holder);
    graphics.x = shift;
    factoryDrawRobot(graphics, bodies.length === 1 ? size : size * 0.8, body, palette);
  });
  if (!layout.compact) factoryLabelAt(holder, factoryTexts.roles[role], 0, factoryLabelAbove(size, layout.font), layout.font, palette.text);
  return holder;
}

function factoryInspectorRobot(parent, point, layout, gate, palette) {
  const size = 34 * layout.scale;
  const holder = factoryContainerAt(parent, point.x, point.robotY);
  const graphics = factoryGraphicsIn(holder);
  factoryDrawRobot(graphics, size, palette.inspector, palette);
  factoryDrawClipboard(graphics, size, palette);
  factoryLabelAt(holder, gate.toUpperCase(), 0, factoryLabelAbove(size, layout.font), layout.font, palette.text);
  return holder;
}

function factoryBuildLane(root, lane, laneLayout, layout, palette) {
  const graphics = factoryGraphicsIn(root);
  factoryDrawBelt(graphics, laneLayout.belt, palette);
  laneLayout.stations.forEach(point => factoryDrawPad(graphics, point, 30 * layout.scale, palette));
  const label = factoryLabelAt(root, lane.name + ' · ' + lane.project, laneLayout.belt.x1, laneLayout.label.y, layout.font, palette.text);
  label.anchor.set(0, 0.5);
  const robots = laneLayout.stations.map((point, role) => factoryStationRobot(root, point, layout, role, palette));
  const inspectors = {};
  ['g1', 'g2'].forEach(gate => {
    inspectors[gate] = factoryInspectorRobot(root, laneLayout.checkpoints[gate], layout, gate, palette);
  });
  [...robots, ...Object.values(inspectors)].forEach(robot => factoryBindTooltip(robot, () => factoryRobotTooltip(lane)));
  return { robots, inspectors };
}

function factoryBuildEntrance(root, layout, palette) {
  const graphics = factoryGraphicsIn(root);
  const gate = { x: layout.entrance.x - 10, y: layout.entrance.y - 28 * layout.scale, width: 20, height: 56 * layout.scale };
  factoryDrawPanel(graphics, gate, palette.door, palette.beltEdge);
  factoryLabelAt(root, 'IN', layout.entrance.x, gate.y - layout.font, layout.font, palette.text);
  const owner = factoryRobotIn(root, layout.entrance.x, layout.entrance.y, 34 * layout.scale, palette.productOwner, palette);
  factoryLabelAt(owner, '!', 0, -34 * layout.scale, layout.font + 2, palette.rage);
  return owner;
}

function factoryBuildDoor(root, layout, palette) {
  const door = layout.door;
  const graphics = factoryGraphicsIn(root);
  factoryDrawPanel(graphics, { x: door.x - door.width / 2, y: door.y - door.height / 2, width: door.width, height: door.height }, palette.door, palette.beltEdge);
  factoryLabelAt(root, 'OUT', door.x, door.y - door.height / 2 - layout.font, layout.font, palette.text);
}

function factoryBuildCar(root, layout, palette) {
  const { width, height } = layout.car;
  const car = factoryContainerAt(root, layout.width + width * 1.5, layout.car.y);
  const graphics = factoryGraphicsIn(car);
  graphics.roundRect(-width / 2, -height / 2, width, height, height * 0.2).fill(palette.car).stroke({ width: 1, color: palette.edge });
  graphics.rect(-width * 0.05, -height * 0.4, width * 0.3, height * 0.35).fill(palette.carWindow);
  graphics.circle(-width * 0.3, height / 2, height * 0.2).fill(palette.edge);
  graphics.circle(width * 0.3, height / 2, height * 0.2).fill(palette.edge);
  const driver = factoryContainerAt(car, width * 0.1, -height * 0.5);
  const driverGraphics = factoryGraphicsIn(driver);
  factoryDrawRobot(driverGraphics, height * 0.9, palette.driver, palette);
  factoryDrawBadge(driverGraphics, height * 0.9, palette);
  return car;
}

function factoryBuildOffice(root, layout, palette) {
  const { office } = layout;
  factoryDrawPanel(factoryGraphicsIn(root), office, palette.office, palette.officeEdge);
  factoryLabelAt(root, 'OFFICE', office.x + office.width * 0.5, office.y + layout.font, layout.font, palette.text);
  const ownerSize = Math.min(office.height * 0.5, 44);
  const owner = factoryRobotIn(root, office.owner.x, office.owner.y, ownerSize, palette.owner, palette);
  const hands = [-1, 1].map(side => {
    const hand = factoryContainerAt(owner, side * ownerSize * 0.3, ownerSize * 0.15);
    factoryGraphicsIn(hand).circle(0, 0, ownerSize * 0.07).fill(palette.accent);
    return hand;
  });
  const moneyBox = factoryContainerAt(root, office.moneyBox.x, office.moneyBox.y);
  factoryDrawPanel(factoryGraphicsIn(moneyBox), { x: -ownerSize * 0.4, y: -ownerSize * 0.25, width: ownerSize * 0.8, height: ownerSize * 0.5 }, palette.money, palette.edge);
  factoryLabelAt(moneyBox, '$', 0, 0, 14, palette.eye);
  const mood = (content, color) => {
    const text = factoryLabelAt(root, content, office.owner.x, office.owner.y - ownerSize * 0.85, 12, color);
    text.alpha = 0;
    return text;
  };
  const dollars = [-1, 0, 1].map(offset => {
    const dollar = factoryLabelAt(root, '$', office.moneyBox.x + offset * 9, office.moneyBox.y, 14, palette.dollar);
    dollar.alpha = 0;
    return dollar;
  });
  return { owner, ownerSize, hands, handRest: hands.map(hand => hand.x), cheer: mood('$$$', palette.dollar), rage: mood('#@!', palette.rage), dollars, moneyBox: office.moneyBox };
}

function factoryBuildBox(root, box, layout, palette) {
  const holder = factoryContainerAt(root, layout.entrance.x, layout.entrance.y);
  factoryDrawBox(factoryGraphicsIn(holder), layout.boxSize, palette);
  holder.visible = false;
  factoryBindTooltip(holder, () => factoryBoxTooltip(box, factoryCurrentStage(box)));
  return holder;
}

function factoryCurrentStage(box) {
  const entry = factoryFindBox(factoryView.state, box.taskId);
  return entry && entry.station !== null ? factoryStations[entry.station] : box.stage;
}

function factoryBuildScene(app, plan, layout, palette, workLanes) {
  const root = new PIXI.Container();
  app.stage.addChild(root);
  factoryDrawPanel(factoryGraphicsIn(root), { x: 0, y: 0, width: layout.width, height: layout.height }, palette.background, palette.background);
  const lanes = plan.lanes.map((lane, index) => factoryBuildLane(root, lane, layout.lanes[index], layout, palette));
  const productOwner = factoryBuildEntrance(root, layout, palette);
  factoryBuildDoor(root, layout, palette);
  const office = factoryBuildOffice(root, layout, palette);
  const car = factoryBuildCar(root, layout, palette);
  const refuseMark = factoryLabelAt(root, 'X', 0, 0, 16, palette.rage);
  refuseMark.alpha = 0;
  const boxes = {};
  plan.boxes.forEach(box => { boxes[box.taskId] = factoryBuildBox(root, box, layout, palette); });
  const fidget = gsap.to(productOwner, { rotation: 0.14, duration: 0.15, repeat: -1, yoyo: true, ease: 'none' });
  return { root, palette, lanes, productOwner, office, car, refuseMark, boxes, workLanes, work: {}, ambient: [fidget] };
}

function factoryDisposeScene(scene) {
  if (!scene) return;
  Object.keys(scene.work).forEach(taskId => factoryStopWork(scene, taskId));
  scene.ambient.forEach(tween => tween.kill());
  scene.root.destroy({ children: true });
}

// ---- work loops ----

function factoryStartWork(scene, layout, taskId, place) {
  factoryStopWork(scene, taskId);
  const post = scene.lanes[place.laneIndex].robots[place.station];
  const spot = factorySlotPoint(layout, place.laneIndex, place.station, place.slot);
  const glyph = factoryLabelAt(scene.root, '</>', post.x, post.y, Math.max(11, 8 * layout.scale), scene.palette.code);
  const tween = gsap.to(glyph, { x: spot.x, y: spot.y, duration: 0.5, repeat: -1, yoyo: true, ease: 'none' });
  scene.work[taskId] = { glyph, tween };
  factoryApplyClockTo(tween);
}

function factoryStartLaneWork(scene, layout, taskId, place) {
  if (scene.workLanes.has(place.laneIndex)) factoryStartWork(scene, layout, taskId, place);
}

function factoryStopWork(scene, taskId) {
  const work = scene.work[taskId];
  if (!work) return;
  work.tween.kill();
  work.glyph.destroy();
  delete scene.work[taskId];
}

// ---- steps: one timeline of factoryStepSeconds per event ----

function factoryRideTo(timeline, display, point, start, duration) {
  timeline.to(display, { x: point.x, y: point.y, duration }, start);
}

function factoryFlowDollars(timeline, scene, direction, start) {
  const { dollars, moneyBox } = scene.office;
  dollars.forEach((dollar, index) => {
    const outside = moneyBox.y - 48;
    const from = direction === 'in' ? outside : moneyBox.y;
    const to = direction === 'in' ? moneyBox.y : outside;
    const at = start + index * 0.08;
    timeline.set(dollar, { y: from, alpha: 1 }, at);
    timeline.to(dollar, { y: to, alpha: 0, duration: 0.35 }, at);
  });
}

function factoryCheerOwner(timeline, scene, start) {
  const { hands, handRest, cheer } = scene.office;
  hands.forEach((hand, index) => {
    const reach = index === 0 ? 4 : -4;
    timeline.to(hand, { x: handRest[index] + reach, duration: 0.08, repeat: 5, yoyo: true }, start);
  });
  timeline.to(cheer, { alpha: 1, duration: 0.1 }, start);
  timeline.to(cheer, { alpha: 0, duration: 0.2 }, start + 0.35);
}

function factoryRageOwner(timeline, scene, start) {
  const { owner, rage } = scene.office;
  const home = owner.x;
  timeline.to(owner, { x: home + 3, duration: 0.05, repeat: 7, yoyo: true }, start);
  timeline.set(owner, { x: home }, start + 0.45);
  timeline.to(rage, { alpha: 1, duration: 0.1 }, start);
  timeline.to(rage, { alpha: 0, duration: 0.2 }, start + 0.4);
}

function factoryEnterStep(job) {
  const { scene, layout, step, laneIndex, slot } = job;
  const timeline = gsap.timeline();
  const box = scene.boxes[step.taskId];
  const station = factoryStations.indexOf(step.stage);
  const target = factorySlotPoint(layout, laneIndex, station, slot);
  const entrance = layout.entrance;
  timeline.call(() => { box.visible = true; factoryPlace(box, entrance.x + layout.hold, entrance.y); }, null, 0);
  factoryRideTo(timeline, scene.productOwner, { x: target.x - layout.hold, y: target.y }, 0, 0.5);
  factoryRideTo(timeline, box, target, 0, 0.5);
  timeline.call(() => factoryStartLaneWork(scene, layout, step.taskId, { laneIndex, station, slot }), null, 0.5);
  factoryRideTo(timeline, scene.productOwner, entrance, 0.5, 0.5);
  return timeline;
}

function factoryMoveStep(job) {
  const { scene, layout, step, laneIndex, slot } = job;
  const timeline = gsap.timeline();
  const station = factoryStations.indexOf(step.stage);
  timeline.call(() => factoryStopWork(scene, step.taskId), null, 0);
  factoryRideTo(timeline, scene.boxes[step.taskId], factorySlotPoint(layout, laneIndex, station, slot), 0, 0.85);
  timeline.call(() => factoryStartLaneWork(scene, layout, step.taskId, { laneIndex, station, slot }), null, 0.85);
  return timeline;
}

function factoryRefuseStep(job) {
  const { scene, layout, step, laneIndex, fromStation, fromSlot } = job;
  const timeline = gsap.timeline();
  const laneLayout = layout.lanes[laneIndex];
  const gate = factoryRefusalCheckpoint(step.gate, fromStation);
  const checkpoint = laneLayout.checkpoints[gate];
  const inspector = scene.lanes[laneIndex].inspectors[gate];
  const box = scene.boxes[step.taskId];
  timeline.call(() => factoryStopWork(scene, step.taskId), null, 0);
  factoryRideTo(timeline, box, checkpoint, 0, 0.35);
  timeline.to(inspector, { rotation: 0.3, duration: 0.06, repeat: 3, yoyo: true }, 0.35);
  timeline.set(scene.refuseMark, { x: checkpoint.x, y: checkpoint.robotY - 24 * layout.scale, alpha: 1 }, 0.35);
  timeline.to(scene.refuseMark, { alpha: 0, duration: 0.3 }, 0.55);
  factoryRageOwner(timeline, scene, 0.35);
  factoryFlowDollars(timeline, scene, 'out', 0.4);
  factoryRideTo(timeline, box, factorySlotPoint(layout, laneIndex, fromStation, fromSlot), 0.55, 0.4);
  timeline.call(() => factoryStartLaneWork(scene, layout, step.taskId, { laneIndex, station: fromStation, slot: fromSlot }), null, 0.95);
  return timeline;
}

function factoryExitStep(job) {
  const { scene, layout, step, laneIndex, fromStation } = job;
  const timeline = gsap.timeline();
  const robot = scene.lanes[laneIndex].robots[fromStation];
  const home = { x: robot.x, y: robot.y };
  const box = scene.boxes[step.taskId];
  const door = layout.door;
  const leaving = { x: layout.width + layout.car.width * 1.5, y: layout.car.y };
  timeline.call(() => factoryStopWork(scene, step.taskId), null, 0);
  factoryRideTo(timeline, scene.car, layout.car, 0, 0.3);
  factoryRideTo(timeline, robot, { x: door.x - layout.hold, y: door.y }, 0, 0.5);
  factoryRideTo(timeline, box, { x: door.x, y: door.y }, 0, 0.5);
  factoryRideTo(timeline, box, layout.car, 0.5, 0.15);
  factoryRideTo(timeline, robot, home, 0.5, 0.45);
  factoryRideTo(timeline, scene.car, leaving, 0.65, 0.35);
  factoryRideTo(timeline, box, leaving, 0.65, 0.35);
  factoryCheerOwner(timeline, scene, 0.5);
  factoryFlowDollars(timeline, scene, 'in', 0.6);
  timeline.call(() => { box.visible = false; }, null, 1);
  return timeline;
}

const factoryStepBuilders = { enter: factoryEnterStep, move: factoryMoveStep, refuse: factoryRefuseStep, exit: factoryExitStep };

function factoryStepTimeline(job) {
  const timeline = factoryStepBuilders[job.step.kind](job);
  timeline.call(() => factoryStepDone(job.step), null, factoryStepSeconds);
  return timeline;
}

function factoryQueueSteps(scene, layout, plan, state, steps) {
  const master = gsap.timeline();
  let current = state;
  steps.forEach(step => {
    const box = factoryFindBox(current, step.taskId);
    const laneIndex = plan.lanes.findIndex(lane => lane.sessionId === box.laneId);
    const next = factoryAdvance(current, step);
    const job = { scene, layout, step, laneIndex, fromStation: box.station, fromSlot: box.slot, slot: factoryFindBox(next, step.taskId).slot };
    master.add(factoryStepTimeline(job));
    current = next;
  });
  return master;
}

function factoryStepDone(step) {
  factoryView.playedIds.add(step.id);
  factoryView.playedCount += 1;
  factoryView.state = factoryAdvance(factoryView.state, step);
  factoryUpdateStatus();
}

function factoryPlaceBoxes(scene, layout, plan, state) {
  state.filter(box => box.station !== null).forEach(box => {
    const laneIndex = plan.lanes.findIndex(lane => lane.sessionId === box.laneId);
    const display = scene.boxes[box.taskId];
    const place = { laneIndex, station: box.station, slot: box.slot };
    const spot = factorySlotPoint(layout, laneIndex, box.station, box.slot);
    display.visible = true;
    factoryPlace(display, spot.x, spot.y);
    factoryStartLaneWork(scene, layout, box.taskId, place);
  });
}

// ---- view: clock, controls, status, data ----

function factoryElement(id) {
  return document.getElementById(id);
}

function factoryTheme() {
  return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
}

function factoryMode() {
  return factoryElement('demo').checked ? 'demo' : 'live';
}

function factoryClockTweens() {
  const view = factoryView;
  const work = view.scene ? Object.values(view.scene.work).map(entry => entry.tween) : [];
  const ambient = view.scene ? view.scene.ambient : [];
  return [view.master, ...ambient, ...work].filter(Boolean);
}

function factoryClockPaused() {
  return factoryView.userPaused || !factoryView.shown;
}

function factoryApplyClockTo(tween) {
  tween.timeScale(factoryView.speed);
  if (factoryClockPaused()) tween.pause();
  else tween.resume();
}

function factoryApplyClock() {
  factoryClockTweens().forEach(factoryApplyClockTo);
}

function factoryApplyTicker() {
  if (factoryView.shown) factoryView.app.ticker.start();
  else factoryView.app.ticker.stop();
}

function factoryProgressText() {
  const view = factoryView;
  if (!view.replayId) return factoryFloorStatusText(factoryOnFloor(view.state));
  const box = view.plan.boxes[0];
  return factoryReplayStatusText(box ? box.title : '', view.playedCount, view.plan.steps.length, view.userPaused);
}

function factoryUpdateStatus() {
  const view = factoryView;
  factoryElement('factory-status').textContent = view.failure || factoryProgressText();
  factoryElement('factory-pause').textContent = view.userPaused ? factoryTexts.playButton : factoryTexts.pauseButton;
  factoryElement('factory-pause').disabled = !view.replayId;
}

function factoryShowFailure(text) {
  factoryView.failure = text;
  factoryUpdateStatus();
}

function factoryTogglePause() {
  if (!factoryView.replayId) return;
  factoryView.userPaused = !factoryView.userPaused;
  factoryUpdateStatus();
  factoryApplyClock();
}

function factorySetSpeed() {
  factoryView.speed = factorySpeedFrom(factoryElement('factory-speed').value);
  factoryApplyClock();
}

function factoryShowTooltip(text, event) {
  factoryElement('factory-tooltip').textContent = text;
  factoryElement('factory-tooltip').hidden = false;
  factoryMoveTooltip(event);
}

function factoryMoveTooltip(event) {
  const tip = factoryElement('factory-tooltip');
  const spot = factoryTooltipPosition(event.clientX, event.clientY, tip.offsetWidth, tip.offsetHeight, window.innerWidth, window.innerHeight);
  tip.style.left = spot.left + 'px';
  tip.style.top = spot.top + 'px';
}

function factoryHideTooltip() {
  factoryElement('factory-tooltip').hidden = true;
}

function factoryMeasureStage() {
  const stage = factoryElement('factory-stage');
  return { width: stage.clientWidth, height: stage.clientHeight };
}

function factoryApplyPlan(plan) {
  const view = factoryView;
  factoryDisposeScene(view.scene);
  if (view.master) view.master.kill();
  const { played, fresh } = factorySplitSteps(plan, view.playedIds);
  const layout = factoryLayout(plan.lanes.length, view.width, view.height);
  const scene = factoryBuildScene(view.app, plan, layout, factoryPalette(factoryTheme()), factoryWorkLanes(plan, view.replayId));
  const state = factoryStateAfter({ boxes: plan.boxes, steps: played }, played.length);
  factoryPlaceBoxes(scene, layout, plan, state);
  Object.assign(view, { plan, scene, layout, state, playedCount: played.length });
  view.master = factoryQueueSteps(scene, layout, plan, state, fresh);
  factoryUpdateStatus();
  factoryApplyClock();
}

async function factoryFetchPlan() {
  const response = await fetch('/api/factory?mode=' + factoryMode());
  if (!response.ok) throw new Error('factory response');
  return factoryPlan(await response.json());
}

async function factoryRefreshData() {
  const ticket = ++factoryView.ticket;
  let plan;
  try {
    plan = await factoryFetchPlan();
  } catch (_) {
    if (ticket === factoryView.ticket) factoryShowFailure(factoryTexts.dataFailed);
    return;
  }
  if (ticket !== factoryView.ticket) return;
  factoryView.failure = '';
  factoryAdoptPayload(plan);
  factoryFillReplayOptions();
  factoryDrawVisible();
}

function factoryShowCurrentState() {
  const view = factoryView;
  view.replayId = '';
  view.userPaused = false;
  view.playedIds = new Set(view.full.steps.map(step => step.id));
}

function factoryStartReplay(taskId) {
  factoryView.replayId = taskId;
  factoryView.playedIds = new Set();
}

function factoryCountHiddenEventsPlayed() {
  const view = factoryView;
  const visible = new Set(factoryVisiblePlan(view.full, view.scope, '').boxes.map(box => box.taskId));
  view.full.steps.filter(step => !visible.has(step.taskId)).forEach(step => view.playedIds.add(step.id));
}

function factoryAdoptPayload(full) {
  const view = factoryView;
  const firstLoad = factoryIsFirstLoad(view.full, full);
  view.full = full;
  if (firstLoad || (view.replayId && !factoryReplayAvailable(full, view.scope, view.replayId))) factoryShowCurrentState();
  else if (!view.replayId) factoryCountHiddenEventsPlayed();
}

function factoryFillReplayOptions() {
  const view = factoryView;
  const newOption = (value, label) => {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    return option;
  };
  const options = factoryReplayOptions(view.full, view.scope).map(option => newOption(option.value, option.label));
  const select = factoryElement('factory-replay');
  select.replaceChildren(newOption('', factoryTexts.currentState), ...options);
  select.value = view.replayId;
}

function factoryDrawVisible() {
  const view = factoryView;
  factoryApplyPlan(factoryVisiblePlan(view.full, view.scope, view.replayId));
}

function factoryChooseReplay() {
  const view = factoryView;
  if (!view.full) return;
  const taskId = factoryElement('factory-replay').value;
  if (taskId !== '' && factoryReplayAvailable(view.full, view.scope, taskId)) factoryStartReplay(taskId);
  else factoryShowCurrentState();
  factoryFillReplayOptions();
  factoryDrawVisible();
}

function factoryApplyScopeChange() {
  const view = factoryView;
  const keepsReplay = view.replayId !== '' && factoryReplayAvailable(view.full, view.scope, view.replayId);
  if (!keepsReplay) factoryShowCurrentState();
  factoryFillReplayOptions();
  if (!keepsReplay) factoryDrawVisible();
}

function factoryRedraw() {
  if (factoryView.opened && factoryView.full) factoryDrawVisible();
}

function factoryResize() {
  const view = factoryView;
  if (!view.opened) return;
  const size = factoryMeasureStage();
  if (size.width === 0 || (size.width === view.width && size.height === view.height)) return;
  Object.assign(view, size);
  view.app.renderer.resize(size.width, size.height);
  factoryRedraw();
}

async function factoryCreateApp() {
  const size = factoryMeasureStage();
  const app = new PIXI.Application();
  await app.init({ width: size.width, height: size.height, background: factoryPalette(factoryTheme()).background, preference: 'webgl', antialias: true });
  factoryElement('factory-stage').appendChild(app.canvas);
  Object.assign(factoryView, size, { app });
}

function factoryListen() {
  factoryElement('factory-pause').addEventListener('click', factoryTogglePause);
  factoryElement('factory-speed').addEventListener('change', factorySetSpeed);
  factoryElement('factory-replay').addEventListener('change', factoryChooseReplay);
  factoryElement('theme-toggle').addEventListener('click', factoryRedraw);
  window.addEventListener('resize', factoryResize);
}

async function factoryOpen() {
  const view = factoryView;
  if (view.opening || view.opened) return;
  view.opening = true;
  factoryShowFailure('');
  factoryElement('factory-status').textContent = factoryTexts.loading;
  try {
    await loadFactoryLibraries();
    await factoryCreateApp();
  } catch (_) {
    view.opening = false;
    factoryShowFailure(factoryTexts.libraryFailed);
    return;
  }
  factoryListen();
  view.opened = true;
  view.opening = false;
  factoryApplyTicker();
  await factoryRefreshData();
}

// Hooks called by app.js.
function factorySetShown(shown) {
  factoryView.shown = shown;
  if (shown && !factoryView.opened) factoryOpen();
  else if (factoryView.opened) {
    factoryApplyTicker();
    factoryApplyClock();
  }
  if (shown) factoryResize();
}

function factorySetScope(selection) {
  const view = factoryView;
  const scope = factoryScopeFrom(selection);
  const changed = scope.key !== view.scope.key;
  view.scope = scope;
  if (changed && view.opened && view.full) factoryApplyScopeChange();
}

function factoryRefresh() {
  if (factoryView.opened) factoryRefreshData();
}
