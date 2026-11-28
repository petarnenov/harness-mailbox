import vm from 'node:vm';
import { readFileSync } from 'node:fs';

const TWEEN_SETTINGS = new Set(['duration', 'repeat', 'yoyo', 'ease']);

export const flush = () => new Promise(resolve => setImmediate(resolve));

export function okResponse(body) {
  const text = JSON.stringify(body);
  return { ok: true, status: 200, text: () => Promise.resolve(text), json: () => Promise.resolve(JSON.parse(text)) };
}

// ---- PIXI stand-ins: they record what was drawn, added and listened to ----

class DisplayDouble {
  constructor(kind, registry) {
    this.kind = kind;
    this.children = [];
    this.parent = null;
    this.x = 0;
    this.y = 0;
    this.visible = true;
    this.alpha = 1;
    this.rotation = 0;
    this.eventMode = null;
    this.listeners = {};
    this.destroyed = false;
    registry.push(this);
  }

  addChild(child) {
    child.parent = this;
    this.children.push(child);
    return child;
  }

  on(name, handler) {
    (this.listeners[name] = this.listeners[name] || []).push(handler);
    return this;
  }

  emit(name, event) {
    (this.listeners[name] || []).forEach(handler => handler(event));
  }

  destroy(options) {
    this.destroyed = true;
    if (options && options.children) this.children.forEach(child => child.destroy(options));
  }

  descendants() {
    return this.children.flatMap(child => [child, ...child.descendants()]);
  }
}

export function createPixiDouble() {
  const created = { containers: [], graphics: [], texts: [], applications: [] };

  class Container extends DisplayDouble {
    constructor() { super('container', created.containers); }
  }

  class Graphics extends DisplayDouble {
    constructor() {
      super('graphics', created.graphics);
      this.ops = [];
    }
  }
  ['roundRect', 'rect', 'circle', 'moveTo', 'lineTo', 'fill', 'stroke'].forEach(op => {
    Graphics.prototype[op] = function record(...args) {
      this.ops.push({ op, args });
      return this;
    };
  });

  class Text extends DisplayDouble {
    constructor(options) {
      super('text', created.texts);
      this.text = options.text;
      this.style = options.style;
      this.anchorCalls = [];
      this.anchor = { set: (...args) => this.anchorCalls.push(args) };
    }
  }

  class Application {
    constructor() {
      this.stage = new Container();
      this.canvas = { tag: 'canvas' };
      this.initOptions = null;
      this.resizes = [];
      this.ticker = { running: true, calls: [], start: () => { this.ticker.running = true; this.ticker.calls.push('start'); }, stop: () => { this.ticker.running = false; this.ticker.calls.push('stop'); } };
      this.renderer = { resize: (width, height) => this.resizes.push({ width, height }) };
      created.applications.push(this);
    }

    init(options) {
      this.initOptions = options;
      return Promise.resolve();
    }
  }

  return { PIXI: { Application, Container, Graphics, Text }, created };
}

// ---- gsap stand-ins: timelines record to/set/call/add with positions and can replay them ----

class TweenDouble {
  constructor(target, vars) {
    this.target = target;
    this.vars = vars;
    this.killed = false;
    this.paused = false;
    this.timeScales = [];
  }

  kill() { this.killed = true; return this; }
  timeScale(value) { this.timeScales.push(value); return this; }
  pause() { this.paused = true; return this; }
  resume() { this.paused = false; return this; }
}

class TimelineDouble extends TweenDouble {
  constructor() {
    super(null, null);
    this.entries = [];
  }

  to(target, vars, position) { this.entries.push({ kind: 'to', target, vars, position }); return this; }
  set(target, vars, position) { this.entries.push({ kind: 'set', target, vars, position }); return this; }
  call(fn, params, position) { this.entries.push({ kind: 'call', fn, position }); return this; }
  add(child) { this.entries.push({ kind: 'add', child }); return this; }

  recorded(kind) { return this.entries.filter(entry => entry.kind === kind); }
  children() { return this.recorded('add').map(entry => entry.child); }

  // Runs the recorded calls and applies the final values of to/set, children first, in position order.
  finish() {
    this.children().forEach(child => child.finish());
    const timed = this.entries.map((entry, index) => ({ entry, index })).filter(({ entry }) => entry.kind !== 'add');
    timed.sort((a, b) => (a.entry.position || 0) - (b.entry.position || 0) || a.index - b.index);
    timed.forEach(({ entry }) => {
      if (entry.kind === 'call') entry.fn();
      else Object.entries(entry.vars).filter(([key]) => !TWEEN_SETTINGS.has(key)).forEach(([key, value]) => { entry.target[key] = value; });
    });
  }
}

export function createGsapDouble() {
  const created = { timelines: [], tweens: [] };
  const gsap = {
    timeline() {
      const timeline = new TimelineDouble();
      created.timelines.push(timeline);
      return timeline;
    },
    to(target, vars) {
      const tween = new TweenDouble(target, vars);
      created.tweens.push(tween);
      return tween;
    }
  };
  return { gsap, created };
}

// ---- inert page: elements by id, listeners a test can fire, a fetch the test settles ----

class ElementDouble {
  constructor(page, tagName) {
    this.page = page;
    this.tagName = tagName;
    this.textContent = '';
    this.hidden = false;
    this.value = '';
    this.checked = false;
    this.open = false;
    this.style = {};
    this.dataset = {};
    this.attributes = new Map();
    this.children = [];
    this.listeners = {};
    this.clientWidth = 0;
    this.clientHeight = 0;
    this.offsetWidth = 0;
    this.offsetHeight = 0;
    this.removed = false;
    this.classList = { add() {}, remove() {}, toggle() {}, contains: () => false };
  }

  set innerHTML(markup) { this.page.markupWrites.push(markup); }
  get innerHTML() { return ''; }

  addEventListener(name, handler) { (this.listeners[name] = this.listeners[name] || []).push(handler); }
  fire(name, event = {}) { (this.listeners[name] || []).forEach(handler => handler(event)); }
  appendChild(child) { this.children.push(child); return child; }
  append(...items) { items.forEach(item => this.children.push(item)); }
  replaceChildren(...items) { this.children = [...items]; }
  prepend(...items) { this.children.unshift(...items); }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name); }
  removeAttribute(name) { this.attributes.delete(name); }
  remove() { this.removed = true; }
  focus() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
}

export function createPage() {
  const page = {
    markupWrites: [],
    elements: new Map(),
    requests: [],
    scripts: [],
    windowListeners: {},
    location: { search: '', hash: '', href: 'http://dashboard.test/' }
  };
  const pixi = createPixiDouble();
  const gsapDouble = createGsapDouble();
  page.pixi = pixi.created;
  page.gsap = gsapDouble.created;
  page.sink = new ElementDouble(page, 'sink');
  page.element = id => {
    if (!page.elements.has(id)) page.elements.set(id, new ElementDouble(page, 'div'));
    return page.elements.get(id);
  };
  page.document = {
    getElementById: id => page.element(id),
    createElement: tag => new ElementDouble(page, tag),
    createElementNS: (_namespace, tag) => new ElementDouble(page, tag),
    createTextNode: text => ({ textContent: String(text) }),
    head: { appendChild: script => { page.scripts.push(script); return script; } },
    documentElement: { dataset: { theme: 'dark' } },
    querySelector: () => page.sink,
    querySelectorAll: () => [],
    activeElement: page.sink
  };
  page.window = {
    innerWidth: 1000,
    innerHeight: 800,
    scrollTo() {},
    addEventListener: (name, handler) => { (page.windowListeners[name] = page.windowListeners[name] || []).push(handler); }
  };
  page.context = vm.createContext({
    document: page.document,
    window: page.window,
    location: page.location,
    history: { pushState() {} },
    localStorage: { getItem: () => null, setItem() {} },
    fetch: url => new Promise((resolve, reject) => page.requests.push({ url: String(url), resolve, reject })),
    setInterval: () => 0,
    setTimeout: callback => { queueMicrotask(callback); return 0; },
    clearInterval() {},
    clearTimeout() {},
    URL,
    URLSearchParams,
    PIXI: pixi.PIXI,
    gsap: gsapDouble.gsap
  });
  page.runScript = url => vm.runInContext(readFileSync(url, 'utf8'), page.context, { filename: url.href });
  page.eval = code => vm.runInContext(code, page.context);
  page.call = (name, ...args) => {
    page.context.callArguments = args;
    const json = vm.runInContext('JSON.stringify(' + name + '(...callArguments))', page.context);
    return json === undefined ? undefined : JSON.parse(json);
  };
  page.fireWindow = (name, event = {}) => (page.windowListeners[name] || []).forEach(handler => handler(event));
  page.requestTo = prefix => page.requests.filter(request => request.url.startsWith(prefix));
  page.elementTexts = () => [...page.elements.values()].flatMap(element => [element.textContent, ...element.children.map(child => child.textContent)]);
  return page;
}
