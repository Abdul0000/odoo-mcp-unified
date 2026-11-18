import assert from 'node:assert/strict';
import fs from 'node:fs';
import {webcrypto} from 'node:crypto';
const folder = new URL('../extensions/chrome/', import.meta.url);
const source = fs.readFileSync(new URL('page.js', folder), 'utf8');
const {pageOperation} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
globalThis.crypto ??= webcrypto;
class FakeElement {
  constructor(tag, name, type = '') {this.tagName = tag; this.innerText = name; this.type = type; this.isConnected = true; this.attributes = {}; this.events = []; this.clicked = 0; this.disabled = false;}
  getClientRects() {return [1];} getAttribute(key) {return this.attributes[key] || null;} closest() {return null;}
  scrollIntoView() {} focus() {} dispatchEvent(e) {this.events.push(e.type);} click() {this.clicked++;}
}
class FakeInput extends FakeElement {get value() {return this._value || '';} set value(v) {this._value = v;}}
globalThis.Element = FakeElement; globalThis.HTMLInputElement = FakeInput; globalThis.HTMLTextAreaElement = FakeInput;
globalThis.KeyboardEvent = class extends Event {constructor(type, options) {super(type, options); this.key = options.key;}};
globalThis.getComputedStyle = () => ({visibility: 'visible'});
globalThis.location = {href: 'https://client.test/web', origin: 'https://client.test'};
const input = new FakeInput('INPUT', '', 'text'); input.attributes['aria-label'] = 'Customer'; input.value = 'Original';
const password = new FakeInput('INPUT', '', 'password'); password.value = 'never-share-this-password';
const button = new FakeElement('BUTTON', 'Save');
globalThis.document = {title: 'Odoo fixture', body: {innerText: 'Customer form'}, querySelectorAll: () => [input, password, button]};
const snapshot = await pageOperation('snapshot', {});
assert.equal(snapshot.elements.length, 2); assert.ok(!JSON.stringify(snapshot).includes(password.value));
const inputRef = snapshot.elements.find(e => e.name === 'Customer').ref;
input.value = 'Manual edit';
await assert.rejects(pageOperation('fill', {snapshot_id: snapshot.snapshot_id, ref: inputRef, text: 'Overwrite'}), /stale_reference/);
assert.equal(input.value, 'Manual edit');
const fresh = await pageOperation('snapshot', {});
await pageOperation('fill', {snapshot_id: fresh.snapshot_id, ref: inputRef, text: 'Agent edit'});
assert.equal(input.value, 'Agent edit'); assert.deepEqual(input.events, ['input', 'change']);
await assert.rejects(pageOperation('click', {snapshot_id: fresh.snapshot_id, ref: 'e1'}), /stale_reference/);
const fresh2 = await pageOperation('snapshot', {});
const verified = await pageOperation('assert', {snapshot_id: fresh2.snapshot_id, ref: 'e0', expected_value: 'Agent edit'});
assert.equal(verified.assertion_passed, true);
assert.equal((await pageOperation('assert', {snapshot_id: fresh2.snapshot_id, expected_text: 'Customer form'})).assertion_passed, true);
const negative = await pageOperation('assert', {snapshot_id: fresh2.snapshot_id, ref: 'e0', expected_value: 'Wrong'});
assert.equal(negative.assertion_passed, false);
await pageOperation('click', {snapshot_id: fresh2.snapshot_id, ref: 'e1'}); assert.equal(button.clicked, 1);
let submissions = 0;
input.form = {action: 'https://client.test/save', requestSubmit: () => {submissions++;}};
input.dispatchEvent = event => event.type !== 'keydown';
const cancelled = await pageOperation('snapshot', {});
await pageOperation('press', {snapshot_id: cancelled.snapshot_id, ref: 'e0', key: 'Enter'});
assert.equal(submissions, 0, 'Cancelled Enter must not submit again');
input.dispatchEvent = () => true;
const enter = await pageOperation('snapshot', {});
await pageOperation('press', {snapshot_id: enter.snapshot_id, ref: 'e0', key: 'Enter'});
assert.equal(submissions, 1);


const id = 'a'.repeat(32); const secret = 'test-paired-device-token';
const saved = {profiles: {[id]: {server: 'https://relay.test', device_id: id, device_token: secret, origins: ['https://client.test'], allow_actions: true, paused: false}}};
let created = 0; let loadReady = true; const updates = new Set(); const sockets = [];
class FakeSocket {
  static OPEN = 1;
  constructor(url) {assert.ok(!String(url).includes(secret)); this.readyState = 1; this.sent = []; sockets.push(this); queueMicrotask(() => this.onopen?.());}
  send(raw) {const value = JSON.parse(raw); this.sent.push(value); if (value.type === 'authenticate') queueMicrotask(() => this.onmessage({data: JSON.stringify({type: 'ready', server_time: Date.now() / 1000})}));}
  close() {this.readyState = 3; this.onclose?.({code: 1008});}
}
globalThis.WebSocket = FakeSocket;
let storageProtected = false;
globalThis.chrome = {
  storage: {local: {setAccessLevel: async opts => {storageProtected = opts.accessLevel === 'TRUSTED_CONTEXTS';}, get: async () => structuredClone(saved), set: async values => Object.assign(saved, structuredClone(values))}},
  tabs: {query: async () => [{id: 12, url: 'https://client.test/web', title: 'Odoo'}, {id: 13, url: 'https://unrelated.test', title: 'Private other tab'}],
    create: async ({url}) => {created++; return {id: 20, url};}, get: async id => id === 20 ? {id: 20, url: 'https://client.test/web', status: loadReady ? 'complete' : 'loading'} : {id: 12, windowId: 1, url: 'https://unrelated.test', status: 'complete'}, onUpdated: {addListener: fn => updates.add(fn), removeListener: fn => updates.delete(fn)}},
  alarms: {create() {}, onAlarm: {addListener() {}}},
  runtime: {id: 'test-extension', getURL: () => 'chrome-extension://test-extension/', onStartup: {addListener() {}}, onInstalled: {addListener() {}}, onMessage: {addListener() {}}},
};
let background = fs.readFileSync(new URL('background.js', folder), 'utf8').replace("import {pageOperation} from './page.js';", 'const pageOperation = () => {};');
await import('data:text/javascript;base64,' + Buffer.from(background).toString('base64')).catch(error => {throw new Error(error.message);});
const wait = async predicate => {for (let i = 0; i < 200; i++) {if (predicate()) return; await new Promise(r => setTimeout(r, 5));} throw new Error('Timed out waiting for extension callback');};
await wait(() => sockets[0]?.authenticated);
assert.equal(storageProtected, true); const ws = sockets[0];
const issue = async (command_id, action, args) => {
  const before = ws.sent.filter(e => e.type === 'result').length;
  ws.onmessage({data: JSON.stringify({type: 'command', command_id, action, arguments: args, deadline: Date.now() / 1000 + 60})});
  await wait(() => ws.sent.filter(e => e.type === 'result').length > before);
  return ws.sent.filter(e => e.type === 'result').at(-1);
};
try {
  const tabs = await issue('b'.repeat(32), 'tabs', {});
  assert.equal(tabs.result.tabs.length, 1); assert.ok(!JSON.stringify(tabs.result).includes('Private other tab'));
  const open = await issue('c'.repeat(32), 'open', {url: 'https://client.test/web'}); assert.equal(open.ok, true);
  const replay = await issue('c'.repeat(32), 'open', {url: 'https://client.test/web'}); assert.equal(replay.ok, false); assert.equal(created, 1);
  const foreign = await issue('d'.repeat(32), 'open', {url: 'https://unrelated.test'}); assert.equal(foreign.ok, false); assert.equal(created, 1);
  const wrongTab = await issue('e'.repeat(32), 'snapshot', {tab_id: 12}); assert.equal(wrongTab.ok, false); assert.match(wrongTab.error, /origin_not_allowed/);
  assert.equal(saved.receipts['c'.repeat(32)].status, 'received');
  saved.profiles[id].allow_actions = false;
  const paused = await issue('f'.repeat(32), 'open', {url: 'https://client.test/web'});
  assert.equal(paused.ok, false); assert.equal(created, 1);
  saved.profiles[id].allow_actions = true; loadReady = false;
  const loading = issue('0'.repeat(32), 'open', {url: 'https://client.test/web'});
  await wait(() => updates.size > 0);
  assert.ok(!ws.sent.some(message => message.command_id === '0'.repeat(32)));
  loadReady = true;
  for (const update of [...updates]) update(20, {status: 'complete'}, {id: 20, url: 'https://client.test/web', status: 'complete'});
  assert.equal((await loading).ok, true);

  console.log('DOM stale/CAS, password exclusion, input events, assertions, origin isolation and durable no-replay passed');
} finally {for (const socket of sockets) socket.close();}
