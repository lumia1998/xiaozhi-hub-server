async function renderTasks(root) {
  let data = await api('tasks');
  let devices = data.devices || [], timer, pendingKey = null, pendingFingerprint = null;
  page.cleanup = () => clearInterval(timer);
  page.dirty = () => Boolean($('task-request')?.value.trim() || root.querySelector('.task-answer')?.value.trim());
  root.innerHTML = `<div class="page-head"><div><h1>任务</h1></div></div>
    <section class="card panel"><div class="editor-heading"><h2>新任务</h2><span class="muted small" id="task-worker-hint"></span></div><div class="task-form"><label class="f">设备<select id="task-device" class="in"></select></label><label class="f">任务内容<textarea id="task-request" class="in" rows="3" maxlength="20000" placeholder="输入需要后台处理的内容"></textarea></label><div class="actions"><span class="result" id="task-start-result" role="status"></span><button class="btn primary" id="task-start">开始任务</button></div></div></section>
    <section class="card panel"><div class="editor-heading"><h2>任务记录</h2><button class="btn" id="task-refresh">刷新</button></div><div id="task-list" class="task-list"></div></section>`;
  const escStatus = s => ({queued:'排队中',running:'处理中',waiting_input:'等待补充',succeeded:'已完成',failed:'失败',cancel_requested:'取消中',cancelled:'已取消',needs_reconciliation:'待核实'}[s] || s);
  const fillDevices = (selected) => { $('task-device').innerHTML = devices.map(d => `<option value="${esc(d)}">${esc(d)}</option>`).join(''); if (devices.includes(selected)) $('task-device').value = selected; $('task-device').disabled = !devices.length; };
  const draw = () => {
    const list = $('task-list');
    if (!data.tasks.length) { list.innerHTML = '<p class="muted small">还没有任务。</p>'; return; }
    list.innerHTML = data.tasks.map(t => `<article class="task-row"><div class="task-main"><div class="task-title"><b>${esc(t.role || '任务')}</b><span class="pill">${escStatus(t.status)}</span></div><p>${esc(t.request?.text || '')}</p>${t.result ? `<details class="task-result"><summary>查看结果</summary><pre>${esc(typeof t.result === 'string' ? t.result : JSON.stringify(t.result, null, 2))}</pre></details>` : ''}<small class="muted task-meta"><span>${esc(t.device_id)}</span><span>${new Date(t.updated * 1000).toLocaleString('zh-CN',{hour12:false})}</span></small></div><div class="task-actions">${t.status === 'waiting_input' ? `<input class="in task-answer" data-task="${esc(t.id)}" placeholder="补充信息"><button class="btn sm" data-answer="${esc(t.id)}">提交</button>` : ''}${['queued','running','waiting_input'].includes(t.status) ? `<button class="btn sm danger" data-cancel="${esc(t.id)}">取消</button>` : ''}</div></article>`).join('');
    list.querySelectorAll('[data-cancel]').forEach(b => b.onclick = () => changeTask('cancel', b.dataset.cancel));
    list.querySelectorAll('[data-answer]').forEach(b => b.onclick = () => changeTask('answer', b.dataset.answer));
  };
  async function load() {
    const requestDraft = $('task-request')?.value || '';
    const selectedDevice = $('task-device')?.value || '';
    const answers = new Map([...root.querySelectorAll('.task-answer')].map(input => [input.dataset.task, input.value]));
    const focused = document.activeElement;
    const focusId = focused?.id || '';
    const focusTask = focused?.dataset?.task || '';
    const selection = focused && typeof focused.selectionStart === 'number' ? [focused.selectionStart, focused.selectionEnd] : null;
    data = await api('tasks'); devices = data.devices || devices; fillDevices(selectedDevice); draw(); updateAvailability();
    $('task-request').value = requestDraft;
    root.querySelectorAll('.task-answer').forEach(input => { if (answers.has(input.dataset.task)) input.value = answers.get(input.dataset.task); });
    const nextFocus = focusId ? $(focusId) : focusTask ? root.querySelector(`[data-task="${focusTask}"]`) : null;
    if (nextFocus) { nextFocus.focus(); if (selection) nextFocus.setSelectionRange(...selection); }
  }
  function updateAvailability() { const available = Boolean(data.worker_available); $('task-worker-hint').textContent = available ? '' : '请先在大模型页配置默认模型'; $('task-start').disabled = !available; $('task-device').disabled = !available || !devices.length; $('task-request').disabled = !available; }
  async function changeTask(action, id) {
    const row = data.tasks.find(t => t.id === id), input = root.querySelector(`[data-task="${id}"]`);
    const body = {device: row?.device_id, task_id:id};
    if (action === 'answer') { body.answer = input?.value?.trim(); if (!body.answer) return; }
    try { await api(`tasks/${action}`, body); await load(); } catch(e) { toast(e.message, true); }
  }
  $('task-start').onclick = async () => { const request = $('task-request').value.trim(), device = $('task-device').value, result = $('task-start-result'); if (!request || !devices.length) { result.textContent = devices.length ? '请填写任务内容' : '没有可用设备'; return; } const fingerprint = JSON.stringify([device,request]); if (pendingFingerprint !== fingerprint) { pendingFingerprint = fingerprint; pendingKey = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`; } try { await api('tasks/start', {device,request,idempotency_key:pendingKey}); pendingKey = null; pendingFingerprint = null; $('task-request').value = ''; result.textContent = '已加入任务队列'; try { await load(); } catch(e) { toast('任务已加入，列表刷新失败', true); } } catch(e) { result.textContent = e.message; } };
  $('task-refresh').onclick = load;
  fillDevices(); draw(); updateAvailability();
  timer = setInterval(() => load().catch(() => {}), 5000);
}
