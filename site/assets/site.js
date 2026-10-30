// AOK 안내서 공용 스크립트: 브리핑 템플릿 복사 버튼.
document.querySelectorAll('button.copy').forEach((btn) => {
  btn.addEventListener('click', () => {
    const el = document.getElementById(btn.dataset.target);
    if (!el) return;
    const done = (ok) => {
      btn.textContent = ok ? '복사됨' : '선택됨 — 복사하세요';
      setTimeout(() => { btn.textContent = '복사'; }, 1800);
    };
    const select = () => {
      const r = document.createRange();
      r.selectNodeContents(el);
      const s = window.getSelection();
      s.removeAllRanges();
      s.addRange(r);
      done(false);
    };
    try {
      navigator.clipboard.writeText(el.textContent).then(() => done(true), select);
    } catch (e) {
      select();
    }
  });
});
