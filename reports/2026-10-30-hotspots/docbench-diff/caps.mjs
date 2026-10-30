import { readFileSync } from 'node:fs';
import { diffLines } from 'diff';
const a = readFileSync('a_4334.md', 'utf8'), b = readFileSync('other_4334.md', 'utf8');
for (const opts of [{}, { timeout: 200 }, { maxEditLength: 1000 }]) {
  const t = performance.now(); const r = diffLines(a, b, opts); const ms = performance.now() - t;
  console.log(JSON.stringify(opts).padEnd(22), `${ms.toFixed(0)} ms`, r === undefined ? '→ undefined (gave up)' : `→ ${r.length} parts`);
}
