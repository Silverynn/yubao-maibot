// Run the real compiled audio handler with mocked audio/model dependencies.
// This catches the regression where expressions run only inside if(audio).
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {dirname, resolve} from 'node:path';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
let bundlePath = process.argv[2];
if (!bundlePath) {
  const index = readFileSync(resolve(root, 'frontend/index.html'), 'utf8');
  bundlePath = resolve(root, 'frontend', index.match(/src="\.\/(assets\/main-[^"]+\.js)"/)[1]);
}
const bundle = readFileSync(bundlePath, 'utf8');
const payload = bundle.indexOf('audioBase64:dt,displayText:ft');
assert.ok(payload > 0, 'known frontend audio payload handler');
const begin = bundle.indexOf('try{', payload);
const end = bundle.indexOf('});return reactExports.useEffect', begin);
assert.ok(end > begin, 'complete frontend audio handler');
const run = new Function('dt','ht','window','st','ot','console','LAppDefine',
  'PriorityNormal','Audio','audioManager','it','toaster','i',
  'var mt,yt;' + bundle.slice(begin,end));

function execute(audio, expressions) {
  const calls = [];
  const adapter = {};
  const win = {
    getLAppAdapter: () => adapter,
    getLive2DManager: () => ({getModel: () => ({startRandomMotion(){}})}),
  };
  class FakeAudio {addEventListener(){} load(){}}
  run(audio,expressions,win,(name,a)=>{assert.equal(a,adapter);calls.push(name);},
    ()=>{}, {log(){},warn(){},error(){}}, {}, 1, FakeAudio,
    {setCurrentAudio(){}}, {current:{aiState:'idle'}},
    {create(){throw Error('unexpected playback error');}}, x=>x);
  return calls;
}
assert.deepEqual(execute('', ['开心兴奋']), ['开心兴奋'], 'silent reply must apply expression');
assert.deepEqual(execute('fake-audio', ['生气']), ['生气'], 'spoken reply applies expression once');
assert.deepEqual(execute('', null), [], 'later text must not reset the current expression');
assert.deepEqual(execute('', [0]), [0], 'expression index zero is valid');
console.log('PASS: actual frontend handler applies silent/spoken expressions once; absent and index-zero actions');
