import {useEffect, useLayoutEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {flushSync} from 'react-dom';
import {Theme} from '@astryxdesign/core/theme';
import {Button} from '@astryxdesign/core/Button';
import {Heading} from '@astryxdesign/core/Heading';
import {neutralTheme} from '@astryxdesign/theme-neutral/built';
import '@astryxdesign/core/reset.css';
import '@astryxdesign/core/astryx.css';
import '@astryxdesign/theme-neutral/theme.css';
import './console.css';
export {mountSpeaker} from './speaker';

const groups = [
  ['概览', [['status','状态','layout-dashboard'],['speaker','音箱预览','robot']]],
  ['模型与能力', [['llm','大模型','brain'],['skills','Skill','book'],['mcp','MCP','plug'],['stickers','表情包','robot'],['calls','呼名','phone'],['memory','记忆','history'],['tasks','任务','list-checks']]],
  ['语音与设备', [['asr','语音识别','microphone'],['tts','语音合成','volume'],['intent','意图识别','settings']]],
  ['帮助', [['how','工作原理','route']]],
];
const links = groups.flatMap(([,items]) => items);
const Icon = ({name}) => <svg className="ui-icon" aria-hidden="true"><use href={'/admin/assets/admin-icons.svg#'+name}/></svg>;
const Brand = () => <div className="brand"><div className="logo"><Icon name="robot"/></div><span>小智 Hub</span></div>;
const systemTheme = matchMedia('(prefers-color-scheme: dark)');
function preferredTheme() {
  try {const saved = localStorage.getItem('hub-theme'); if (['dark','light'].includes(saved)) return saved;} catch {}
  return systemTheme.matches ? 'dark' : 'light';
}

function SignIn({mode, submit, reload}) {
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);
  const setup = mode==='setup';
  async function handleSubmit(event) {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    const secret = fields.get('password');
    if (setup && secret!==fields.get('repeat')) {setError('两次输入的密码不一致'); return;}
    setPending(true); setError('');
    try {await submit(mode,secret);} catch (failure) {setError(failure.message);} finally {setPending(false);}
  }
  return <main id="auth" className="auth"><form id="auth-form" className="card" onSubmit={handleSubmit}>
    <Brand/>{mode==='loading'?<p role="status">正在连接管理后台…</p>: mode==='error'?<><p role="alert">连不上管理后台</p><Button label="重试" onClick={reload}/></>:<>
      <Heading level={1}>{setup?'设置管理后台密码':'登录'}</Heading>
      <input type="text" name="username" value="admin" autoComplete="username" hidden readOnly/>
      <label className="f">密码<input autoFocus required minLength={setup?6:undefined} placeholder={setup?'至少 6 位':undefined} name="password" className="in" type="password" autoComplete={setup?'new-password':'current-password'}/></label>
      {setup && <label className="f">再次输入<input required name="repeat" className="in" type="password" autoComplete="new-password"/></label>}
      {error && <p className="error-text" role="alert">{error}</p>}<Button type="submit" label={setup?'设置并进入':'登录'} variant="primary" isLoading={pending}/>
    </>}
  </form></main>;
}

function Shell({state, actions}) {
  const [theme, setTheme] = useState(preferredTheme);
  const [expanded,setExpanded] = useState(false);
  const toggle = useRef(null), aside = useRef(null);
  useLayoutEffect(() => {
    document.documentElement.classList.toggle('dark',theme==='dark');
    document.documentElement.classList.toggle('light',theme==='light');
  },[theme]);
  useEffect(() => {
    const change = () => setTheme(preferredTheme()); systemTheme.addEventListener('change',change);
    return () => systemTheme.removeEventListener('change',change);
  },[]);
  useLayoutEffect(() => {
    if (!expanded || state.mode!=='app') return;
    document.body.classList.add('nav-open'); aside.current.querySelector('[aria-current="page"]')?.focus();
    const escape = event => {if(event.key==='Escape'){setExpanded(false);toggle.current?.focus();}};
    const outside = event => {if(!aside.current?.contains(event.target) && !toggle.current?.contains(event.target))setExpanded(false);};
    document.addEventListener('keydown',escape);document.addEventListener('click',outside);
    return () => {document.body.classList.remove('nav-open');document.removeEventListener('keydown',escape);document.removeEventListener('click',outside);};
  },[expanded,state.mode]);
  function switchTheme() {const next = theme==='light'?'dark':'light'; try{localStorage.setItem('hub-theme',next);}catch{}setTheme(next);}
  const title = links.find(([id])=>id===state.page)?.[1] || '状态';
  return <Theme theme={neutralTheme} mode={theme}>{state.mode!=='app'?<SignIn key={state.mode} mode={state.mode} submit={actions.submit} reload={actions.reload}/>:<div id="app" className="app">
    <aside ref={aside}><Brand/><nav className="nav" id="nav" aria-label="后台导航">{groups.map(([name,items])=><div key={name}><div className="nav-label">{name}</div>{items.map(([id,label,image])=><button key={id} data-page={id} aria-current={state.page===id?'page':undefined} className={state.page===id?'active':''} onClick={() => {if(actions.navigate(id)!==false)setExpanded(false);}}><Icon name={image}/>{label}</button>)}</div>)}</nav>
      <div className="side-foot"><span className={'dot '+state.onlineClass}/>{state.online}<div className="row"><button id="logout" onClick={actions.logout}>退出登录</button><a href="https://github.com/justhalfbit/xiaozhi-hub-server" target="_blank" rel="noopener">GitHub ↗</a></div></div>
    </aside><div className="workspace"><header className="workspace-bar"><div className="workspace-breadcrumb"><button ref={toggle} className="icon-button" id="nav-toggle" aria-label="展开导航" aria-controls="nav" aria-expanded={expanded} onClick={()=>setExpanded(!expanded)}><Icon name="menu-2"/></button><span>管理后台</span><span>/</span><strong>{title}</strong></div><button id="theme-toggle" className="icon-button" aria-label={theme==='light'?'切换深色主题':'切换浅色主题'} onClick={switchTheme}><Icon name={theme==='light'?'moon':'sun'}/></button></header><main id="page"/></div>
  </div>}</Theme>;
}

export function mountShell(actions) {
  const root = createRoot(document.getElementById('console-ui'));
  let state = {mode:'loading', page:'status',online:'正在查询设备',onlineClass:''};
  function update(changes) {state={...state,...changes};flushSync(()=>root.render(<Shell {...{state,actions}}/>));}
  update({}); return update;
}

function Status({status, modules, open, refresh, copy, devices, pairing}) {
  const addresses = [['本机 IP',status.addresses.ip],['OTA 地址',status.addresses.ota],['管理后台',status.addresses.admin]];
  useLayoutEffect(()=>{devices();pairing();},[devices,pairing]);
  return <article className="status-board"><header className="board-heading"><Heading level={1}>状态</Heading><Button label="刷新" onClick={refresh}/></header>
    <section className="module-strip" aria-label="当前模块">{modules.map(({title,value,detail,route,warning})=><button data-go={route} key={route} onClick={()=>open(route)}><div><Icon name={links.find(([id])=>id===route)?.[2]}/>{title}<span aria-hidden="true">↗</span></div><strong>{value}</strong><small className={warning?'module-warning':''}>{detail}</small></button>)}</section>
    <div className="board-columns"><div>
      <section className="board-section"><Heading level={2}>在线设备</Heading><div id="devices"/></section>
      <section className="board-section"><Heading level={2}>设备配对</Heading><div id="pairing-panel"/></section>
    </div><div>
      <section className="board-section"><Heading level={2}>连接地址</Heading><dl className="address-list">{addresses.map(([name,value])=><div key={name}><dt>{name}</dt><dd><code>{value}</code><Button size="sm" label="复制" aria-label={'复制'+name} onClick={()=>copy(value)}/></dd></div>)}</dl><p className="small muted">设备配网时填写 OTA 地址，建议为 Hub 分配固定 IP。</p></section>
      <section className="board-section"><Heading level={2}>环境自检</Heading>{status.checks.map(check=><div className="check-row" key={check.name}><strong>{check.name}</strong><span className={'pill '+({ok:'ok',warn:'warn',error:'err'}[check.level]||'')}>{({ok:'正常',warn:'注意',error:'异常'}[check.level]||check.level)}</span><small>{check.detail}</small></div>)}</section>
    </div></div>
  </article>;
}

export function mountStatus(element, props) {
  const root = createRoot(element); flushSync(()=>root.render(<Status {...props}/>)); return ()=>root.unmount();
}
