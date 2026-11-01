import { readFileSync } from 'node:fs';
import { lineDiff } from './linediff.mjs';
const med = (xs) => xs.sort((a, b) => a - b)[Math.floor(xs.length / 2)];
for (const n of process.argv.slice(2)) for (const kind of ['light', 'heavy', 'other']) {
  const a = readFileSync(`a_${n}.md`, 'utf8'), b = readFileSync(`${kind}_${n}.md`, 'utf8');
  const ts = []; let r;
  for (let i = 0; i < 5; i++) { const t = performance.now(); r = lineDiff(a, b); ts.push(performance.now() - t); }
  console.log(`${String(n).padStart(5)} lines  ${kind.padEnd(5)}  median ${med(ts).toFixed(1).padStart(8)} ms   (+${r.added} -${r.removed})`);
}
