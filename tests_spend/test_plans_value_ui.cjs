// Fake-DOM unit tests for Plans & Value UI (Case 12 + Case 1/2 display).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class FakeNode {
  constructor(tag = '', attrs = {}) {
    this.tagName = String(tag || '').toUpperCase();
    this.attrs = {...attrs};
    this.children = [];
    this.parent = null;
    this.handlers = {};
    this.dataset = {};
    this.className = attrs.className || attrs.class || '';
    this._text = '';
    this._html = '';
    this.hidden = false;
    this.disabled = false;
    this.open = false;
    this.isConnected = true;
    this.ownerDocument = null;
    if (attrs['data-period']) this.dataset.period = attrs['data-period'];
    if (attrs['data-group-id']) this.dataset.groupId = attrs['data-group-id'];
    if (attrs['data-kind']) this.dataset.kind = attrs['data-kind'];
  }
  get textContent() {
    if (this.children.length) return this.children.map(child => child.textContent).join('');
    return this._text;
  }
  set textContent(value) {
    this.children = [];
    this._text = String(value ?? '');
    this._html = this._text;
  }
  get innerHTML() {
    if (this.children.length) {
      return this.children.map(child => {
        if (child.tagName === '#TEXT') return escapeHtml(child._text);
        const attrs = Object.entries(child.attrs)
          .filter(([key]) => key !== 'className')
          .map(([key, val]) => ` ${key}="${escapeHtml(val)}"`)
          .join('');
        const cls = child.className ? ` class="${escapeHtml(child.className)}"` : '';
        return `<${child.tagName.toLowerCase()}${cls}${attrs}>${child.innerHTML}</${child.tagName.toLowerCase()}>`;
      }).join('');
    }
    return this._html;
  }
  set innerHTML(value) {
    this.children = [];
    this._html = String(value ?? '');
    this._text = this._html.replace(/<[^>]+>/g, '');
  }
  get classList() {
    const self = this;
    return {
      add(...names) {
        const set = new Set(String(self.className || '').split(/\s+/).filter(Boolean));
        names.forEach(name => set.add(name));
        self.className = [...set].join(' ');
      },
      contains(name) {
        return String(self.className || '').split(/\s+/).includes(name);
      },
      toggle(name, force) {
        const has = this.contains(name);
        if (force === true || (!has && force !== false)) this.add(name);
        else {
          self.className = String(self.className || '').split(/\s+/).filter(part => part && part !== name).join(' ');
        }
      }
    };
  }
  setAttribute(name, value) {
    this.attrs[name] = String(value);
    if (name === 'class') this.className = String(value);
    if (name.startsWith('data-')) {
      const key = name.slice(5).replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
      this.dataset[key] = String(value);
    }
  }
  getAttribute(name) {
    if (name === 'class') return this.className || null;
    return this.attrs[name] ?? null;
  }
  hasAttribute(name) {
    return this.getAttribute(name) != null;
  }
  removeAttribute(name) {
    delete this.attrs[name];
  }
  addEventListener(name, fn) {
    (this.handlers[name] ||= []).push(fn);
  }
  dispatchEvent(event) {
    const type = event.type || event;
    for (const fn of this.handlers[type] || []) fn(event);
  }
  click() {
    this.dispatchEvent({type: 'click', currentTarget: this, target: this, preventDefault() {}});
  }
  focus() {
    if (this.ownerDocument) this.ownerDocument.activeElement = this;
  }
  append(...nodes) {
    for (const node of nodes) {
      const child = typeof node === 'string' ? createText(node) : node;
      child.parent = this;
      child.ownerDocument = this.ownerDocument;
      this.children.push(child);
    }
  }
  appendChild(node) {
    this.append(node);
    return node;
  }
  replaceChildren(...nodes) {
    this.children = [];
    this._text = '';
    this._html = '';
    this.append(...nodes);
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
  querySelectorAll(selector) {
    const out = [];
    walk(this, node => {
      if (node !== this && matches(node, selector)) out.push(node);
    });
    return out;
  }
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  }[ch]));
}

function createText(value) {
  const node = new FakeNode('#text');
  node.tagName = '#TEXT';
  node._text = String(value ?? '');
  return node;
}

function walk(node, visit) {
  visit(node);
  for (const child of node.children || []) walk(child, visit);
}

function matches(node, selector) {
  if (!node || node.tagName === '#TEXT') return false;
  if (selector.startsWith('.')) {
    const classes = selector.slice(1).split('.').filter(Boolean);
    const have = String(node.className || '').split(/\s+/).filter(Boolean);
    return classes.every(cls => have.includes(cls));
  }
  if (selector.startsWith('#')) return node.attrs.id === selector.slice(1);
  if (selector.startsWith('[') && selector.endsWith(']')) {
    const body = selector.slice(1, -1);
    const eqAt = body.indexOf('=');
    if (eqAt === -1) return (node.attrs[body] ?? node.dataset[body]) != null;
    const attr = body.slice(0, eqAt);
    const raw = body.slice(eqAt + 1).replace(/^["']|["']$/g, '');
    const actual = attr === 'class' ? node.className : (node.attrs[attr] ?? node.dataset[attr]);
    return String(actual) === raw;
  }
  if (selector.includes('[')) {
    const match = /^([a-z0-9]*)\[([^=\]]+)(?:=\"([^\"]*)\")?\]$/i.exec(selector);
    if (!match) return false;
    const [, tag, attr, value] = match;
    if (tag && node.tagName !== tag.toUpperCase()) return false;
    const actual = attr === 'class' ? node.className : (node.attrs[attr] ?? node.dataset[attr]);
    if (value === undefined) return actual != null && actual !== undefined;
    return String(actual) === value;
  }
  if (selector.includes('.')) {
    const parts = selector.split('.').filter(Boolean);
    const tag = parts[0];
    const classes = parts.slice(1);
    if (tag && !tag.includes('[') && node.tagName !== tag.toUpperCase()) return false;
    const have = String(node.className || '').split(/\s+/).filter(Boolean);
    return classes.every(cls => have.includes(cls));
  }
  return node.tagName === selector.toUpperCase();
}

function createDocument() {
  const document = {
    activeElement: null,
    body: null,
    createElement(tag) {
      const node = new FakeNode(tag);
      node.ownerDocument = document;
      return node;
    },
    createTextNode(value) {
      const node = createText(value);
      node.ownerDocument = document;
      return node;
    },
    getElementById() { return null; }
  };
  document.body = document.createElement('body');
  return document;
}

function loadApi(document, extras = {}) {
  const source = fs.readFileSync(path.join(__dirname, '..', 'frontend_src', 'plans-value.js'), 'utf8');
  const sandbox = {
    module: {exports: {}},
    exports: {},
    window: extras.window || {},
    document,
    fetch: extras.fetch,
    AbortController: extras.AbortController || AbortController,
    Intl,
    Number,
    String,
    Date,
    Math,
    Object,
    Array,
    JSON,
    encodeURIComponent,
    console
  };
  sandbox.window = sandbox.window;
  vm.runInNewContext(source, sandbox);
  return sandbox.module.exports;
}

function case1Payload(overrides = {}) {
  return {
    schemaVersion: 1,
    period: 'this_month',
    timezone: 'America/New_York',
    generatedAt: '2026-09-09T04:00:00Z',
    asOf: '2026-09-09T04:00:00Z',
    localStartDate: '2026-09-01',
    localEndDate: '2026-09-08',
    groups: [{
      groupId: 'tool:codex',
      name: 'Coding plan',
      toolKeys: ['codex'],
      toolLabel: 'codex',
      plans: [{id: 1, name: 'Coding plan', accruedCostUsd: '80.00'}],
      configuredCostUsd: '80.00',
      usageValueUsd: '2400.00',
      usageStatus: 'priced',
      usageBound: 'exact',
      multiple: '30.00',
      multipleBasis: 'exact',
      multipleReason: null,
      pricingCoverage: {status: 'complete', label: 'Complete (120 events)', eventCount: 120, unpricedModels: []},
      collectionEvidence: {status: 'healthy', label: 'Healthy (5m ago)', freshnessSeconds: 300},
      attribution: {status: 'configured', label: 'Configured tool association'},
      explanation: {
        period: {activeIntervals: [{startUtc: '2026-09-01T04:00:00Z', endUtc: '2026-09-09T04:00:00Z'}]},
        configuredCost: {formula: '8 days × ($300 / 30) = $80.00'},
        tools: {label: 'Configured tool association'},
        referenceValue: {note: 'Published API-equivalent rates at event time', pricedEvents: 120, unpricedEvents: 0, unpricedModels: []},
        limits: {pricingVsCollection: 'Healthy collection evidence; does not prove historical completeness'},
        multiple: {formula: '$2,400.00 / $80.00 = 30.00×'}
      }
    }],
    unassigned: null,
    ...overrides
  };
}

function case2Payload() {
  const payload = case1Payload();
  payload.groups[0] = {
    ...payload.groups[0],
    usageStatus: 'partial',
    usageBound: 'lower_bound',
    multipleBasis: 'lower_bound',
    pricingCoverage: {
      status: 'partial',
      label: 'Partial (1 unpriced model)',
      eventCount: 121,
      unpricedModels: ['mystery-model'],
      unpricedTokens: 1000
    },
    explanation: {
      ...payload.groups[0].explanation,
      referenceValue: {note: '1,000 unpriced tokens omitted from the known subtotal', unpricedEvents: 1},
      multipleFormula: '≥ $2,400.00 / $80.00 = ≥ 30.00× (lower bound)'
    }
  };
  return payload;
}

let passed = 0;
const ok = (condition, message) => {
  assert.ok(condition, message);
  passed += 1;
};
const eq = (actual, expected, message) => {
  assert.equal(actual, expected, message);
  passed += 1;
};
const match = (actual, pattern, message) => {
  assert.match(String(actual), pattern, message);
  passed += 1;
};

(async () => {
  // This fixture is also checked against the real pure Python report assembler.
  {
    const document = createDocument();
    const api = loadApi(document);
    const root = document.createElement('div');
    const report = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/plans_value_response.json'), 'utf8'));
    api.renderPlansValue(root, report);
    match(root.textContent, /Sep 1, 2026.*Sep 8, 2026/);
    match(root.querySelector('.plans-value-cost').textContent, /\$80\.00/);
    eq(root.querySelector('.plans-value-usage').textContent, '≥ $2,400.00');
    eq(root.querySelector('.plans-value-multiple').textContent, '≥ 30.00×');
    match(root.textContent, /unpriced-model/);
    match(root.textContent, /charge|invoice/i, 'known excluded charge must have a readable reason');
    match(root.textContent, /No recorded usage/);
    match(root.textContent, /redacted provider failure/);
    match(root.textContent, /Last success: Aug 1, 2026, 8:00 AM/, 'source date uses the configured New York timezone');
    match(root.textContent, /Freshness: stale/);
    ok(!/undefined|never expose|Bearer|[A-Z]:\\Users\\|@example\.com/.test(root.textContent), 'only safe available source facts appear');
    ok(!root.textContent.includes('[object Object]'), 'structured explanations must render readable text');
  }
  // --- Pure helpers ---
  {
    const document = createDocument();
    const api = loadApi(document);
    const root = document.createElement('div');
    const payload = case1Payload();
    payload.groups[0].collectionEvidence = {
      status: 'unknown', label: 'Freshness unavailable',
      sources: [{source: 'codex_local', status: 'success', lastAttemptAt: null, lastSuccessAt: null, freshness: {state: 'unknown'}, coverage: {private: 'do not display'}}]
    };
    payload.groups[0].explanation.referenceValue.excluded = [{detail: 'Invoice charge excluded', raw: 'sk-hidden-secret'}];
    api.renderPlansValue(root, payload);
    match(root.textContent, /Last attempt: Unavailable/);
    match(root.textContent, /Last success: Unavailable/);
    match(root.textContent, /Freshness: unknown/);
    match(root.textContent, /Excluded: Invoice charge excluded/);
    ok(!/1970|Healthy recent|\[object Object\]|do not display|sk-hidden-secret/.test(root.textContent));
  }
  {
    const document = createDocument();
    const api = loadApi(document);
    eq(api.formatUsd('80'), '$80.00');
    eq(api.formatUsd('2400'), '$2,400.00');
    eq(api.formatMultiple('30'), '30.00×');
    eq(api.formatShortDate('2026-09-01'), 'Sep 1, 2026');
    eq(api.periodCaption(case1Payload()), 'Sep 1, 2026 – Sep 8, 2026 (America/New_York)');
    eq(api.esc('<img src=x onerror=alert(1)>'), '&lt;img src=x onerror=alert(1)&gt;');
    match(api.sanitizeExplanationText('see C:\\Secrets\\vault.txt and a@b.co'), /\[path omitted\].*\[redacted\]/);
  }

  // --- Full payload render + Case 1 ---
  {
    const document = createDocument();
    const api = loadApi(document);
    const root = document.createElement('div');
    const opened = {plans: 0, harnesses: 0};
    const controller = api.renderPlansValue(root, case1Payload(), {
      openPlanManager() { opened.plans += 1; },
      openHarnesses() { opened.harnesses += 1; }
    });
    eq(controller.getPeriod(), 'this_month');
    match(root.textContent, /Sep 1, 2026 \u2013 Sep 8, 2026 \(America\/New_York\)/);
    match(root.textContent, /Coding plan/);
    match(root.textContent, /\$80\.00/);
    match(root.textContent, /\$2,400\.00/);
    match(root.textContent, /30\.00×/);
    match(root.textContent, /Complete \(120 events\)/);
    match(root.textContent, /Healthy \(5m ago\)/);
    match(root.textContent, /Configured tool association/);
    const details = root.querySelector('details.plans-value-why');
    ok(details, 'why-this-number details present');
    const summary = details.querySelector('summary');
    ok(summary, 'summary present');
    match(summary.getAttribute('aria-label') || '', /Why this number/);
    match(details.textContent, /8 days × \(\$300 \/ 30\) = \$80\.00/);
    match(details.textContent, /\$2,400\.00 \/ \$80\.00 = 30\.00×/);
    const thisMonth = root.querySelector('button[data-period="this_month"]');
    const lastMonth = root.querySelector('button[data-period="last_month"]');
    eq(thisMonth.getAttribute('aria-pressed'), 'true');
    eq(lastMonth.getAttribute('aria-pressed'), 'false');
    const footer = root.querySelector('.plans-value-footer');
    footer.querySelector('.settings-link').click();
    footer.querySelector('.ghost-button').click();
    eq(opened.plans, 1);
    eq(opened.harnesses, 1);
  }

  // --- Case 2 lower-bound / partial ---
  {
    const document = createDocument();
    const api = loadApi(document);
    const root = document.createElement('div');
    api.renderPlansValue(root, case2Payload(), {});
    match(root.querySelector('.plans-value-usage').textContent, /^\u2265 \$2,400\.00$/);
    match(root.querySelector('.plans-value-multiple').textContent, /^\u2265 30\.00×$/);
    match(root.textContent, /Partial \(1 unpriced model\)/);
    ok(root.querySelector('.badge-partial'), 'partial badge class present');
  }

  // --- Rapid period switch discards late responses ---
  {
    const document = createDocument();
    const resolvers = [];
    const api = loadApi(document, {
      fetch: () => new Promise((resolve, reject) => resolvers.push({resolve, reject}))
    });
    const root = document.createElement('div');
    const controller = api.createPlansValueController(root, {
      fetchPeriod(period) {
        return new Promise((resolve, reject) => resolvers.push({period, resolve, reject}));
      }
    });
    const first = controller.load('this_month');
    const second = controller.load('last_month');
    eq(resolvers.length, 2);
    // Late success for first period must not overwrite last_month.
    resolvers[0].resolve(case1Payload({period: 'this_month', groups: [{
      ...case1Payload().groups[0],
      name: 'STALE THIS MONTH',
      configuredCostUsd: '80.00'
    }]}));
    await first;
    match(root.textContent, /Loading last month/i);
    ok(!root.textContent.includes('STALE THIS MONTH'), 'late this_month success ignored');
    resolvers[1].resolve(case1Payload({
      period: 'last_month',
      localStartDate: '2026-08-01',
      localEndDate: '2026-08-31',
      groups: [{...case1Payload().groups[0], name: 'August plan', configuredCostUsd: '300.00', usageValueUsd: '100.00', multiple: '0.33'}]
    }));
    await second;
    match(root.textContent, /August plan/);
    ok(!root.textContent.includes('STALE THIS MONTH'));
    eq(controller.getPeriod(), 'last_month');
    match(root.textContent, /Aug 1, 2026 \u2013 Aug 31, 2026/);
  }

  // --- Delayed error after switch does not paint old values under new label ---
  {
    const document = createDocument();
    const resolvers = [];
    const api = loadApi(document);
    const root = document.createElement('div');
    const controller = api.createPlansValueController(root, {
      fetchPeriod(period) {
        return new Promise((resolve, reject) => resolvers.push({period, resolve, reject}));
      }
    });
    controller.apply(case1Payload());
    match(root.textContent, /\$80\.00/);
    const pending = controller.load('last_month');
    match(root.textContent, /Loading last month/i);
    ok(!root.textContent.includes('$80.00'), 'clears prior period amounts while loading');
    resolvers[0].reject(Object.assign(new Error('network down'), {name: 'Error'}));
    await pending;
    match(root.textContent, /network down|could not load/i);
    ok(root.querySelector('button[aria-label="Retry loading Plans and Value"]'), 'retry button present');
    ok(!root.textContent.includes('Coding plan') || root.textContent.includes('Retry'), 'error state shown');
  }

  // --- Error retry ---
  {
    const document = createDocument();
    let calls = 0;
    const api = loadApi(document);
    const root = document.createElement('div');
    const controller = api.createPlansValueController(root, {
      async fetchPeriod(period) {
        calls += 1;
        if (calls === 1) throw new Error('temporary failure');
        return case1Payload({period});
      }
    });
    await controller.load('this_month');
    match(root.textContent, /temporary failure/);
    const retryButton = root.querySelector('button[aria-label="Retry loading Plans and Value"]');
    ok(retryButton, 'retry control present');
    const retried = controller.load('this_month');
    await retried;
    match(root.textContent, /\$80\.00/);
    eq(calls, 2);
  }

  // --- Dialog close/reopen discards in-flight ---
  {
    const document = createDocument();
    const resolvers = [];
    const api = loadApi(document);
    const root = document.createElement('div');
    const controller = api.createPlansValueController(root, {
      fetchPeriod() {
        return new Promise((resolve, reject) => resolvers.push({resolve, reject}));
      }
    });
    const pending = controller.load('this_month');
    controller.close();
    resolvers[0].resolve(case1Payload({groups: [{...case1Payload().groups[0], name: 'AFTER CLOSE'}]}));
    await pending;
    ok(!root.textContent.includes('AFTER CLOSE'), 'closed view ignores late success');
    controller.reopen();
    const reopened = controller.load('this_month');
    eq(resolvers.length, 2);
    resolvers[1].resolve(case1Payload());
    await reopened;
    match(root.textContent, /Coding plan/);
  }

  // --- Hostile labels escaped / not interpreted as HTML ---
  {
    const document = createDocument();
    const api = loadApi(document);
    const root = document.createElement('div');
    const hostile = case1Payload({
      groups: [{
        ...case1Payload().groups[0],
        name: '<img src=x onerror=alert(1)>',
        plans: [{id: 9, name: '<script>evil()</script>'}],
        explanation: {
          ...case1Payload().groups[0].explanation,
          contributingPlans: ['<script>evil()</script>'],
          configuredCost: {formula: 'path C:\\Users\\kamol\\secret.env and user@example.com'}
        }
      }]
    });
    api.renderPlansValue(root, hostile, {});
    ok(!root.querySelector('img'), 'hostile image element not created');
    ok(!root.querySelector('script'), 'script element not created');
    eq(root.querySelector('.plans-value-name').textContent, '<img src=x onerror=alert(1)>');
    match(root.querySelector('.plans-value-included').textContent, /<script>evil\(\)<\/script>/);
    match(root.textContent, /\[path omitted\]/);
    match(root.textContent, /\[redacted\]/);
    // Escaper still available for any intentional HTML joins.
    match(api.esc(hostile.groups[0].name), /&lt;img/);
  }

  // --- Null / zero formatting ---
  {
    const document = createDocument();
    const api = loadApi(document);
    const root = document.createElement('div');
    api.renderPlansValue(root, case1Payload({
      groups: [{
        ...case1Payload().groups[0],
        configuredCostUsd: '0.00',
        usageValueUsd: null,
        usageStatus: 'no_records',
        usageBound: null,
        multiple: null,
        multipleBasis: null,
        multipleReason: 'No recorded usage',
        pricingCoverage: {status: 'none', label: 'No recorded usage', eventCount: 0}
      }]
    }), {});
    match(root.querySelector('.plans-value-cost').textContent, /\$0\.00/);
    eq(root.querySelector('.plans-value-usage').textContent, 'No recorded usage');
    eq(root.querySelector('.plans-value-multiple').textContent, 'No recorded usage');
  }

  // --- Unassigned + empty states ---
  {
    const document = createDocument();
    const api = loadApi(document);
    const root = document.createElement('div');
    api.renderPlansValue(root, {
      period: 'this_month',
      timezone: 'America/New_York',
      localStartDate: '2026-09-01',
      localEndDate: '2026-09-08',
      groups: [],
      unassigned: {
        usageValueUsd: '12.50',
        usageStatus: 'priced',
        usageBound: 'exact',
        explanation: 'Events for tools with no configured plan.'
      }
    }, {});
    match(root.textContent, /Unassigned \/ no configured plan/);
    match(root.textContent, /\$12\.50/);
  }
  {
    const document = createDocument();
    const api = loadApi(document);
    const root = document.createElement('div');
    api.renderPlansValue(root, {
      period: 'this_month',
      timezone: 'UTC',
      localStartDate: '2026-09-01',
      localEndDate: '2026-09-08',
      groups: [],
      unassigned: null,
      emptyReason: 'no_plans'
    }, {});
    match(root.textContent, /No plans are configured/);
  }

  // --- Accessibility: period buttons and aria attributes ---
  {
    const document = createDocument();
    const api = loadApi(document);
    const root = document.createElement('div');
    api.renderPlansValue(root, case1Payload(), {});
    const group = root.querySelector('.cadence-control.plans-value-period');
    eq(group.getAttribute('role'), 'group');
    eq(group.getAttribute('aria-label'), 'Comparison period');
    const buttons = root.querySelectorAll('button[data-period]');
    eq(buttons.length, 2);
    ok(buttons.every(button => button.getAttribute('aria-label')), 'period buttons labeled');
    ok(root.querySelector('article.plans-value-row').getAttribute('aria-label'), 'row labeled');
    ok(!root.querySelector('.plans-value').getAttribute('aria-live'), 'whole view is not aria-live');
    const liveRegions = root.querySelectorAll('[aria-live]');
    ok(liveRegions.length === 0, 'no aria-live on updating shell');
  }

  // --- Manager handoff helpers on module export / window ---
  {
    const document = createDocument();
    const window = {};
    const api = loadApi(document, {window});
    ok(typeof window.renderPlansValue === 'function');
    ok(window.PlansValue === api);
  }

  console.log(`${passed} plans-value UI assertions passed`);
  process.exit(0);
})().catch(error => {
  console.error(error);
  process.exit(1);
});
