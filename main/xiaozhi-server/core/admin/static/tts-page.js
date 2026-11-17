async function renderTTSProviders(root) {
  const d = await api('tts');
  const drafts = new Map();
  let pickers = [];
  const clearPickers = () => { pickers.forEach(p => p.destroy()); pickers = []; };
  let chosen = d.provider || 'edge', pending = false, controller = null, objectUrl = null, disposed = false;
  function stopAudio() {
    if (audio) { audio.pause(); audio = null; }
    if (objectUrl) { URL.revokeObjectURL(objectUrl); objectUrl = null; }
  }
  page.cleanup = () => { disposed = true; controller?.abort(); stopAudio(); clearPickers(); };
  page.dirty = () => [...drafts].some(([id, values]) => {
    const p = d.providers.find(v => v.id === id);
    return p.fields.some(f => String(values[f.key] ?? '') !== String(f.value ?? ''));
  });
  root.innerHTML = `<section class="tts-page">
    <header class="tts-head"><h1>语音合成</h1><span id="tts-current" class="tts-current"></span></header>
    <form id="tts-form" class="tts-form">
      <label class="tts-field tts-provider"><span>语音服务</span><select id="tts-provider">${d.providers.map(p =>
        `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join('')}</select></label>
      <p id="tts-hint" class="tts-hint"></p>
      <div id="tts-fields" class="tts-fields"></div>
      <div class="tts-preview"><label class="tts-field"><span>试听文字</span><textarea id="tts-sample" maxlength="200" rows="2">${esc(d.sample_text)}</textarea></label></div>
      <p id="tts-result" class="tts-result" role="status" aria-live="polite"></p>
      <div class="tts-actions"><span>保存后，音箱下次对话时生效</span><button id="tts-preview" type="button">试听</button><button id="tts-save" class="tts-primary" type="submit">保存并启用</button></div>
    </form></section>`;
  const form = $('tts-form'), selector = $('tts-provider');
  function message(text = '', kind = '') { $('tts-result').textContent = text; $('tts-result').dataset.kind = kind; }
  function working(value, label = '') {
    pending = value;
    pickers.forEach(p => p.setDisabled(value));
    form.querySelectorAll('input, select, button, textarea').forEach(el => { el.disabled = value; });
    if (value) message(label);
  }
  function values() {
    return Object.fromEntries([...form.querySelectorAll('[data-tts-field]')].map(el => [el.dataset.ttsField, el.value]));
  }
  function remember() { drafts.set(chosen, values()); }
  function renderFields() {
    clearPickers();
    const p = d.providers.find(v => v.id === chosen), draft = drafts.get(chosen);
    selector.value = chosen;
    $('tts-current').textContent = '当前：' + (d.providers.find(v => v.entry === d.selected)?.label || d.selected);
    $('tts-hint').textContent = p.hint;
    $('tts-fields').innerHTML = p.fields.map(f => {
      const value = draft?.[f.key] ?? f.value ?? '';
      const type = f.kind === 'secret' ? 'password' : f.kind === 'number' ? 'number' : f.kind === 'url' ? 'url' : 'text';
      const id = 'tts-' + f.key;
      const required = f.required && !(f.kind === 'secret' && f.configured);
      const hasPresets = Boolean(f.choices?.length);
      return `<div class="tts-field ${f.kind === 'url' || f.key === 'instructions' ? 'tts-wide' : ''}">
        <label for="${id}">${esc(f.label)}${f.kind === 'secret' && f.configured ? '<small>已保存，留空保留</small>' : ''}</label>
        <input id="${id}" data-tts-field="${esc(f.key)}" type="${type}" value="${esc(value)}" ${required ? 'required' : ''}
          ${f.kind === 'number' ? `min="${f.min}" max="${f.max}" step="${f.step}"` : ''}
          autocomplete="${f.kind === 'secret' ? 'new-password' : 'off'}" spellcheck="false" autocapitalize="none">
        ${hasPresets ? `<div id="${id}-presets" class="tts-picker" data-tts-presets="${esc(f.key)}"></div>` : ''}
        ${f.hint ? `<small>${esc(f.hint)}</small>` : ''}</div>`;
    }).join('');
    // Page-rendered menus do not depend on a browser/embedded-webview native popup.
    for (const f of p.fields) {
      if (f.choices?.length) pickers.push(createTTSPicker($(`tts-${f.key}-presets`), $(`tts-${f.key}`), f));
    }
    $('tts-fields').querySelectorAll('input').forEach(input => {
      if (!input.hasAttribute('data-tts-field')) return;
      input.addEventListener('input', () => { remember(); message(); });
      input.addEventListener('change', () => { remember(); message(); });
    });
    if (p.conflict) { message('此配置名称已被其他类型占用，请先检查服务端配置', 'error'); $('tts-save').disabled = true; }
  }
  selector.onchange = () => {
    if (pending) return;
    remember(); stopAudio(); chosen = selector.value; message(); renderFields();
  };
  $('tts-preview').onclick = async () => {
    if (pending || !form.reportValidity()) return;
    stopAudio(); remember();
    const body = { provider: chosen, values: values(), text: $('tts-sample').value };
    controller = new AbortController(); working(true, '正在生成试听…');
    try {
      const response = await fetch('/admin/api/tts/preview', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal: controller.signal });
      if (!response.ok) {
        let detail; try { detail = await response.json(); } catch (_) { /* non-JSON auth error */ }
        throw new Error(detail?.error || (response.status === 401 ? '登录已过期，请重新登录' : '试听失败，请检查配置'));
      }
      const blob = await response.blob();
      if (disposed) return;
      objectUrl = URL.createObjectURL(blob);
      const a = audio = new Audio(objectUrl);
      a.onended = () => { if (!disposed) message('试听完成', 'ok'); stopAudio(); };
      a.onerror = () => { if (!disposed) message('返回的音频无法播放，请检查服务格式', 'error'); stopAudio(); };
      await a.play();
      message('正在播放试听', 'ok');
    } catch (e) { if (!disposed && e.name !== 'AbortError') message(e.message, 'error'); }
    finally { if (!disposed) working(false); controller = null; }
  };
  form.onsubmit = async e => {
    e.preventDefault();
    if (pending || !form.reportValidity()) return;
    remember(); stopAudio();
    const body = { provider: chosen, values: values() };
    working(true, '正在保存…');
    try {
      const result = await api('tts', body);
      if (disposed) return;
      drafts.delete(chosen);
      Object.assign(d, await api('tts'));
      if (disposed) return;
      renderFields(); message(result.note || '已保存', 'ok');
    } catch (error) { if (!disposed) message(error.message, 'error'); }
    finally { if (!disposed) working(false); }
  };
  renderFields();
}

function createTTSPicker(host, input, field) {
  const listId = `${input.id}-options`;
  host.innerHTML = `<button type="button" class="tts-picker-toggle" aria-label="${esc(field.label)}预设" aria-expanded="false" aria-controls="${listId}">
      <span class="tts-picker-value"></span><span class="tts-picker-arrow" aria-hidden="true">▾</span></button>
    <div class="tts-picker-panel" hidden>
      <input class="tts-picker-search" type="search" aria-label="搜索${esc(field.label)}" placeholder="搜索名称或 ID" autocomplete="off" spellcheck="false">
      <div id="${listId}" class="tts-picker-options" role="listbox" aria-label="${esc(field.label)}选项"></div>
      <div class="tts-picker-empty" role="status" hidden>没有匹配的预设，可在上方输入框手填 ID</div>
    </div>`;
  const toggle = host.querySelector('.tts-picker-toggle'), label = host.querySelector('.tts-picker-value');
  const panel = host.querySelector('.tts-picker-panel'), search = host.querySelector('.tts-picker-search');
  const list = host.querySelector('.tts-picker-options'), empty = host.querySelector('.tts-picker-empty');
  const choices = field.choices.map(o => ({ ...o, value: String(o.value) }));
  function close(focus = false) {
    panel.hidden = true; toggle.setAttribute('aria-expanded', 'false');
    if (focus) toggle.focus({ preventScroll: true });
  }
  function sync() {
    const current = choices.find(o => o.value === input.value);
    label.textContent = current?.label ?? (input.value ? `自定义：${input.value}` : '从预设中选择');
    label.title = label.textContent;
    if (!panel.hidden) render();
  }
  function render() {
    const needle = search.value.trim().toLocaleLowerCase();
    const all = [...choices];
    if (input.value && !all.some(o => o.value === input.value)) {
      all.unshift({ value: input.value, label: '当前自定义音色 / 参数', group: '自定义' });
    }
    const filtered = all.filter(o => `${o.label} ${o.value} ${o.group || ''}`.toLocaleLowerCase().includes(needle));
    const groups = new Map();
    for (const option of filtered) {
      const group = option.group || '';
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push(option);
    }
    list.replaceChildren();
    for (const [group, options] of groups) {
      const section = document.createElement('div');
      if (group) {
        section.setAttribute('role', 'group'); section.setAttribute('aria-label', group);
        const heading = document.createElement('div'); heading.className = 'tts-picker-group'; heading.textContent = group;
        section.appendChild(heading);
      }
      for (const option of options) {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'tts-picker-option';
        button.setAttribute('role', 'option'); button.setAttribute('aria-selected', String(option.value === input.value));
        button.dataset.value = option.value;
        const name = document.createElement('span'); name.textContent = option.label; button.appendChild(name);
        if (option.value && option.label !== option.value) {
          const id = document.createElement('small'); id.textContent = option.value; button.appendChild(id);
        }
        button.onclick = () => {
          input.value = option.value; close(true);
          input.dispatchEvent(new Event('input', { bubbles: true }));
          input.dispatchEvent(new Event('change', { bubbles: true }));
        };
        section.appendChild(button);
      }
      list.appendChild(section);
    }
    empty.hidden = filtered.length > 0;
  }
  function open() {
    if (toggle.disabled) return;
    // Only one list remains open; don't preserve a search from a different opening.
    document.dispatchEvent(new CustomEvent('tts-picker-open', { detail: host }));
    panel.hidden = false; toggle.setAttribute('aria-expanded', 'true'); search.value = ''; render();
    search.focus({ preventScroll: true });
  }
  toggle.onclick = () => panel.hidden ? open() : close();
  toggle.onkeydown = event => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); open(); }
  };
  search.oninput = render;
  panel.onkeydown = event => {
    if (event.isComposing) return;
    if (event.key === 'Escape') { event.preventDefault(); close(true); return; }
    const options = [...list.querySelectorAll('[role="option"]')];
    const index = options.indexOf(document.activeElement);
    if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
      event.preventDefault();
      const next = event.key === 'ArrowDown' ? Math.min(index + 1, options.length - 1) : (index < 0 ? options.length - 1 : Math.max(0, index - 1));
      options[next]?.focus({ preventScroll: true }); options[next]?.scrollIntoView({ block: 'nearest' });
    } else if (index >= 0 && ['Home', 'End'].includes(event.key)) {
      event.preventDefault(); (event.key === 'Home' ? options[0] : options[options.length - 1])?.focus();
    } else if (event.key === 'Enter' && document.activeElement === search) {
      event.preventDefault(); options[0]?.click();
    }
  };
  const outside = event => { if (!host.contains(event.target)) close(); };
  const another = event => { if (event.detail !== host) close(); };
  const focusLeft = event => { if (!host.contains(event.target)) close(); };
  document.addEventListener('pointerdown', outside);
  document.addEventListener('tts-picker-open', another);
  document.addEventListener('focusin', focusLeft);
  input.addEventListener('input', sync); input.addEventListener('change', sync);
  sync();
  return {
    setDisabled(disabled) { toggle.disabled = disabled; if (disabled) close(); },
    destroy() {
      close(); document.removeEventListener('pointerdown', outside);
      document.removeEventListener('tts-picker-open', another); document.removeEventListener('focusin', focusLeft);
      input.removeEventListener('input', sync); input.removeEventListener('change', sync);
    },
  };
}
