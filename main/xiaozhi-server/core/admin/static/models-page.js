async function renderModelServices(root) {
  const standalone = '__standalone__';
  let data = await api('model-services');
  let currentService = data.services.find(s => s.models.some(m => m.entry === data.selected))?.id
    || (data.existing.some(m => m.entry === data.selected) ? standalone : data.services[0]?.id)
    || (data.existing.length ? standalone : '');
  let selectedEntry = '', pending = false, disposed = false, serviceDirty = false, personaOriginal = data.persona;
  page.cleanup = () => { disposed = true; };
  page.dirty = () => serviceDirty || ($('model-persona') && $('model-persona').value !== personaOriginal);
  root.innerHTML = `<section class="models-page">
    <header class="models-head"><h1>大模型</h1><span id="model-current"></span></header>
    <section class="models-panel"><h2>人设</h2><label class="models-field" for="model-persona"><span>助手身份与行为设定</span>
      <textarea id="model-persona" rows="5" maxlength="12000">${esc(data.persona)}</textarea></label>
      <div class="models-actions"><span>下次对话连接时生效</span><button id="persona-save" type="button">保存人设</button></div>
      <p id="persona-result" class="models-result" role="status"></p></section>
    <section class="models-panel"><h2>模型服务</h2>
      <div class="models-service-choice"><label class="models-field"><span>服务</span><select id="model-service"></select></label><button id="model-new" type="button">新增服务</button></div>
      <form id="model-connect" class="models-connect">
        <label class="models-field"><span>名称</span><input id="model-name" required maxlength="80" placeholder="家里的模型网关"></label>
        <label class="models-field"><span>接口地址</span><input id="model-url" type="url" required placeholder="https://api.example.com/v1" spellcheck="false"></label>
        <label class="models-field models-wide"><span>API Key <small id="model-key-hint"></small></span><input id="model-key" type="password" autocomplete="new-password" spellcheck="false"></label>
        <div class="models-actions models-wide"><button class="models-primary" id="model-fetch" type="submit">获取模型并保存</button></div>
      </form><p id="model-result" class="models-result" role="status" aria-live="polite"></p>
      <div id="model-discovery" class="models-discovery"></div>
      <div class="models-catalog-head"><label class="models-field"><span>搜索模型</span><input id="model-search" type="search" placeholder="输入名称筛选"></label><span id="model-count"></span></div>
      <div id="model-list" class="models-list" role="listbox" aria-label="模型列表"></div>
      <div class="models-probe" id="model-probe"></div>
      <div class="models-actions"><span id="model-selection"></span><button type="button" id="model-test">检测协议</button><button type="button" class="models-primary" id="model-use">设为默认</button></div>
      <p id="model-probe-note" class="models-note">检测协议会发送四次简短请求，可能消耗额度。</p>
    </section>
  </section>`;
  function message(id, text = '', bad = false) { $(id).textContent = text; $(id).dataset.kind = bad ? 'error' : 'ok'; }
  function activeService() { return data.services.find(s => s.id === currentService); }
  function models() {
    return currentService === standalone
      ? data.existing.map(m => ({...m, id: m.model, available: true}))
      : activeService()?.models || [];
  }
  function selectedModel() { return models().find(m => m.entry === selectedEntry); }
  function drawSelection() {
    const model = selectedModel(), isDefault = model?.entry === data.selected;
    $('model-current').textContent = '默认模型：' + (data.current_model || '未设置');
    $('model-selection').textContent = model ? (isDefault ? '当前默认：' : '已选择：') + model.id : '请选择模型';
    $('model-use').textContent = isDefault ? '已是默认' : '设为默认';
    $('model-use').disabled = pending || serviceDirty || !model || model.available === false || isDefault;
    $('model-test').disabled = pending || serviceDirty || !model || !activeService() || model.available === false;
    $('model-test').hidden = $('model-probe-note').hidden = currentService === standalone;
  }
  function busy(value) {
    pending = value;
    root.querySelectorAll('input,select,textarea,button').forEach(e => e.disabled = value);
    drawModels();
    drawSelection();
  }
  function drawProbe(probe) {
    $('model-probe').replaceChildren();
    if (!probe) return;
    const table = document.createElement('div'); table.className = 'models-protocols';
    for (const p of probe.results || []) {
      const row = document.createElement('div'), name = document.createElement('b'), state = document.createElement('span');
      name.textContent = data.protocol_labels[p.protocol] || p.protocol;
      state.textContent = p.ok ? (p.protocol === probe.selected ? '可用，已选用' : '可用') : (p.error || '不可用');
      state.dataset.kind = p.ok ? 'ok' : 'error'; row.append(name, state); table.append(row);
    }
    $('model-probe').append(table);
  }
  function drawModels() {
    const all = models(), needle = $('model-search').value.trim().toLowerCase();
    const visible = all.filter(m => (m.id + ' ' + (m.label || '')).toLowerCase().includes(needle));
    $('model-count').textContent = `${visible.length} / ${all.length} 个模型`;
    const list = $('model-list'); list.replaceChildren();
    for (const model of visible) {
      const b = document.createElement('button'); b.type = 'button'; b.setAttribute('role', 'option');
      b.setAttribute('aria-selected', String(model.entry === selectedEntry));
      b.disabled = pending || model.available === false;
      const name = document.createElement('b'); name.textContent = model.id;
      const state = document.createElement('small');
      state.textContent = model.entry === data.selected ? '默认模型' : model.available === false ? '不可用'
        : data.protocol_labels[model.probe?.selected || model.protocol] || '自动选择协议';
      b.append(name, state);
      b.onclick = () => { if (pending || model.available === false) return; selectedEntry = model.entry; drawModels(); drawProbe(model.probe); drawSelection(); };
      list.append(b);
    }
    if (!visible.length) { const e = document.createElement('p'); e.textContent = currentService ? '没有匹配的模型' : '填写服务信息并获取模型列表'; list.append(e); }
  }
  function drawService() {
    const service = activeService();
    $('model-service').innerHTML = '<option value="">新增服务</option>'
      + data.services.map(s => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('')
      + (data.existing.length ? `<option value="${standalone}">独立配置</option>` : '');
    $('model-service').value = currentService;
    $('model-connect').hidden = currentService === standalone;
    $('model-name').value = service?.name || ''; $('model-url').value = service?.base_url || ''; $('model-key').value = '';
    $('model-key-hint').textContent = service?.key_set ? '已保存，留空保留' : '免认证服务可留空';
    $('model-search').value = '';
    const all = models(); selectedEntry = all.find(m => m.entry === data.selected)?.entry || all.find(m => m.available !== false)?.entry || '';
    $('model-discovery').textContent = service?.partial ? '模型列表未取完，请重新获取。' : '';
    serviceDirty = false;
    drawModels(); drawProbe(selectedModel()?.probe); busy(false);
  }
  ['model-name', 'model-url', 'model-key'].forEach(id => $(id).oninput = () => { serviceDirty = true; drawSelection(); });
  $('model-service').onchange = () => {
    if (serviceDirty && !confirm('服务信息尚未保存，确定切换吗？')) { $('model-service').value = currentService; return; }
    currentService = $('model-service').value; drawService(); message('model-result');
  };
  $('model-new').onclick = () => { if (serviceDirty && !confirm('服务信息尚未保存，确定新增吗？')) return; currentService = ''; drawService(); message('model-result'); };
  $('model-search').oninput = drawModels;
  $('persona-save').onclick = async () => {
    if (pending) return; busy(true);
    try { const r = await api('persona', {persona: $('model-persona').value, expected: personaOriginal}); if (disposed) return; personaOriginal = r.persona; $('model-persona').value = r.persona; message('persona-result', r.note); }
    catch (e) { if (!disposed) message('persona-result', e.message, true); } finally { if (!disposed) busy(false); }
  };
  $('model-connect').onsubmit = async e => {
    e.preventDefault(); if (pending || !$('model-connect').reportValidity()) return;
    const body = {id: currentService, name: $('model-name').value, base_url: $('model-url').value, api_key: $('model-key').value};
    busy(true); message('model-result', '正在获取模型列表…');
    try { const r = await api('model-services/discover', body); if (disposed) return; data = r; currentService = r.id; drawService(); message('model-result', '模型列表和服务配置已保存'); }
    catch (e) { if (!disposed) message('model-result', e.message, true); } finally { if (!disposed) busy(false); }
  };
  async function action(use) {
    const model = selectedModel();
    if (pending || serviceDirty || !model || model.available === false || (use && model.entry === data.selected)) return;
    if (!use && !activeService()) return;
    busy(true); message('model-result', use ? '正在切换默认模型…' : '正在检测协议…');
    try {
      const r = await api(use ? 'model-services/use' : 'model-services/probe', use ? {entry: model.entry} : {id: currentService, model: model.id});
      if (disposed) return;
      data = await api('model-services'); if (disposed) return;
      drawModels(); drawProbe(selectedModel()?.probe); drawSelection();
      message('model-result', use ? r.note : (r.probe.selected ? '可用协议已保存' : '四种协议均未通过'), !use && !r.probe.selected);
    } catch (e) { if (!disposed) message('model-result', e.message, true); } finally { if (!disposed) busy(false); }
  }
  $('model-test').onclick = () => action(false); $('model-use').onclick = () => action(true);
  drawService();
}
