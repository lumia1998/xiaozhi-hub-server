(() => {
  const base = location.pathname.replace(/\/$/, '');
  const nativeFetch = window.fetch.bind(window);
  window.fetch = (input, options) => {
    if (typeof input !== 'string' || !input.startsWith('/api/')) return nativeFetch(input, options);
    const headers = new Headers(options?.headers); headers.set('X-Speaker-Console', '1');
    return nativeFetch(base + input, {...options, headers});
  };
  const Source = window.EventSource;
  window.EventSource = class extends Source { constructor(url, options) { super(url === '/events' ? base + url : url, options); } };
  document.addEventListener('DOMContentLoaded', () => {
    for (const id of ['serverSave','serverCheck','powerBtn']) {
      const control = document.getElementById(id);
      if (control) { control.disabled = true; control.title = '请在音箱上使用'; }
    }
    const mic=document.getElementById('btnMic');
    mic.title='长按使用音箱麦克风'; mic.setAttribute('aria-label',mic.title);
  });
})();
