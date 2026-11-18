const $ = id => document.getElementById(id);
const message = text => {$('message').textContent = text;};
async function profiles() {return (await chrome.storage.local.get('profiles')).profiles || {};}
async function render() {
  const saved = await profiles();
  const state = await chrome.runtime.sendMessage({type: 'status'});
  $('profiles').replaceChildren();
  for (const [id, profile] of Object.entries(saved)) {
    const article = document.createElement('article');
    const title = document.createElement('strong'); title.textContent = profile.client_name;
    const status = document.createElement('p'); status.textContent = state.connected?.includes(id) ? 'Connected' : (profile.paused ? 'Paused' : 'Reconnecting…');
    const url = document.createElement('p'); url.className = 'origin'; url.textContent = profile.origins.join(', ');
    const label = document.createElement('label'); const controls = document.createElement('input'); controls.type = 'checkbox'; controls.checked = !!profile.allow_actions;
    label.append(controls, document.createTextNode('Allow browser actions'));
    const connect = document.createElement('button'); connect.textContent = 'Connect / apply';
    connect.onclick = async () => {
      try {
        const granted = await chrome.permissions.request({origins: profile.origins.includes('*') ? ['https://*/*', 'http://*/*'] : profile.origins.map(origin => origin + '/*')});
        if (!granted) throw new Error('Website permission was not granted.');
        const latest = await profiles(); latest[id].paused = false; latest[id].allow_actions = controls.checked;
        await chrome.storage.local.set({profiles: latest});
        await chrome.runtime.sendMessage({type: 'refresh', device_id: id});
        message('Connecting. Keep Chrome open; the MCP can now inspect your browser.'); await render();
      } catch (e) {message(e.message);}
    };
    const pause = document.createElement('button'); pause.textContent = 'Pause'; pause.className = 'secondary';
    pause.onclick = async () => {
      const latest = await profiles(); latest[id].paused = true; await chrome.storage.local.set({profiles: latest});
      await chrome.runtime.sendMessage({type: 'refresh', device_id: id}); message('Client connection paused.'); await render();
    };
    const remove = document.createElement('button'); remove.textContent = 'Remove'; remove.className = 'secondary';
    remove.onclick = async () => {
      const latest = await profiles(); delete latest[id]; await chrome.storage.local.set({profiles: latest});
      await chrome.runtime.sendMessage({type: 'refresh', device_id: id}); message('Pairing removed from this Chrome. Revoke it in Control center to invalidate its server credential.'); await render();
    };
    article.append(title, status, url, label, connect, pause, remove); $('profiles').append(article);
  }
}
$('pair').onsubmit = async event => {
  event.preventDefault();
  try {
    const url = new URL($('server').value);
    if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash || !['', '/'].includes(url.pathname)) throw new Error('Use the HTTPS server origin without paths, query parameters or credentials.');
    const granted = await chrome.permissions.request({origins: [url.origin + '/*']});
    if (!granted) throw new Error('Server permission was not granted.');
    const response = await fetch(url.origin + '/browser-extension/pair', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({code: $('code').value.trim(), extension_id: chrome.runtime.id, label: $('label').value.trim()})});
    const result = await response.json(); if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Pairing failed. Generate a new code.');
    const saved = await profiles(); saved[result.device_id] = {...result, server: url.origin, allow_actions: false, paused: true};
    await chrome.storage.local.set({profiles: saved}); $('code').value = '';
    message('Paired. Enable actions if needed, then click Connect to grant this client site.'); await render();
  } catch (e) {message(e.message);}
};
render().catch(e => message(e.message));
