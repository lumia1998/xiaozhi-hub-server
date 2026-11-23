async function renderSpeaker(root) {
  const view = document.createElement('section');
  root.replaceChildren(view);
  view.innerHTML = `<div class="page-head"><h1>音箱预览</h1></div><label>在线音箱 <select class="in"></select></label>
    <button class="btn" data-refresh>刷新设备</button><p data-note role="status"></p>
    <iframe hidden title="音箱实时界面" referrerpolicy="no-referrer" style="width:100%;aspect-ratio:1024/600;border:0"></iframe>
    <h2>家居卡片</h2><p class="muted">勾选要在音箱显示的设备。移除卡片不会删除 HA 设备或修改控制权限。</p>
    <div data-list></div><div class="actions"><button class="btn primary" data-save disabled>保存布局</button><button class="btn" data-load>重新读取</button></div><p data-result role="status"></p>`;
  const get = selector => view.querySelector(selector);
  const screen=get('iframe'), select=get('select'), note=get('[data-note]');
  function display() {
    screen.hidden=!select.value;
    if(select.value) screen.src='/admin/speaker/'+encodeURIComponent(select.value)+'/'; else screen.removeAttribute('src');
    note.textContent=select.value?'实时连接。长按使用音箱麦克风，声音从音箱播放；家居开关会操作真实设备。':'没有已连接预览的音箱，请检查客户端版本和设备配对。';
  }
  async function screens(){
    try {const response=await api('devices'); if(!view.isConnected)return;
      const old=select.value; select.replaceChildren(...(response.screens||[]).map(id=>new Option(id,id)));
      if([...select.options].some(o=>o.value===old)) select.value=old; display();
    } catch(error){note.textContent=error.message;}
  }
  select.onchange=display; get('[data-refresh]').onclick=screens;
  const list=get('[data-list]'), save=get('[data-save]'), result=get('[data-result]');
  let revision, dirty=false;
  page.dirty=()=>dirty;
  async function load(){
    if(dirty&&!confirm('放弃未保存的卡片选择？'))return;
    save.disabled=true; result.textContent='正在读取 HA 设备…';
    try {const data=await api('speaker/cards');if(!view.isConnected)return;
      revision=data.revision;list.replaceChildren();const selected=new Set(data.selected);
      const rooms=new Map();
      for(const card of data.cards){
        if(!rooms.has(card.room)){const group=document.createElement('fieldset'),legend=document.createElement('legend');legend.textContent=card.room;group.append(legend);rooms.set(card.room,group);list.append(group);}
        const label=document.createElement('label'),check=document.createElement('input');
        check.type='checkbox';check.value=card.id;check.checked=selected.has(card.id);check.onchange=()=>{dirty=true;save.disabled=false;};
        label.append(check,document.createTextNode(' '+card.name));label.style.cssText='display:inline-flex;align-items:center;min-height:44px;margin-right:16px';rooms.get(card.room).append(label);
      }
      dirty=false;result.textContent=`已显示 ${selected.size} 张，共 ${data.cards.length} 台设备`;
    } catch(error){result.textContent=error.message;}
  }
  save.onclick=async()=>{
    save.disabled=true;list.inert=true;get('[data-load]').disabled=true;
    try {const data=await api('speaker/cards',{revision,selected:[...list.querySelectorAll('input:checked')].map(c=>c.value)});
      revision=data.revision;dirty=false;result.textContent=`已保存 ${data.selected.length} 张卡片`;
    }catch(error){result.textContent=error.message;save.disabled=false;}
    finally{list.inert=false;get('[data-load]').disabled=false;}
  };
  get('[data-load]').onclick=load; await Promise.all([screens(),load()]);
}
