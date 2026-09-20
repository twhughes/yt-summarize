// Run with node; no packages or browser installation required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const shared = fs.readFileSync(path.join(__dirname, '../extension/shared.js'), 'utf8');

async function polling() {
  const requests = [];
  const replies = [
    { status: 202, data: { video_id: 'aaaaaaaaaaa', state: 'queued' } },
    { status: 202, data: { video_id: 'aaaaaaaaaaa', state: 'running' } },
    { status: 200, data: { summary: 'Finished after a long transcription' } }
  ];
  const context = vm.createContext({
    AbortController,
    setTimeout(fn, ms) { return ms === 1000 ? setTimeout(fn, 0) : setTimeout(fn, ms); },
    clearTimeout,
    async fetch(url, options) {
      requests.push([url, JSON.parse(options.body)]);
      const reply = replies.shift();
      return { status: reply.status, ok: reply.status < 400, json: async () => reply.data };
    }
  });
  vm.runInContext(shared, context);
  const result = await context.YT.requestSummary('http://localhost:8188', 'https://youtu.be/aaaaaaaaaaa');
  assert.equal(result.summary, 'Finished after a long transcription');
  assert.deepEqual(requests.map(r => r[0]), [
    'http://localhost:8188/summarize', 'http://localhost:8188/job', 'http://localhost:8188/job'
  ]);
  assert.equal(requests[1][1].id, 'aaaaaaaaaaa');
  context.fetch = async () => ({ status: 502, ok: false, json: async () => ({ error: 'Claude quota exceeded' }) });
  await assert.rejects(context.YT.requestSummary('', 'url'), /Claude quota exceeded/);
  console.log('ok polling handles queued/running/completed jobs and preserves errors');
}

function deepLink() {
  const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
  const source = html.slice(html.indexOf('  function focusFromHash()'), html.indexOf('  function reveal(id)'));
  let starts = 0;
  let button;
  const context = vm.createContext({
    location: { hash: '#chat=aaaaaaaaaaa' }, items: new Map(),
    noticeEl: { textContent: '', appendChild(value) { button = value; } },
    document: { createElement() { return { addEventListener(name, fn) { this.click = fn; } }; } },
    enqueue(id) { context.items.set(id, {}); }, save() {}, pump() { starts++; },
    render() {}, scrollTo() {}, focusChatInput() {}
  });
  vm.runInContext(source + '\nfocusFromHash();', context);
  assert.equal(starts, 0, 'Navigation must not start a summary');
  assert.equal(button.textContent, 'Summarize this video');
  button.click();
  assert.equal(starts, 1, 'Explicit click starts the job');
  assert.equal(context.items.get('aaaaaaaaaaa').chatOpen, true);
  console.log('ok chat deep link requires a click before starting work');
}

polling().then(deepLink).catch(error => { console.error(error); process.exitCode = 1; });
