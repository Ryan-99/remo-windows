// Executes the production script with mocked Frida IO; never injects a process.
const vm = require('node:vm');
const {spawnSync} = require('node:child_process');
const assert = require('node:assert/strict');
const [python, source] = process.argv.slice(2);
const loaded = spawnSync(python, ['-c', 'from ovb_rc003.frida_hid_tap_runtime import GADGET_SCRIPT; print(GADGET_SCRIPT)'], {
  env: {...process.env, PYTHONPATH: source}, encoding: 'utf8'
});
assert.equal(loaded.status, 0, loaded.stderr);
const sent = [];
const timers = [];
let hooks = [], detachCount = 0, finishRead, heartbeat;
const context = {
  Uint8Array, Promise, JSON,
  setTimeout: callback => { timers.push(callback); return timers.length; },
  setInterval: callback => { heartbeat = callback; return 1; },
  Process: {id: 42, findModuleByName: () => ({findExportByName: () => ({})})},
  Interceptor: {attach: (_target, callbacks) => {
    hooks.push(callbacks); return {detach: () => { detachCount++; }};
  }},
  Socket: {connect: async () => ({
    output: {writeAll: async bytes => sent.push(JSON.parse(Buffer.from(bytes).toString()))},
    input: {read: () => new Promise(resolve => { finishRead = resolve; })},
    close: async () => {}
  })},
  rpc: {exports: {}}
};
const flush = () => new Promise(resolve => setImmediate(resolve));
function report(usage, status = 0, capacity = 9) {
  const args = Array.from({length: 10}, () => ({toUInt32: () => 0}));
  args[5] = {toUInt32: () => 0x80018483};
  args[9] = {toUInt32: () => capacity};
  args[8] = {isNull: () => false, readByteArray: () => Uint8Array.from([1,0,0,usage & 255,usage >> 8,0,0,0,0]).buffer};
  const call = {};
  hooks.at(-1).onEnter.call(call, args);
  hooks.at(-1).onLeave.call(call, {toUInt32: () => status});
}
(async () => {
  vm.createContext(context);
  vm.runInContext(loaded.stdout, context);
  await context.rpc.exports.init('test', {});
  await flush();
  assert.equal(sent[0].hook_installed, true);
  assert.equal(sent[0].protocol, 'observe-v1');
  const usages = [0x3e,0xf1,0x28,0x35,0x4a,0x4f,0x50,0x51,0x52,0x65,0x66,0x80,0x81];
  for (const usage of usages) { report(usage); report(0); }
  report(0xf1, 0x103);
  report(0xf1, 0, 121);
  await flush();
  const frames = sent.filter(item => item.kind === 'gatt_read');
  assert.equal(frames.length, 26);
  frames.forEach((item, index) => { assert.equal(item.seq, index + 1); assert.equal(item.pid, 42); });
  heartbeat();
  await flush();
  assert.equal(sent.at(-1).counts.pending, 1);
  finishRead(new ArrayBuffer(0));
  await flush();
  assert.equal(detachCount, 1, 'socket EOF must detach the hook');
  assert.equal(timers.length, 1);
  timers.shift()();
  await flush();
  assert.equal(hooks.length, 2, 'reconnect must install a fresh hook');
  assert.equal(sent.at(-1).kind, 'ready');
  // A slow connect and heartbeat retry must never create overlapping sockets.
  finishRead(new ArrayBuffer(0));
  await flush();
  let connectCount = 0;
  context.Socket.connect = () => { connectCount++; return new Promise(() => {}); };
  timers.shift()();
  await flush();
  heartbeat();
  timers.shift()();
  await flush();
  assert.equal(connectCount, 1, 'connection attempts must be single-flight');
  console.log('PASS: 13 down/up reports, sequence, pending counter, EOF detach, reconnect handshake');
})().catch(error => { console.error(error); process.exitCode = 1; });
