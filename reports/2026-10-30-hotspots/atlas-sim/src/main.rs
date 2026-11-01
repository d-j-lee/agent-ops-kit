//! work-atlas `mine/text.py` 의 Index.load + query 를 같은 text_index.json 으로 수행하는 프로토타입.
//! 사용: atlas-sim <text_index.json> <exclude_wu_id 또는 -> <k> < query.txt
//! 출력: 각 줄 "<wu_id>\t<score>" + stderr 에 단계별 시간(ms)
use std::collections::HashMap;
use std::io::Read;
use std::time::Instant;

use fancy_regex::Regex;
use serde::Deserialize;

#[derive(Deserialize)]
struct TextIndex {
    n: u64,
    idf: HashMap<String, f64>,
    vecs: HashMap<String, HashMap<String, f64>>,
}

const STOP: &str = "a an and are as at be by can could do does for from has have how i if in into is it its may might
not of on or our should so that the their them then there these they this to use used using was we were what when
where which while will with would you your can't don't doesn't also any all some more most other such only same than
too very just get set new add added adds fix fixed fixes issue issues bug bugs patch please thanks code see test tests
java class method methods null true false return returns value values";

struct Tok {
    word: Regex,
    camel: Regex,
    issue: Regex,
    stop: std::collections::HashSet<&'static str>,
}

impl Tok {
    fn new() -> Self {
        Tok {
            word: Regex::new(r"[A-Za-z][A-Za-z0-9_]*|[가-힣]+").unwrap(),
            camel: Regex::new(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+").unwrap(),
            issue: Regex::new(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{1,9}-[0-9]+").unwrap(),
            stop: STOP.split_whitespace().collect(),
        }
    }

    fn tokens(&self, text: &str) -> Vec<String> {
        let cleaned = self.issue.replace_all(text, " ");
        let mut out = Vec::new();
        for m in self.word.find_iter(cleaned.as_ref()) {
            let w = m.unwrap().as_str();
            let first = w.chars().next().unwrap();
            if ('가'..='힣').contains(&first) {
                let cs: Vec<char> = w.chars().collect();
                if cs.len() == 1 {
                    out.push(w.to_string());
                } else {
                    for i in 0..cs.len() - 1 {
                        out.push(cs[i..i + 2].iter().collect());
                    }
                }
                continue;
            }
            let low = w.to_lowercase();
            if low.chars().count() >= 2 && !self.stop.contains(low.as_str()) {
                out.push(low);
            }
            let parts: Vec<String> = w
                .split('_')
                .flat_map(|piece| self.camel.find_iter(piece).map(|p| p.unwrap().as_str().to_lowercase()).collect::<Vec<_>>())
                .collect();
            if parts.len() > 1 {
                for p in parts {
                    if p.chars().count() >= 3 && !self.stop.contains(p.as_str()) && !p.chars().all(|c| c.is_ascii_digit()) {
                        out.push(p);
                    }
                }
            }
        }
        out
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let (path, exclude, k) = (&args[1], &args[2], args[3].parse::<usize>().unwrap());
    let mut query = String::new();
    std::io::stdin().read_to_string(&mut query).unwrap();

    let t0 = Instant::now();
    let bytes = std::fs::read(path).unwrap();
    let idx: TextIndex = serde_json::from_slice(&bytes).unwrap();
    let t_load = t0.elapsed();

    let t1 = Instant::now();
    let mut postings: HashMap<&str, Vec<&str>> = HashMap::new();
    for (d, v) in &idx.vecs {
        for t in v.keys() {
            postings.entry(t.as_str()).or_default().push(d.as_str());
        }
    }
    let t_post = t1.elapsed();

    let t2 = Instant::now();
    let tok = Tok::new();
    // _vec(Counter(tokens(text))) — Counter 는 처음 나온 순서를 유지한다(파이썬 dict 순서)
    let mut counts: Vec<(String, u32)> = Vec::new();
    let mut pos: HashMap<String, usize> = HashMap::new();
    for t in tok.tokens(&query) {
        if let Some(&i) = pos.get(&t) { counts[i].1 += 1; } else { pos.insert(t.clone(), counts.len()); counts.push((t, 1)); }
    }
    let mut q: Vec<(String, f64)> = counts
        .into_iter()
        .filter_map(|(t, c)| idx.idf.get(&t).map(|idf| { let w = (1.0 + (c as f64).ln()) * idf; (t, w) }))
        .collect();
    let norm = q.iter().map(|(_, x)| x * x).sum::<f64>().sqrt();
    let norm = if norm == 0.0 { 1.0 } else { norm };
    for (_, x) in q.iter_mut() { *x /= norm; }
    let mut scores: HashMap<&str, f64> = HashMap::new();
    let mut order: Vec<&str> = Vec::new();
    for (t, w) in &q {
        if let Some(ds) = postings.get(t.as_str()) {
            for d in ds {
                let e = scores.entry(d).or_insert_with(|| { order.push(d); 0.0 });
                *e += w * idx.vecs[*d][t.as_str()];
            }
        }
    }
    scores.remove(exclude.as_str());
    let mut res: Vec<(&str, f64)> = scores.into_iter().filter(|(_, s)| *s > 0.0).map(|(d, s)| (d, py_round4(s))).collect();
    res.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap().then(a.0.cmp(b.0)));
    res.truncate(k);
    let t_query = t2.elapsed();

    for (d, s) in &res { println!("{d}\t{s:.4}"); }
    eprintln!("n={} load_ms={:.1} postings_ms={:.1} query_ms={:.1}", idx.n,
        t_load.as_secs_f64() * 1e3, t_post.as_secs_f64() * 1e3, t_query.as_secs_f64() * 1e3);
}

/// Python 의 `round(x, 4)` 와 같은 결과: 정확한 10진 전개를 4자리로 반올림(정확한 동점은 짝수 쪽)한 뒤 다시 f64 로 읽는다.
/// `(x * 1e4).round() / 1e4` 는 곱셈 오차와 동점 처리(0에서 먼 쪽) 때문에 경계에서 Python 과 갈린다.
fn py_round4(x: f64) -> f64 {
    format!("{x:.4}").parse().expect("formatted float parses")
}
