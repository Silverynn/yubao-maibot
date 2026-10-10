import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source=await readFile(new URL('../web/audio.js',import.meta.url),'utf8');
const {wavBase64,fastSignal}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const raw=Buffer.from(wavBase64([new Float32Array(4800).fill(.5)],48000),'base64');
assert.equal(raw.toString('ascii',0,4),'RIFF');assert.equal(raw.readUInt32LE(24),16000);
assert.equal(raw.readUInt16LE(22),1);assert.equal(raw.length,3244);assert.equal(raw.readInt16LE(44),16383);
assert.equal(fastSignal('停一下！'),true);assert.equal(fastSignal('你先停一下看看灯塔'),false);
console.log('浏览器音频与快速信号：7项通过');
