import {pageOperation} from './page.js';
const connections = new Map();
const TTL = 48 * 60 * 60 * 1000;
const writes = new Set(['open', 'navigate', 'click', 'fill', 'select', 'press', 'scroll']);
const queues = new Map();

const state = async () => (await chrome.storage.local.get(['profiles', 'receipts']));
const allowed = (url, config) => { try { return config.origins.includes(new URL(url).origin); } catch { return false; } };
let receiptQueue = Promise.resolve();
function claimReceipt(config, id, action) {
  const claimed = receiptQueue.then(async () => {
    const saved = await state();
    const current = saved.profiles?.[config.device_id];
    if (!current || current.paused || current.device_token !== config.device_token) throw new Error('controls_paused: connection was paused or removed');
    if (writes.has(action) && !current.allow_actions) throw new Error('controls_paused: enable actions in the extension');
    const receipts = Object.fromEntries(Object.entries(saved.receipts || {}).filter(([, r]) => Date.now() - r.time < TTL));
    if (receipts[id]) throw new Error('reconciliation_required: this command was already received; inspect the page before new intent');
    if (Object.keys(receipts).length >= 5000) throw new Error('receipt_capacity: wait for old receipts to expire');
    receipts[id] = {time: Date.now(), status: 'received'};
    await chrome.storage.local.set({receipts}); // Persist before actions, including worker restart/disconnect.
  });
  receiptQueue = claimed.catch(() => {});
  return claimed;
}
async function runCommand(config, message) {
  const {action, arguments: args, command_id: id} = message;
  if (!id?.match(/^[a-f0-9]{32}$/) || Date.now() - (config.clock_offset || 0) > message.deadline * 1000) throw new Error('expired_command');
  await claimReceipt(config, id, action);
  if (action === 'tabs') return {tabs: (await chrome.tabs.query({})).filter(t => allowed(t.url, config)).map(t => ({tab_id: t.id, title: t.title, url: t.url, active: t.active}))};
  if (action === 'open') {
    if (!allowed(args.url, config)) throw new Error('origin_not_allowed');
    const tab = await chrome.tabs.create({url: args.url, active: true});
    return {tab_id: tab.id, url: args.url, performed: true};
  }
  const tab = await chrome.tabs.get(args.tab_id);
  if (!allowed(tab.url, config)) throw new Error('origin_not_allowed');
  if (action === 'navigate') {
    if (!allowed(args.url, config)) throw new Error('origin_not_allowed');
    await chrome.tabs.update(tab.id, {url: args.url});
    return {tab_id: tab.id, url: args.url, performed: true};
  }
  if (action === 'screenshot') {
    if (!tab.active) throw new Error('activate_target_tab: screenshots require the selected shared tab');
    const image = await chrome.tabs.captureVisibleTab(tab.windowId, {format: 'jpeg', quality: 55});
    const active = (await chrome.tabs.query({active: true, windowId: tab.windowId}))[0];
    if (active?.id !== tab.id || active.url !== tab.url) throw new Error('tab_changed: screenshot discarded');
    return {tab_id: tab.id, url: tab.url, image: {mimeType: 'image/jpeg', data: image.split(',')[1]}};
  }
  const result = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: pageOperation, args: [action, args], world: 'ISOLATED'});
  const currentTab = await chrome.tabs.get(tab.id);
  if (!allowed(currentTab.url, config)) throw new Error('origin_changed: inspect the browser locally');
  return {tab_id: tab.id, ...result[0].result};
}
async function connect(config) {
  if (config.paused || connections.has(config.device_id)) return;
  const url = new URL(config.server); url.protocol = 'wss:'; url.pathname = '/browser-extension/connect'; url.search = ''; url.hash = '';
  const ws = new WebSocket(url); ws.authenticated = false;
  connections.set(config.device_id, ws);
  const keepalive = setInterval(() => { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type: 'ping'})); }, 20000);
  ws.onopen = () => ws.send(JSON.stringify({type: 'authenticate', device_id: config.device_id, token: config.device_token}));
  ws.onmessage = event => {
    let message; try { message = JSON.parse(event.data); } catch { ws.close(); return; }
    if (message.type === 'ready') {ws.authenticated = true; config.clock_offset = Date.now() - message.server_time * 1000; return;}
    if (message.type !== 'command') return;
    const pending = (queues.get(config.device_id) || Promise.resolve()).catch(() => {}).then(async () => {
      try {
        const result = await runCommand(config, message);
        if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type: 'result', command_id: message.command_id, ok: true, result}));
      } catch (error) {
        if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type: 'result', command_id: message.command_id, ok: false, error: String(error.message).slice(0, 1000)}));
      }
    });
    queues.set(config.device_id, pending);
    pending.finally(() => {if (queues.get(config.device_id) === pending) queues.delete(config.device_id);});
  };
  ws.onclose = event => { clearInterval(keepalive); if (connections.get(config.device_id) === ws) connections.delete(config.device_id); if (event.code !== 1008) setTimeout(reconnect, 3000); };
  ws.onerror = () => ws.close();
}
async function reconnect() {
  const {profiles = {}} = await state();
  for (const profile of Object.values(profiles)) await connect(profile);
}
chrome.alarms.create('reconnect', {periodInMinutes: 1});
chrome.alarms.onAlarm.addListener(reconnect);
chrome.runtime.onStartup.addListener(reconnect);
chrome.runtime.onInstalled.addListener(reconnect);
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id || !sender.url?.startsWith(chrome.runtime.getURL(''))) return;
  (async () => {
    if (message.type === 'refresh') {
      if (message.device_id) {const ws = connections.get(message.device_id); if (ws) ws.close(); connections.delete(message.device_id);} else {for (const ws of connections.values()) ws.close(); connections.clear();}
      await reconnect();
    }
    return {connected: [...connections].filter(([, ws]) => ws.readyState === WebSocket.OPEN && ws.authenticated).map(([id]) => id)};
  })().then(reply, error => reply({error: String(error.message)}));
  return true;
});
chrome.storage.local.setAccessLevel({accessLevel: 'TRUSTED_CONTEXTS'}).then(reconnect);
