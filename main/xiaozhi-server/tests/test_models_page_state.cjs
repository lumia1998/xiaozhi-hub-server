// No browser or model service needed: drive the page's actual handlers against a small DOM.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../core/admin/static/models-page.js'), 'utf8');

async function pageHarness({legacyDefault = false} = {}) {
  const elements = new Map(), calls = [];
  class Element {
    constructor(tag = 'div') { this.tag = tag; this.children = []; this.dataset = {}; this.value = ''; this.textContent = ''; this.disabled = false; this.hidden = false; this.attributes = {}; }
    setAttribute(k, v) { this.attributes[k] = v; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    reportValidity() { return true; }
    set innerHTML(value) {
      this.html = value;
      if (this === root) for (const match of value.matchAll(/<([a-z]+)[^>]*\bid="([^"]+)"[^>]*>/g)) elements.set(match[2], new Element(match[1]));
    }
    get innerHTML() { return this.html; }
    querySelectorAll() { return [...elements.values()].filter(e => ['button', 'input', 'select', 'textarea'].includes(e.tag)); }
  }
  const root = new Element();
  const data = {selected: 'old', current_model: 'old-model', persona: '', protocol_labels: {chat: 'OpenAI Chat'},
    services: [{id: 'gateway', name: '网关', base_url: 'https://test/v1', key_set: true, models: [
      {id: 'old-model', label: 'old', entry: 'old', available: true},
      {id: 'new-model', label: 'new', entry: 'new', available: true}]}],
    existing: [{entry: 'standalone', model: 'local-model', protocol: 'chat'}]};
  if (legacyDefault) {
    data.selected = 'LegacyOld';
    data.services[0].models[0].entry = 'LegacyOld';
  }
  let fail = false;
  const api = async (route, body) => {
    if (body) calls.push({route, body});
    if (route === 'model-services/use') {
      if (fail) throw new Error('切换失败');
      data.selected = body.entry;
      data.current_model = body.entry === 'standalone' ? 'local-model' : body.entry + '-model';
      return {ok: true, note: '已生效'};
    }
    if (route === 'model-services/probe') {
      const model = data.services.find(s => s.id === body.id).models.find(m => m.id === body.model);
      model.probe = {selected: 'chat', results: [{protocol: 'chat', ok: true}]};
      return {ok: true, probe: structuredClone(model.probe)};
    }
    if (route === 'model-services') return structuredClone(data);
    throw new Error('Unexpected request ' + route);
  };
  const context = vm.createContext({api, page: {}, $: id => elements.get(id), esc: s => String(s || ''),
    document: {createElement: tag => new Element(tag)}, confirm: () => true});
  vm.runInContext(source, context);
  await context.renderModelServices(root);
  return {get: id => elements.get(id), root, calls, fail: () => { fail = true; }};
}

test('choosing a model only selects it; one explicit action changes the default', async () => {
  const h = await pageHarness();
  assert.equal(h.get('model-use').disabled, true);
  h.get('model-list').children[1].onclick();
  assert.equal(h.calls.length, 0);
  assert.equal(h.get('model-current').textContent, '默认模型：old-model');
  assert.equal(h.get('model-selection').textContent, '已选择：new-model');
  assert.equal(h.get('model-use').disabled, false);
  await h.get('model-use').onclick();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].body.entry, 'new');
  assert.equal(h.get('model-current').textContent, '默认模型：new-model');
  assert.equal(h.get('model-use').textContent, '已是默认');
  assert.equal(h.get('model-use').disabled, true);
});

test('standalone configuration shares the same list and default action', async () => {
  const h = await pageHarness();
  assert.equal(h.root.innerHTML.includes('model-existing'), false);
  h.get('model-service').value = '__standalone__'; h.get('model-service').onchange();
  assert.equal(h.get('model-connect').hidden, true);
  assert.equal(h.get('model-test').hidden, true);
  assert.equal(h.calls.length, 0);
  await h.get('model-use').onclick();
  assert.equal(h.calls[0].body.entry, 'standalone');
  assert.equal(h.get('model-current').textContent, '默认模型：local-model');
});

test('failed switch never displays the candidate as default', async () => {
  const h = await pageHarness(); h.fail();
  h.get('model-list').children[1].onclick();
  await h.get('model-use').onclick();
  assert.equal(h.get('model-current').textContent, '默认模型：old-model');
  assert.equal(h.get('model-result').dataset.kind, 'error');
  assert.equal(h.get('model-use').disabled, false);
});

test('unsaved service credentials prevent applying a model from the old saved route', async () => {
  const h = await pageHarness();
  h.get('model-list').children[1].onclick();
  h.get('model-key').value = 'new-unsaved-key'; h.get('model-key').oninput();
  assert.equal(h.get('model-use').disabled, true);
  await h.get('model-use').onclick();
  assert.equal(h.calls.length, 0);
});

test('protocol probing preserves the exact active legacy entry after refreshing data', async () => {
  const h = await pageHarness({legacyDefault: true});
  await h.get('model-test').onclick();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].route, 'model-services/probe');
  assert.equal(h.calls[0].body.model, 'old-model');
  assert.equal(h.get('model-list').children[0].attributes['aria-selected'], 'true');
  assert.equal(h.get('model-selection').textContent, '当前默认：old-model');
  assert.equal(h.get('model-use').disabled, true);
  assert.equal(h.get('model-probe').children.length, 1);
});
