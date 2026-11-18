const $ = id => document.getElementById(id); let csrf = '';
async function api(path, method = 'GET', body) {
  const response = await fetch(path, {method, credentials: 'same-origin', headers: {'Content-Type': 'application/json', ...(method !== 'GET' ? {'X-CSRF-Token': csrf} : {})}, ...(body ? {body: JSON.stringify(body)} : {})});
  const data = await response.json(); if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Request failed'); return data;
}
async function refresh() {
  const data = await api('/admin/browser-extension'); $('devices').replaceChildren();
  if (!data.devices.length) $('devices').textContent = 'No browser paired yet.';
  for (const device of data.devices) {
    const row = document.createElement('p'); const label = document.createElement('span');
    label.textContent = `${device.label} · ${device.client_name} · ${device.revoked ? 'Revoked' : device.connected ? 'Connected' : 'Offline'} `;
    row.append(label);
    if (!device.revoked) {
      const revoke = document.createElement('button'); revoke.className = 'quiet'; revoke.textContent = 'Revoke';
      revoke.onclick = async () => {try {await api('/admin/browser-extension/' + device.device_id, 'DELETE'); await refresh();} catch (e) {$('message').textContent = e.message;}};
      row.append(revoke);
    }
    $('devices').append(row);
  }
}
$('generate').onclick = async () => {
  try {const data = await api('/admin/browser-extension/pair', 'POST', {client_name: $('client').value}); $('pair-code').value = data.code; $('copy').disabled = false; $('message').textContent = 'Paste this code into the extension within five minutes.';} catch (e) {$('message').textContent = e.message;}
};
$('copy').onclick = () => navigator.clipboard.writeText($('pair-code').value).then(() => {$('message').textContent = 'Code copied.';});
$('refresh').onclick = () => refresh().catch(e => {$('message').textContent = e.message;});
(async () => {
  $('server').textContent = location.origin;
  const session = await api('/admin/session'); csrf = session.csrf_token;
  const clients = await api('/admin/clients');
  for (const client of clients.clients) {const option = document.createElement('option'); option.value = option.textContent = client.name; $('client').append(option);}
  $('generate').disabled = !clients.clients.length; $('message').textContent = 'Choose the client whose Chrome tabs you want to share.'; await refresh();
})().catch(e => {$('message').replaceChildren(document.createTextNode(e.message + ' ')); const link = document.createElement('a'); link.href = '/clients'; link.textContent = 'Sign in to Control center'; $('message').append(link);});
