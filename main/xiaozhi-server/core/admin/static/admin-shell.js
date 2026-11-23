(() => {
  const icon = name => `<svg class="ui-icon" aria-hidden="true"><use href="/admin/assets/admin-icons.svg#${name}"></use></svg>`;
  const names={status:'layout-dashboard',llm:'brain',skills:'book',mcp:'plug',stickers:'robot',calls:'phone',memory:'history',tasks:'list-checks',asr:'microphone',tts:'volume',intent:'settings',how:'route'};
  names.speaker='robot';
  document.querySelectorAll('#nav [data-page]').forEach(b=>{b.querySelector('.ico').innerHTML=icon(names[b.dataset.page]);});
  document.querySelectorAll('.logo').forEach(el=>el.innerHTML=icon('robot'));
  $('nav-toggle').innerHTML=icon('menu-2');
  $('nav-toggle').onclick=()=>{const opened=document.body.classList.toggle('nav-open');$('nav-toggle').setAttribute('aria-expanded',String(opened));if(opened)document.querySelector('#nav .active')?.focus();};
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&document.body.classList.contains('nav-open')){document.body.classList.remove('nav-open');$('nav-toggle').setAttribute('aria-expanded','false');$('nav-toggle').focus();}});
  document.addEventListener('click',e=>{if(document.body.classList.contains('nav-open')&&!e.target.closest('aside,#nav-toggle')){document.body.classList.remove('nav-open');$('nav-toggle').setAttribute('aria-expanded','false');}});
  const media=matchMedia('(prefers-color-scheme: dark)');
  const apply=()=>{let saved;try{saved=localStorage.getItem('hub-theme');}catch{}const dark=saved?saved==='dark':media.matches;document.documentElement.classList.toggle('dark',dark);document.documentElement.classList.toggle('light',!dark);$('theme-toggle').innerHTML=icon(dark?'sun':'moon');$('theme-toggle').setAttribute('aria-label',dark?'切换浅色主题':'切换深色主题');};
  $('theme-toggle').onclick=()=>{const next=document.documentElement.classList.contains('dark')?'light':'dark';try{localStorage.setItem('hub-theme',next);}catch{}apply();};
  media.addEventListener('change',apply);apply();
})();
