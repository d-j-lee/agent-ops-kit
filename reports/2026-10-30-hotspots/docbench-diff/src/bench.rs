use std::time::Instant;
fn main() {
    let dir = std::env::args().nth(1).unwrap();
    for n in std::env::args().skip(2) {
        let a = std::fs::read_to_string(format!("{dir}/a_{n}.md")).unwrap();
        for kind in ["light", "heavy", "other"] {
            let b = std::fs::read_to_string(format!("{dir}/{kind}_{n}.md")).unwrap();
            // 정확성: 접지 않은 결과에서 A·B 를 그대로 복원할 수 있어야 한다
            let full = docdiff::line_diff(&a, &b, None);
            let (mut ra, mut rb) = (Vec::new(), Vec::new());
            for l in &full.lines { match l {
                docdiff::DiffLine::Ctx { text } => { ra.push(text.as_str()); rb.push(text.as_str()); }
                docdiff::DiffLine::Del { text, .. } => ra.push(text.as_str()),
                docdiff::DiffLine::Add { text, .. } => rb.push(text.as_str()),
                docdiff::DiffLine::Skip { .. } => unreachable!(),
            }}
            let ok = ra.join("\n") == a.strip_suffix('\n').unwrap_or(&a) && rb.join("\n") == b.strip_suffix('\n').unwrap_or(&b);
            let mut ts = Vec::new(); let mut r = None;
            for _ in 0..5 { let t = Instant::now(); let x = docdiff::line_diff(&a, &b, Some(2)); let _j = serde_json::to_string(&x).unwrap(); ts.push(t.elapsed().as_secs_f64() * 1e3); r = Some(x); }
            ts.sort_by(|x, y| x.partial_cmp(y).unwrap()); let r = r.unwrap();
            println!("{n:>5} lines  {kind:<5}  median {:>8.1} ms   (+{} -{})  reconstruct={}", ts[2], r.added, r.removed, if ok { "ok" } else { "FAIL" });
        }
    }
}
