import { readFileSync } from 'node:fs';
import { diffLines, diffWordsWithSpace } from 'diff';
for (const n of process.argv.slice(2)) for (const kind of ['heavy', 'other']) {
  const a = readFileSync(`a_${n}.md`, 'utf8'), b = readFileSync(`${kind}_${n}.md`, 'utf8');
  let t = performance.now(); const parts = diffLines(a, b); const tl = performance.now() - t;
  t = performance.now(); let pairs = 0;
  for (let i = 0; i < parts.length; i++) { const p = parts[i], q = parts[i + 1];
    if (p.removed && q && q.added) { const ls = p.value.replace(/\n$/, '').split('\n'), ns = q.value.replace(/\n$/, '').split('\n');
      if (ls.length === ns.length) ls.forEach((l, j) => { diffWordsWithSpace(l, ns[j]); pairs++; }); i++; } }
  const tw = performance.now() - t;
  console.log(`${n} ${kind}: diffLines ${tl.toFixed(0)} ms · word diffs ${tw.toFixed(0)} ms over ${pairs} line pairs`);
}
