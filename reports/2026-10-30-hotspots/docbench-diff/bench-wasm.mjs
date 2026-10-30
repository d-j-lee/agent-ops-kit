// WASM(Rust) lineDiff 를 브라우저와 같은 V8 에서 잰다: 문자열 복사 → diff → JSON 결과 → JSON.parse 까지 포함
import { readFileSync } from 'node:fs';
import { lineDiff } from './linediff.mjs';
const { instance } = await WebAssembly.instantiate(readFileSync(process.argv[2]));
const w = instance.exports, enc = new TextEncoder(), dec = new TextDecoder();
function wasmLineDiff(a, b, context = 2) {
  const ab = enc.encode(a), bb = enc.encode(b);
  const pa = w.alloc(ab.length), pb = w.alloc(bb.length);
  new Uint8Array(w.memory.buffer, pa, ab.length).set(ab);
  new Uint8Array(w.memory.buffer, pb, bb.length).set(bb);
  const po = w.diff_json(pa, ab.length, pb, bb.length, context), n = w.out_len();
  const out = JSON.parse(dec.decode(new Uint8Array(w.memory.buffer, po, n)));
  w.dealloc(pa, ab.length); w.dealloc(pb, bb.length); w.dealloc(po, n);
  return out;
}
const med = (xs) => xs.sort((x, y) => x - y)[Math.floor(xs.length / 2)];
for (const n of process.argv.slice(3)) for (const kind of ['light', 'heavy', 'other']) {
  const a = readFileSync(`a_${n}.md`, 'utf8'), b = readFileSync(`${kind}_${n}.md`, 'utf8');
  const ts = []; let r; for (let i = 0; i < 5; i++) { const t = performance.now(); r = wasmLineDiff(a, b); ts.push(performance.now() - t); }
  const js = lineDiff(a, b);
  // 화면이 그리는 것과 같은지: 줄 종류·글자 순서가 같고, 낱말 조각을 이어 붙이면 줄 글자와 같은지
  const sig = (res) => res.lines.map((l) => l.t + ':' + (l.text ?? l.count)).join('\n');
  const wordsOk = r.lines.every((l) => !l.words || l.words.map((x) => x.text).join('') === l.text);
  console.log(`${String(n).padStart(5)} lines  ${kind.padEnd(5)}  wasm median ${med(ts).toFixed(1).padStart(7)} ms  (+${r.added} -${r.removed})  same lines as jsdiff: ${sig(r) === sig(js)}  words rejoin: ${wordsOk}`);
}
