async function renderStickers(root) {
  let disposed = false, pending = false, draft = null, previewURL = '', initial = '';
  page.cleanup = () => {
    disposed = true;
    if (previewURL) URL.revokeObjectURL(previewURL);
    root.querySelectorAll('dialog').forEach(dialog => dialog.close());
  };
  const data = await api('stickers');
  if (disposed) return;
  let items = data.items;
  root.innerHTML = `<section class="stickers-page">
    <header class="page-head"><h1>表情包</h1><button class="btn primary" type="button" id="sticker-add">添加表情</button></header>
    <div class="sticker-toolbar"><span class="muted" id="sticker-count"></span><label for="sticker-search">搜索名称或标签</label><input class="in" id="sticker-search" type="search" autocomplete="off"></div>
    <p class="sticker-result" id="sticker-result" role="status"></p>
    <div class="sticker-grid" id="sticker-list"></div><p class="muted" id="sticker-empty">添加表情并填写标签，助手会在适合的对话中选择发送。</p>
    <dialog id="sticker-editor" aria-labelledby="sticker-editor-title">
      <form class="dialog-body sticker-form" id="sticker-form" action="/admin/api/stickers/save" method="post">
        <h2 id="sticker-editor-title">添加表情</h2>
        <div class="sticker-preview"><img id="sticker-preview" alt="所选表情预览" hidden></div>
        <div id="sticker-file-field"><label for="sticker-file">图片</label><input class="in" id="sticker-file" name="file" type="file" accept="image/png,image/jpeg,image/webp,image/gif" aria-describedby="sticker-file-help"><p id="sticker-file-help" class="muted">PNG、JPEG、WebP、GIF，每张最大 5 MB。</p></div>
        <button class="btn" id="sticker-annotate" type="button">使用视觉模型打标</button>
        <p class="muted sticker-annotation-note">生成名称、标签和语境后可编辑，保存后生效。动图按首帧识别。</p>
        <label for="sticker-name">名称</label><input class="in" id="sticker-name" name="name" maxlength="60" required>
        <label for="sticker-tags">情绪与语义标签</label><input class="in" id="sticker-tags" name="tags" placeholder="开心，收到，鼓励" required aria-describedby="sticker-tags-help"><p class="muted" id="sticker-tags-help">用逗号分隔，最多 12 个标签。</p>
        <label for="sticker-description">图片内容</label><textarea class="in" id="sticker-description" name="description" rows="2" maxlength="240" required placeholder="例如，一只猫竖起大拇指，配字“收到”"></textarea>
        <label for="sticker-usage">适用语境</label><textarea class="in" id="sticker-usage" name="usage" rows="3" maxlength="240" required placeholder="例如，轻松地确认收到消息或表示赞同；避免用于严肃或悲伤的话题"></textarea>
        <label class="sticker-enabled"><input id="sticker-enabled" name="enabled" type="checkbox" checked>允许助手使用</label>
        <p class="sticker-result" id="sticker-editor-result" role="status"></p>
        <footer class="button-row"><button class="btn" id="sticker-cancel" type="button">取消</button><button class="btn primary" type="submit">保存</button></footer>
      </form>
    </dialog>
    <dialog id="sticker-delete-dialog" aria-labelledby="sticker-delete-title"><div class="dialog-body">
      <h2 id="sticker-delete-title">删除表情</h2><p id="sticker-delete-name"></p><p class="muted">删除后，历史消息中的这张图片也将无法加载。</p>
      <p id="sticker-delete-result" class="sticker-result" role="status"></p>
      <div class="button-row"><button class="btn" type="button" id="sticker-delete-cancel">取消</button><button class="btn danger" type="button" id="sticker-delete-confirm">删除</button></div>
    </div></dialog>
  </section>`;
  const get = id => root.querySelector('#' + id);
  const editor = get('sticker-editor'), form = get('sticker-form'), deletion = get('sticker-delete-dialog');
  const state = () => JSON.stringify([get('sticker-name').value, get('sticker-tags').value, get('sticker-description').value,
    get('sticker-usage').value, get('sticker-enabled').checked, draft?.image || '']);
  page.dirty = () => pending || (editor.open && state() !== initial);
  function message(id, text = '', error = false) {
    get(id).textContent = text;
    get(id).classList.toggle('error-text', error);
  }
  function setBusy(value) {
    pending = value;
    root.querySelectorAll('button,input,textarea').forEach(element => { element.disabled = value; });
    root.querySelector('.stickers-page').setAttribute('aria-busy', String(value));
  }
  function draw() {
    const query = get('sticker-search').value.trim().toLowerCase();
    const filtered = items.filter(item => [item.name, item.description, item.usage, ...item.tags].join(' ').toLowerCase().includes(query));
    get('sticker-count').textContent = `${items.length} 张表情，${items.filter(item => item.enabled).length} 张已启用`;
    get('sticker-list').replaceChildren();
    get('sticker-empty').hidden = filtered.length > 0;
    get('sticker-empty').textContent = query ? '没有匹配的表情' : '添加表情并填写标签，助手会在适合的对话中选择发送。';
    for (const item of filtered) {
      const card = document.createElement('article');
      card.className = 'sticker-card';
      card.innerHTML = `<div class="sticker-picture"><img src="/media/stickers/${item.id}" alt="${esc(item.description)}" loading="lazy"></div>
        <div class="sticker-card-body"><div class="sticker-card-heading"><h2>${esc(item.name)}</h2><span class="muted">${item.enabled ? '已启用' : '已停用'}</span></div>
        <div class="sticker-tags">${item.tags.map(tag => `<span>${esc(tag)}</span>`).join('')}</div>
        <p>${esc(item.usage)}</p><div class="button-row"><button class="btn" data-action="edit" type="button">编辑</button><button class="btn" data-action="toggle" type="button">${item.enabled ? '停用' : '启用'}</button><button class="btn danger" data-action="delete" type="button">删除</button></div></div>`;
      card.querySelector('[data-action="edit"]').onclick = () => openEditor(item);
      card.querySelector('[data-action="toggle"]').onclick = () => mutate('save', {...item, enabled: !item.enabled});
      card.querySelector('[data-action="delete"]').onclick = () => {
        deletion.dataset.id = item.id;
        deletion.dataset.version = item.version;
        get('sticker-delete-name').textContent = item.name;
        message('sticker-delete-result');
        deletion.showModal();
        get('sticker-delete-cancel').focus();
      };
      get('sticker-list').appendChild(card);
    }
  }
  function openEditor(item) {
    if (previewURL) { URL.revokeObjectURL(previewURL); previewURL = ''; }
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    draft = item ? {...item} : {id: Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join(''), version: 0};
    form.reset();
    get('sticker-editor-title').textContent = item ? '编辑表情' : '添加表情';
    get('sticker-file-field').hidden = Boolean(item);
    get('sticker-file').required = !item;
    get('sticker-preview').hidden = !item;
    get('sticker-preview').removeAttribute('src');
    if (item) get('sticker-preview').src = '/media/stickers/' + item.id;
    get('sticker-name').value = item?.name || '';
    get('sticker-tags').value = item?.tags.join('，') || '';
    get('sticker-description').value = item?.description || '';
    get('sticker-usage').value = item?.usage || '';
    get('sticker-enabled').checked = item ? item.enabled : true;
    message('sticker-editor-result');
    initial = state();
    editor.showModal();
    get(item ? 'sticker-name' : 'sticker-file').focus();
  }
  function closeEditor() {
    if (pending || (state() !== initial && !confirm('放弃尚未保存的表情修改？'))) return;
    editor.close();
    get('sticker-add').focus();
  }
  async function mutate(action, payload, resultId = 'sticker-result') {
    if (pending) return false;
    setBusy(true);
    message(resultId, '正在保存…');
    try {
      const result = await api('stickers/' + action, payload, true);
      if (disposed) return false;
      items = result.items;
      draw();
      message(resultId);
      message('sticker-result', action === 'delete' ? '已删除' : '已保存，下次提问时生效。');
      return true;
    } catch (error) {
      if (!disposed) message(resultId, error.message, true);
      return false;
    } finally {
      if (!disposed) setBusy(false);
    }
  }
  get('sticker-file').onchange = async () => {
    const file = get('sticker-file').files[0];
    draft.image = '';
    if (previewURL) { URL.revokeObjectURL(previewURL); previewURL = ''; }
    get('sticker-preview').hidden = true;
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) {
      get('sticker-file').value = '';
      message('sticker-editor-result', '每张图片最大 5 MB', true);
      return;
    }
    setBusy(true);
    try {
      const encoded = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result.split(',')[1]);
        reader.onerror = () => reject(new Error('图片读取失败，请重新选择'));
        reader.readAsDataURL(file);
      });
      if (disposed) return;
      draft.image = encoded;
      previewURL = URL.createObjectURL(file);
      get('sticker-preview').src = previewURL;
      get('sticker-preview').hidden = false;
      if (!get('sticker-name').value) get('sticker-name').value = file.name.replace(/\.[^.]+$/, '').slice(0, 60);
      message('sticker-editor-result');
    } catch (error) {
      if (!disposed) message('sticker-editor-result', error.message, true);
    } finally {
      if (!disposed) setBusy(false);
    }
  };
  get('sticker-annotate').onclick = async () => {
    if (!draft.version && !draft.image) return message('sticker-editor-result', '请先选择图片', true);
    setBusy(true);
    message('sticker-editor-result', '正在生成标签…');
    try {
      const result = await api('stickers/annotate', draft.version ? {id: draft.id} : {image: draft.image}, true);
      if (disposed) return;
      get('sticker-name').value = result.name;
      get('sticker-description').value = result.description;
      get('sticker-usage').value = result.usage;
      get('sticker-tags').value = result.tags.join('，');
      message('sticker-editor-result', '标签已填入，可编辑后保存。');
    } catch (error) {
      if (!disposed) message('sticker-editor-result', error.message, true);
    } finally {
      if (!disposed) setBusy(false);
    }
  };
  form.onsubmit = async event => {
    event.preventDefault();
    const payload = {...draft, name: get('sticker-name').value, description: get('sticker-description').value,
      usage: get('sticker-usage').value, tags: get('sticker-tags').value.split(/[,，]/).map(tag => tag.trim()).filter(Boolean),
      enabled: get('sticker-enabled').checked};
    if (await mutate('save', payload, 'sticker-editor-result')) {
      editor.close();
      get('sticker-add').focus();
    }
  };
  get('sticker-add').onclick = () => openEditor(null);
  get('sticker-search').oninput = draw;
  get('sticker-cancel').onclick = closeEditor;
  editor.addEventListener('cancel', event => { event.preventDefault(); closeEditor(); });
  deletion.addEventListener('cancel', event => { if (pending) event.preventDefault(); });
  get('sticker-delete-cancel').onclick = () => deletion.close();
  get('sticker-delete-confirm').onclick = async () => {
    if (await mutate('delete', {id: deletion.dataset.id, version: Number(deletion.dataset.version)}, 'sticker-delete-result')) deletion.close();
  };
  draw();
}
