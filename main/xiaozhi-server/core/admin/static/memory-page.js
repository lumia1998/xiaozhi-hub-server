async function renderMemoryPolicy(root) {
  let data = await api('memory');
  let current = data.devices.find(d => d.online)?.device || data.devices[0]?.device || '';
  let disposed = false;
  const drafts = new Map();
  page.cleanup = () => { disposed = true; };
  page.dirty = () => data.devices.some(d => {
    const draft = drafts.get(d.device);
    return draft && (draft.pinned !== (d.memory.pinned || '') || draft.summary !== (d.memory.summary || '') ||
      JSON.stringify(draft.preferences) !== JSON.stringify(d.preferences || []) || draft.owner !== d.owner ||
      Boolean(draft.preferenceTopic || draft.preferenceContent || draft.editingPreferenceId));
  });
  const fmt = time => time ? new Date(time * 1000).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',hour12:false}) : '';
  root.innerHTML = `<div class="page-head"><div><h1>记忆</h1></div><span class="pill ${data.settings.summary?'ok':''}">${data.settings.summary?'自动整理已开启':'自动整理已关闭'}</span></div>
    <section class="card panel"><label class="f">设备<select id="memory-device" class="in"></select></label><div class="memory-retention" id="memory-retention"></div></section><div id="memory-editor"></div>`;
  function getDraft(d) {
    let draft = drafts.get(d.device);
    if (!draft) { draft = {pinned:d.memory.pinned || '', summary:d.memory.summary || '', owner:d.owner || d.device,
      preferences:(d.preferences || []).map(p => ({...p})), preferenceTopic:'', preferenceContent:'', editingPreferenceId:null, preferenceVersion:null}; drafts.set(d.device, draft); }
    return draft;
  }
  function draw() {
    const select = $('memory-device');
    select.innerHTML = data.devices.map(d => `<option value="${esc(d.device)}">${esc(d.label || d.device)}${d.online?'（在线）':''}</option>`).join('');
    select.value = current;
    const d = data.devices.find(d => d.device === current), host = $('memory-editor');
    if (!d) { select.disabled = true; host.innerHTML = '<div class="card panel empty-state"><h2>还没有记忆</h2><p>音箱开始对话后，可以在这里管理长期信息。</p></div>'; return; }
    select.disabled = false;
    const draft = getDraft(d), total = d.models.reduce((n,m) => n + m.count, 0);
    $('memory-retention').innerHTML = `<span class="pill">${total} 条聊天消息</span><span class="pill">${d.temporary} 条当天临时消息</span>${d.expires_at?`<span class="muted small">${esc(fmt(d.expires_at))} 起清理临时消息</span>`:''}`;
    host.innerHTML = `<section class="card panel memory-section"><div class="editor-heading"><h2>设备归属</h2><span class="pill">共享记忆分组</span></div><p class="muted small">归属只决定记忆分组，不代表设备身份已认证。</p><label class="f" for="memory-owner">归属名称</label><input class="in" id="memory-owner" maxlength="200" value="${esc(draft.owner)}"><div class="actions"><span class="result" id="memory-owner-result" role="status"></span><button class="btn primary" data-owner-save>保存归属</button></div></section>
      <section class="card panel memory-section"><div class="editor-heading"><h2>明确偏好</h2><span class="pill">手动维护</span></div><div class="preference-list" id="preference-list"></div><div class="preference-form"><label class="f" for="preference-topic">主题</label><input class="in" id="preference-topic" maxlength="100" placeholder="例如：饮食" value="${esc(draft.preferenceTopic)}"><label class="f" for="preference-content">内容</label><input class="in" id="preference-content" maxlength="2000" placeholder="例如：不吃香菜" value="${esc(draft.preferenceContent)}"><div class="actions"><span class="result" id="preference-result" role="status"></span>${draft.editingPreferenceId?'<button class="btn" data-preference-cancel>取消修改</button>':''}<button class="btn primary" data-preference-save>${draft.editingPreferenceId?'保存修改':'新增偏好'}</button></div></div></section>
      <section class="card panel memory-section"><div class="editor-heading"><h2>固定保留</h2><span class="pill">手动维护</span></div><label class="f" for="memory-pinned">内容</label><textarea class="in" id="memory-pinned" rows="5" maxlength="5000" placeholder="例如：我喜欢简短回答。">${esc(draft.pinned)}</textarea><div class="actions"><span class="result" id="memory-pinned-result" role="status"></span><button class="btn" data-clear="pinned">清空固定内容</button><button class="btn primary" data-save="pinned">保存固定内容</button></div></section>
      <section class="card panel memory-section"><div class="editor-heading"><h2>自动整理</h2><span class="muted small">${d.memory.updated?'最近更新 '+esc(fmt(d.memory.updated)):''}</span></div><label class="f" for="memory-summary">内容</label><textarea class="in" id="memory-summary" rows="7" maxlength="5000" placeholder="没有需要长期保留的信息">${esc(draft.summary)}</textarea><div class="actions"><span class="result" id="memory-summary-result" role="status"></span><button class="btn" data-clear="summary">清空自动记忆</button><button class="btn primary" data-save="summary">保存修改</button></div></section>`;
    const list = $('preference-list');
    list.innerHTML = draft.preferences.length ? draft.preferences.map(p => `<div class="preference-row"><div><b>${esc(p.topic)}</b><span>${esc(p.content)}</span></div><div class="preference-actions"><button class="btn sm" data-preference-edit="${p.id}">修改</button><button class="btn sm danger" data-preference-delete="${p.id}" data-version="${p.version}">删除</button></div></div>`).join('') : '<p class="muted small">还没有明确偏好。</p>';
    $('memory-owner').oninput = e => { draft.owner = e.target.value; };
    $('memory-pinned').oninput = e => { draft.pinned = e.target.value; };
    $('memory-summary').oninput = e => { draft.summary = e.target.value; };
    $('preference-topic').oninput = e => { draft.preferenceTopic = e.target.value; };
    $('preference-content').oninput = e => { draft.preferenceContent = e.target.value; };
    $('memory-device').onchange = e => { current = e.target.value; draw(); };
    $('preference-list').querySelectorAll('[data-preference-edit]').forEach(b => b.onclick = () => {
      const preference = draft.preferences.find(p => p.id === Number(b.dataset.preferenceEdit));
      if (!preference) return;
      draft.editingPreferenceId = preference.id; draft.preferenceVersion = preference.version;
      draft.preferenceTopic = preference.topic; draft.preferenceContent = preference.content; draw();
    });
    $('preference-list').querySelectorAll('[data-preference-delete]').forEach(b => b.onclick = () => preferenceDelete(d, draft, Number(b.dataset.preferenceDelete), Number(b.dataset.version)));
    host.querySelector('[data-owner-save]').onclick = () => ownerSave(d, draft);
    host.querySelector('[data-preference-save]').onclick = () => preferenceSave(d, draft);
    host.querySelector('[data-preference-cancel]')?.addEventListener('click', () => { draft.editingPreferenceId = null; draft.preferenceVersion = null; draft.preferenceTopic = ''; draft.preferenceContent = ''; draw(); });
    host.querySelectorAll('[data-save]').forEach(b => b.onclick = () => memorySave(d, draft, b.dataset.save, draft[b.dataset.save]));
    host.querySelectorAll('[data-clear]').forEach(b => b.onclick = () => { if (confirm(b.dataset.clear === 'pinned' ? '清空固定内容？' : '清空自动记忆？')) memorySave(d, draft, b.dataset.clear, ''); });
  }
  const busy = (host, value) => host.querySelectorAll('button,input,textarea,select').forEach(e => { e.disabled = value; });
  async function ownerSave(d, draft) { const result = $('memory-owner-result'); busy(root, true); try { const r = await api('memory/owner', {device:d.device, owner:draft.owner, version:d.owner_version}); if (!r.ok) throw Error(r.error); const fresh = await api('memory'), latest = fresh.devices.find(xd => xd.device === d.device); if (!latest) throw Error('设备信息已变化，请刷新页面'); d.owner = latest.owner; d.owner_version = latest.owner_version; d.preferences = latest.preferences || []; d.memory = latest.memory; draft.owner = d.owner; draft.preferences = d.preferences.map(p => ({...p})); draft.pinned = d.memory.pinned || ''; draft.summary = d.memory.summary || ''; draft.preferenceTopic = ''; draft.preferenceContent = ''; draft.editingPreferenceId = null; draft.preferenceVersion = null; result.textContent = '已保存'; draw(); $('memory-owner-result').textContent = '已保存'; } catch(e) { result.textContent = e.message; } finally { busy(root, false); } }
  async function preferenceSave(d, draft) { const topic = $('preference-topic').value.trim(), content = $('preference-content').value.trim(), result = $('preference-result'); if (!topic || !content) { result.textContent = '请填写主题和内容'; return; } busy(root, true); try { const r = await api('memory/preferences/save', {device:d.device,topic,content,id:draft.editingPreferenceId,version:draft.preferenceVersion}); if (!r.ok) throw Error(r.error); if (draft.editingPreferenceId) draft.preferences = draft.preferences.map(p => p.id === draft.editingPreferenceId ? r.preference : p); else draft.preferences.push(r.preference); d.preferences = draft.preferences.map(p => ({...p})); draft.editingPreferenceId = null; draft.preferenceVersion = null; draft.preferenceTopic = ''; draft.preferenceContent = ''; draw(); $('preference-result').textContent = '已保存'; } catch(e) { result.textContent = e.message; } finally { busy(root, false); } }
  async function preferenceDelete(d, draft, id, version) { if (!confirm('删除这条偏好？')) return; busy(root, true); try { const r = await api('memory/preferences/delete', {device:d.device,id,version}); if (!r.ok) throw Error(r.error); draft.preferences = draft.preferences.filter(p => p.id !== id); d.preferences = draft.preferences.map(p => ({...p})); draw(); } catch(e) { $('preference-result').textContent = e.message; } finally { busy(root, false); } }
  async function memorySave(d, draft, kind, value) { const result = $('memory-'+kind+'-result'); busy(root, true); try { const r = await fetch('/admin/api/memory/save', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device:d.device,kind,summary:value,version:d.memory.version})}); const body = await r.json().catch(() => ({})); if (r.status === 409) { d.memory = body.memory || d.memory; result.textContent = '记忆刚刚更新，草稿已保留。请刷新后再保存。'; return; } if (!r.ok || body.ok === false) throw Error(body.error || '保存失败'); d.memory = body.memory; draft[kind] = value; draw(); $('memory-'+kind+'-result').textContent = value ? '已保存' : '已清空'; } catch(e) { if (!disposed) result.textContent = e.message; } finally { busy(root, false); } }
  draw();
}
