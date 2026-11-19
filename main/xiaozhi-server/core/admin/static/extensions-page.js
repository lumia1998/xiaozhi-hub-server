async function renderExtensions(root, kind) {
  const isSkill = kind === 'skills';
  const title = isSkill ? 'Skill' : 'MCP';
  const clone = value => JSON.parse(JSON.stringify(value));
  const uid = () => 'ext_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  const icon = name => `<svg class="ui-icon" aria-hidden="true"><use href="/admin/assets/admin-icons.svg#${name}"></use></svg>`;
  let disposed = false, pending = false, draft = null, initialForm = '', toolServer = '', busyFocus = null;
  const cards = new Map(), probes = new Map();
  page.cleanup = () => {
    disposed = true;
    root.querySelectorAll('dialog').forEach(dialog => dialog.close());
  };
  let data = await api('extensions');
  if (disposed) return;
  const rows = () => data[kind];

  root.innerHTML = `<section class="extensions-page">
    <header class="page-head">
      <h1>${title}</h1>
      <div class="button-row">
        <button type="button" class="btn" id="ext-import">${isSkill ? '导入文件' : '导入 JSON'}</button>
        <button type="button" class="btn primary" id="ext-add">${isSkill ? '新增 Skill' : '添加服务器'}</button>
      </div>
    </header>
    <div class="extensions-toolbar">
      <span id="ext-count" class="muted small"></span>
      <label class="extensions-search" for="ext-search"><span class="ext-sr-only">搜索${title}</span>
        <input class="in" id="ext-search" type="search" placeholder="${isSkill ? '搜索名称或用途' : '搜索名称或服务地址'}">
      </label>
    </div>
    <p id="ext-result" class="extensions-result" role="status"></p>
    <div id="ext-list" class="extensions-grid"></div>
    <div id="ext-empty" class="extensions-empty"></div>
    <section id="ext-tools" class="extensions-tools" hidden></section>
    <input type="file" id="skill-file" accept=".md,.markdown,text/markdown,text/plain" hidden>
    <dialog id="ext-editor" class="extension-dialog" aria-labelledby="ext-editor-title"></dialog>
    <dialog id="ext-import-dialog" class="extension-dialog" aria-labelledby="ext-import-title">
      <form id="ext-import-form" class="dialog-body">
        <h2 id="ext-import-title">导入 MCP 配置</h2>
        <label class="f" for="ext-json">mcpServers JSON</label>
        <textarea class="in code-editor" id="ext-json" rows="10" required autofocus spellcheck="false"
          placeholder='{"mcpServers":{"my-server":{"url":"https://example.com/mcp"}}}'></textarea>
        <p id="ext-import-error" class="extensions-result" role="status"></p>
        <p class="ext-discard-note" id="ext-import-discard-note">内容尚未保存，可继续编辑或放弃修改。</p>
        <footer class="extension-dialog-actions">
          <button type="button" class="btn danger ext-discard" aria-describedby="ext-import-discard-note">放弃修改</button>
          <button type="button" class="btn" data-cancel>取消</button>
          <button type="submit" class="btn primary">导入并保存</button>
        </footer>
      </form>
    </dialog>
    <dialog id="ext-delete-dialog" class="extension-dialog extension-delete-dialog" aria-labelledby="ext-delete-title">
      <form method="dialog" class="dialog-body">
        <h2 id="ext-delete-title">删除${title}</h2>
        <p id="ext-delete-name"></p>
        <p id="ext-delete-error" class="extensions-result" role="status"></p>
        <footer class="extension-dialog-actions">
          <button class="btn" value="cancel" autofocus>取消</button>
          <button type="button" class="btn danger" id="ext-delete-confirm">删除</button>
        </footer>
      </form>
    </dialog>
  </section>`;
  const get = id => root.querySelector('#' + id);
  const editor = get('ext-editor'), importer = get('ext-import-dialog');
  const formState = form => JSON.stringify([...new FormData(form)]);
  const editorDirty = () => Boolean(draft && formState(get('ext-form')) !== initialForm);
  page.dirty = () => pending || editorDirty() || (importer.open && Boolean(get('ext-json').value.trim()));

  function message(id, text = '', bad = false) {
    get(id).textContent = text;
    get(id).classList.toggle('error-text', bad);
  }

  function setBusy(value) {
    if (value) busyFocus = document.activeElement;
    pending = value;
    root.querySelectorAll('button,input,select,textarea').forEach(element => { element.disabled = value; });
    root.querySelector('.extensions-page').setAttribute('aria-busy', String(value));
    if (!value && document.activeElement === document.body && busyFocus?.isConnected) busyFocus.focus();
    if (!value) busyFocus = null;
  }

  // Commit a copy only after the server accepts its revision. Failed requests
  // keep the saved state and editor draft available for retry.
  async function persist(next, resultId = 'ext-result') {
    if (pending) return false;
    setBusy(true);
    message(resultId, '正在保存…');
    try {
      const result = await api('extensions', next, true);
      if (disposed) return false;
      data = result;
      drawCards();
      const note = isSkill ? '已保存，下次提问时生效。' : '已保存，下次对话连接时生效。';
      message('ext-result', note);
      if (resultId !== 'ext-result') message(resultId, note);
      return true;
    } catch (error) {
      if (!disposed) {
        drawCards();
        message(resultId, error.message, true);
      }
      return false;
    } finally {
      if (!disposed) setBusy(false);
    }
  }

  function drawCards() {
    const needle = get('ext-search').value.trim().toLowerCase();
    const visible = rows().filter(row =>
      [row.name, row.description, row.url, row.command].join(' ').toLowerCase().includes(needle));
    const visibleIds = new Set(visible.map(row => row.id));
    for (const [id, card] of cards) {
      if (!rows().some(row => row.id === id)) { card.remove(); cards.delete(id); }
    }
    for (const row of rows()) {
      let card = cards.get(row.id);
      if (!card) {
        card = document.createElement('article');
        card.className = 'extension-card';
        card.dataset.id = row.id;
        card.innerHTML = `<header>
          <span class="extension-icon">${icon(isSkill ? 'book' : 'plug')}</span>
          <div class="extension-card-heading"><h2></h2><span class="extension-state"></span></div>
          <label class="extension-switch" for="toggle-${esc(row.id)}">
            <input id="toggle-${esc(row.id)}" type="checkbox" role="switch">
            <span class="extension-switch-track" aria-hidden="true"></span>
          </label>
        </header>
        <div class="extension-card-content"><p></p><div class="extension-meta"></div></div>
        <footer>
          ${isSkill ? '' : '<button type="button" class="btn sm" data-action="probe">测试连接</button>'}
          <button type="button" class="btn sm" data-action="edit">编辑</button>
          <button type="button" class="btn sm danger" data-action="delete">删除</button>
        </footer>`;
        card.querySelector('input').onchange = async event => {
          if (pending) return;
          const next = clone(data);
          next[kind].find(item => item.id === row.id).enabled = event.target.checked;
          await persist(next);
        };
        card.querySelector('[data-action="edit"]').onclick = () => openEditor(rows().find(item => item.id === row.id));
        card.querySelector('[data-action="delete"]').onclick = () => openDelete(row.id);
        const probeButton = card.querySelector('[data-action="probe"]');
        if (probeButton) probeButton.onclick = () => probe(rows().find(item => item.id === row.id));
        get('ext-list').append(card);
        cards.set(row.id, card);
      }
      card.hidden = !visibleIds.has(row.id);
      card.dataset.enabled = String(row.enabled);
      card.dataset.selected = String(toolServer === row.id);
      card.querySelector('h2').textContent = row.name || '未命名';
      const toggle = card.querySelector('input');
      toggle.checked = row.enabled;
      toggle.setAttribute('aria-label', '启用 ' + row.name);
      card.querySelector('.extension-state').textContent = row.enabled ? '已启用' : '已停用';
      card.querySelector('.extension-card-content > p').textContent = isSkill
        ? row.description || '暂无用途说明'
        : row.transport === 'stdio' ? row.command : row.url;
      const result = probes.get(row.id);
      card.querySelector('.extension-meta').innerHTML = isSkill ? ''
        : `<span>${esc(({ 'streamable-http': 'Streamable HTTP', sse: 'SSE', stdio: 'stdio' })[row.transport])}</span>
           <span>超时 ${esc(row.timeout)} 秒</span>
           ${result ? `<span class="${result.error ? 'error-text' : 'extension-probe-ok'}">${result.error ? '测试失败' : `测试通过，${result.tools.length} 个工具`}</span>` : ''}`;
      card.querySelectorAll('[data-action]').forEach(button => {
        button.setAttribute('aria-label', button.textContent + ' ' + row.name);
      });
    }
    get('ext-count').textContent = needle ? `${visible.length} / ${rows().length} 项`
      : `${rows().length} 项，${rows().filter(row => row.enabled).length} 项已启用`;
    get('ext-empty').hidden = visible.length > 0;
    get('ext-empty').innerHTML = `<span class="extension-empty-icon">${icon(isSkill ? 'book' : 'plug')}</span>
      <h2>${rows().length ? '没有匹配结果' : isSkill ? '还没有 Skill' : '还没有 MCP 服务器'}</h2>
      <p>${rows().length ? '请尝试其他关键词。' : isSkill ? '导入 SKILL.md，或新增任务指令。' : '添加服务器，或导入 mcpServers JSON。'}</p>`;
    drawTools();
  }

  function drawTools() {
    const row = rows().find(item => item.id === toolServer), result = probes.get(toolServer);
    const box = get('ext-tools');
    box.hidden = !row || !result;
    if (box.hidden) return;
    box.innerHTML = `<div class="extensions-tools-head"><h2>工具</h2><span>${esc(row.name)}</span></div>
      ${result.error ? `<p class="error-text">${esc(result.error)}</p>` :
      `<div class="extension-tool-grid">${result.tools.map(tool => `<article class="extension-tool-card">
        <h3>${esc(tool.name)}</h3><p>${esc(tool.description || '暂无说明')}</p></article>`).join('')}</div>
      ${result.tools.length ? '' : '<p class="muted">服务未提供工具。</p>'}`}`;
  }

  async function probe(row) {
    if (pending || !row) return;
    setBusy(true);
    message('ext-result', '正在测试 ' + row.name + '…');
    try {
      const result = await api('extensions/probe', row, true);
      if (disposed) return;
      probes.set(row.id, { tools: result.tools });
      message('ext-result', '连接测试通过，已读取 ' + result.tools.length + ' 个工具。');
    } catch (error) {
      if (disposed) return;
      probes.set(row.id, { error: error.message });
      message('ext-result', error.message, true);
    } finally {
      if (!disposed) { toolServer = row.id; drawCards(); setBusy(false); }
    }
  }

  function guardDialog(dialog, dirty) {
    function requestCancel(event) {
      if (pending || dirty()) {
        event?.preventDefault();
        dialog.querySelector('.ext-discard-note').style.visibility = 'visible';
        dialog.querySelector('.ext-discard-note').textContent = pending
          ? '正在保存，请稍候。' : '内容尚未保存，可继续编辑或放弃修改。';
        dialog.querySelector('.ext-discard').style.visibility = pending ? 'hidden' : 'visible';
        return;
      }
      dialog.close();
    }
    dialog.oncancel = requestCancel;
    dialog.querySelector('[data-cancel]').onclick = requestCancel;
    dialog.querySelector('.ext-discard').onclick = () => { if (!pending) dialog.close(); };
    dialog.oninput = () => dialog.setAttribute('closedby', dirty() ? 'none' : 'closerequest');
  }

  function openEditor(row, imported = false) {
    if (pending) return;
    draft = clone(row || { id: uid(), name: '', enabled: true, engines: ['api'],
      ...(isSkill ? { description: '', content: '' } : { transport: 'streamable-http', timeout: 15, url: '', args: [] }) });
    const exists = rows().some(item => item.id === draft.id);
    editor.innerHTML = `<form id="ext-form" class="dialog-body">
      <header class="extension-dialog-heading"><h2 id="ext-editor-title">${exists ? '编辑' : '新增'}${isSkill ? ' Skill' : ' MCP 服务器'}</h2>
        <label class="extension-switch" for="ext-enabled"><span>启用</span><input id="ext-enabled" name="enabled" type="checkbox" role="switch" ${draft.enabled ? 'checked' : ''}><span class="extension-switch-track" aria-hidden="true"></span></label>
      </header>
      <label class="f" for="ext-name">名称<input id="ext-name" name="name" class="in" required maxlength="80" value="${esc(draft.name)}" autofocus></label>
      ${isSkill ? `<label class="f" for="ext-description">用途<input id="ext-description" name="description" class="in" maxlength="500" value="${esc(draft.description)}"></label>
        <label class="f" for="ext-content">指令内容<textarea id="ext-content" name="content" class="in code-editor" rows="12" required maxlength="24000" spellcheck="false" aria-describedby="ext-skill-note">${esc(draft.content)}</textarea></label>
        <p id="ext-skill-note" class="field-note">支持 Markdown 指令；脚本和附带文件不会自动执行。</p>` :
      `<div class="grid2"><label class="f" for="ext-transport">传输方式<select class="in" id="ext-transport" name="transport">
        ${[['streamable-http', 'Streamable HTTP'], ['sse', 'SSE'], ['stdio', '本地程序 / stdio']].map(([value, label]) =>
          `<option value="${value}" ${draft.transport === value ? 'selected' : ''}>${label}</option>`).join('')}
        </select></label><label class="f" for="ext-timeout">超时（秒）<input id="ext-timeout" name="timeout" class="in" type="number" min="1" max="60" step="any" required value="${esc(draft.timeout)}"></label></div>
        <div id="ext-connection"></div>
        <label class="f" for="ext-allow">允许的工具（JSON 数组）</label>
        <p id="ext-allow-note" class="field-note">留空开放全部，[] 不开放任何工具。</p>
        <textarea id="ext-allow" name="allowed_tools" class="in code-editor" rows="2" aria-describedby="ext-allow-note">${esc(draft.allowed_tools ? JSON.stringify(draft.allowed_tools) : '')}</textarea>`}
      <p id="ext-editor-result" class="extensions-result" role="status"></p>
      <p id="ext-discard-note" class="ext-discard-note">内容尚未保存，可继续编辑或放弃修改。</p>
      <footer class="extension-dialog-actions">
        <button type="button" class="btn danger ext-discard" aria-describedby="ext-discard-note">放弃修改</button>
        <button type="button" class="btn" data-cancel>取消</button>
        <button type="submit" class="btn primary">保存</button>
      </footer>
    </form>`;
    const connectionDraft = {
      url: draft.url || '', command: draft.command || '', args: JSON.stringify(draft.args || []),
      headers: draft.headers ? JSON.stringify(draft.headers, null, 2) : '',
      env: draft.env ? JSON.stringify(draft.env, null, 2) : ''
    };
    function drawConnection() {
      const stdio = get('ext-transport').value === 'stdio', secret = stdio ? 'env' : 'headers';
      get('ext-connection').innerHTML = stdio
        ? `<label class="f" for="ext-command">启动程序<input id="ext-command" name="command" class="in mono" required value="${esc(connectionDraft.command)}" placeholder="例如 /usr/bin/npx"></label>
           <label class="f" for="ext-args">启动参数（JSON 数组）<textarea id="ext-args" name="args" class="in code-editor" rows="3">${esc(connectionDraft.args)}</textarea></label>`
        : `<label class="f" for="ext-url">服务地址<input id="ext-url" name="url" class="in mono" type="url" required value="${esc(connectionDraft.url)}" placeholder="https://example.com/mcp"></label>`;
      get('ext-connection').insertAdjacentHTML('beforeend', `<label class="f" for="ext-secret">${stdio ? '环境变量（JSON）' : '请求头（JSON）'}
        <textarea id="ext-secret" name="${secret}" class="in code-editor" rows="4" spellcheck="false" aria-describedby="ext-secret-note">${esc(connectionDraft[secret])}</textarea></label>
        <p id="ext-secret-note" class="field-note">${(draft[secret + '_keys'] || []).length ? '已保存 ' + esc(draft[secret + '_keys'].join('、')) + '。' : ''}留空保留，填写 {} 清空。</p>`);
      get('ext-connection').querySelectorAll('input,textarea').forEach(input => {
        input.oninput = () => { connectionDraft[input.name] = input.value; input.setCustomValidity(''); };
      });
    }
    if (!isSkill) {
      drawConnection();
      get('ext-transport').onchange = drawConnection;
    }
    initialForm = imported ? '' : formState(get('ext-form'));
    guardDialog(editor, editorDirty);
    editor.onclose = () => { draft = null; editor.returnValue = ''; };
    editor.setAttribute('closedby', imported ? 'none' : 'closerequest');
    get('ext-form').onsubmit = async event => {
      event.preventDefault();
      if (pending || !get('ext-form').reportValidity()) return;
      const row = { ...draft, name: get('ext-name').value.trim(), enabled: get('ext-enabled').checked };
      if (isSkill) {
        row.description = get('ext-description').value;
        row.content = get('ext-content').value;
      } else {
        row.transport = get('ext-transport').value;
        row.timeout = Number(get('ext-timeout').value);
        try {
          const stdio = row.transport === 'stdio';
          const allow = get('ext-allow').value.trim();
          if (allow) {
            row.allowed_tools = JSON.parse(allow);
            if (!Array.isArray(row.allowed_tools) || row.allowed_tools.some(name => typeof name !== 'string' || !name.trim())) throw Error('工具名称需要是非空文本数组。');
          } else delete row.allowed_tools;
          const secret = get('ext-secret');
          const value = secret.value.trim() ? JSON.parse(secret.value) : null;
          if (value !== null && (typeof value !== 'object' || Array.isArray(value)
            || Object.values(value).some(item => typeof item !== 'string'))) {
            throw Error('环境变量和请求头需要是值为文本的 JSON 对象。');
          }
          row[stdio ? 'env' : 'headers'] = value;
          if (stdio) {
            row.command = get('ext-command').value;
            row.args = JSON.parse(get('ext-args').value);
            if (!Array.isArray(row.args) || row.args.some(item => typeof item !== 'string')) {
              throw Error('启动参数需要是 JSON 字符串数组。');
            }
          } else row.url = get('ext-url').value;
        } catch (error) {
          message('ext-editor-result', '请检查 JSON 格式。' + error.message, true);
          return;
        }
      }
      const next = clone(data), index = next[kind].findIndex(item => item.id === row.id);
      if (index < 0) next[kind].push(row);
      else next[kind][index] = row;
      if (await persist(next, 'ext-editor-result')) {
        probes.delete(row.id);
        drawCards();
        editor.close();
      }
    };
    editor.showModal();
  }

  function openDelete(id) {
    if (pending) return;
    const row = rows().find(item => item.id === id);
    get('ext-delete-name').textContent = `确定删除“${row.name}”？`;
    message('ext-delete-error');
    const dialog = get('ext-delete-dialog');
    dialog.oncancel = event => { if (pending) event.preventDefault(); };
    get('ext-delete-confirm').onclick = async () => {
      const next = clone(data);
      next[kind] = next[kind].filter(item => item.id !== id);
      if (await persist(next, 'ext-delete-error')) {
        probes.delete(id);
        dialog.close();
        get('ext-add').focus();
      }
    };
    dialog.showModal();
  }

  get('ext-search').oninput = drawCards;
  get('ext-add').onclick = () => openEditor();
  get('ext-import').onclick = () => {
    if (isSkill) get('skill-file').click();
    else {
      get('ext-json').value = '';
      message('ext-import-error');
      importer.querySelector('.ext-discard-note').style.visibility = 'hidden';
      importer.querySelector('.ext-discard').style.visibility = 'hidden';
      importer.setAttribute('closedby', 'closerequest');
      importer.showModal();
    }
  };
  guardDialog(importer, () => Boolean(get('ext-json').value.trim()));
  get('skill-file').onchange = async event => {
    const file = event.target.files[0];
    event.target.value = '';
    if (!file) return;
    if (file.size > 96000) { message('ext-result', '文件过大，Skill 内容最多 24000 字符。', true); return; }
    try {
      const content = await file.text();
      if (disposed) return;
      if (content.length > 24000) throw Error('Skill 内容最多 24000 字符。');
      const row = { id: uid(), name: file.name.replace(/\.(md|markdown)$/i, ''), description: '', content, enabled: true, engines: ['api'] };
      const header = content.match(/^---\r?\n([\s\S]*?)\r?\n---/);
      if (header) {
        for (const key of ['name', 'description']) {
          const match = header[1].match(new RegExp('^' + key + ':\\s*(.+)$', 'm'));
          if (match && !/^[|>][-+]?$/.test(match[1].trim())) row[key] = match[1].replace(/^["']|["']$/g, '').trim();
        }
      }
      openEditor(row, true);
    } catch (error) { if (!disposed) message('ext-result', error.message, true); }
  };
  get('ext-import-form').onsubmit = async event => {
    event.preventDefault();
    if (pending) return;
    try {
      const servers = JSON.parse(get('ext-json').value).mcpServers;
      if (!servers || typeof servers !== 'object' || Array.isArray(servers) || !Object.keys(servers).length) {
        throw Error('需要包含非空的 mcpServers 对象。');
      }
      const incoming = Object.entries(servers).map(([name, config]) => {
        if (!config || typeof config !== 'object' || Array.isArray(config) || (!config.command && !config.url)) {
          throw Error('每个服务需要 command 或 url。');
        }
        return { ...config, id: uid(), name, enabled: config.enabled !== false, engines: ['api'],
          timeout: config.timeout ?? 15,
          transport: config.command ? 'stdio' : (config.transport || config.type) === 'sse' ? 'sse' : 'streamable-http' };
      });
      const next = clone(data);
      next[kind].push(...incoming);
      if (await persist(next, 'ext-import-error')) { get('ext-json').value = ''; importer.close(); }
    } catch (error) { message('ext-import-error', error.message, true); }
  };
  drawCards();
}
