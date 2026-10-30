// 실제 문서(공개 레포들의 마크다운을 이어 붙인 base_all.md)로 편집 시나리오를 만든다. 결정적(seed 고정).
import { readFileSync, writeFileSync } from 'node:fs';
let seed = 42; const rnd = () => (seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648;
const all = readFileSync('base_all.md', 'utf8').split('\n');
const sizes = [1000, 2000, all.length];
const edit = (lines, rate, rewrite) => lines.map((l) => {
  if (rnd() >= rate || !l.trim()) return l;
  if (rewrite) return l.split('').reverse().join('') + ' (재작성)';        // 줄 전체를 바꿈
  const w = l.split(' '); w[Math.floor(rnd() * w.length)] = '수정됨'; return w.join(' ');  // 낱말 하나 바꿈
});
for (const n of sizes) {
  const a = all.slice(0, n);
  writeFileSync(`a_${n}.md`, a.join('\n'));
  writeFileSync(`light_${n}.md`, edit(a, 0.03, false).join('\n'));
  writeFileSync(`heavy_${n}.md`, edit(a, 0.40, true).join('\n'));
  // 재구성된 판: 같은 문단들을 순서를 섞어 다시 배치 + 30% 재작성 (Claude 가 문서를 갈아엎은 경우)
  const paras = a.join('\n').split(/\n\s*\n/); for (let i = paras.length - 1; i > 0; i--) { const j = Math.floor(rnd() * (i + 1)); [paras[i], paras[j]] = [paras[j], paras[i]]; }
  writeFileSync(`other_${n}.md`, edit(paras.join('\n\n').split('\n'), 0.3, true).join('\n'));
}
