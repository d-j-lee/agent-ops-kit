//! docbench `src/core/diff.ts` 의 lineDiff 를 같은 의미로 옮긴 것: 줄 단위 Myers diff + 바뀐 줄 쌍의 낱말 diff.
//! 낱말 토크나이저는 jsdiff 9 의 WordsWithSpaceDiff 정규식을 그대로 쓴다(한글은 글자 단위 토큰).
use std::sync::OnceLock;

use regex::Regex;
use serde::Serialize;
use similar::DiffOp;
use similar::algorithms::{Capture, Replace, myers};

#[derive(Debug, Serialize, PartialEq)]
pub struct WordPart {
    pub text: String,
    pub changed: bool,
}

#[derive(Debug, Serialize, PartialEq)]
#[serde(tag = "t")]
pub enum DiffLine {
    #[serde(rename = "ctx")]
    Ctx { text: String },
    #[serde(rename = "add")]
    Add { text: String, #[serde(skip_serializing_if = "Option::is_none")] words: Option<Vec<WordPart>> },
    #[serde(rename = "del")]
    Del { text: String, #[serde(skip_serializing_if = "Option::is_none")] words: Option<Vec<WordPart>> },
    #[serde(rename = "skip")]
    Skip { count: usize },
}

#[derive(Debug, Serialize)]
pub struct DiffResult {
    pub lines: Vec<DiffLine>,
    pub added: usize,
    pub removed: usize,
}

const WORD_CHARS: &str = r"a-zA-Z0-9_\x{AD}\x{C0}-\x{D6}\x{D8}-\x{F6}\x{F8}-\x{2C6}\x{2C8}-\x{2D7}\x{2DE}-\x{2FF}\x{1E00}-\x{1EFF}";

fn word_regex() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(&format!(r"(\r?\n)|[{w}]+|[^\S\n\r]+|[^{w}]", w = WORD_CHARS)).expect("valid regex")
    })
}

fn tokenize_words(s: &str) -> Vec<&str> {
    word_regex().find_iter(s).map(|m| m.as_str()).collect()
}

fn word_parts(old: &str, new: &str) -> (Vec<WordPart>, Vec<WordPart>) {
    let (a, b) = (tokenize_words(old), tokenize_words(new));
    let mut del = Vec::new();
    let mut add = Vec::new();
    for op in raw_myers(&a, &b) {
        match op {
            DiffOp::Equal { old_index, new_index, len } => {
                del.push(WordPart { text: a[old_index..old_index + len].concat(), changed: false });
                add.push(WordPart { text: b[new_index..new_index + len].concat(), changed: false });
            }
            DiffOp::Delete { old_index, old_len, .. } => {
                del.push(WordPart { text: a[old_index..old_index + old_len].concat(), changed: true });
            }
            DiffOp::Insert { new_index, new_len, .. } => {
                add.push(WordPart { text: b[new_index..new_index + new_len].concat(), changed: true });
            }
            DiffOp::Replace { old_index, old_len, new_index, new_len } => {
                del.push(WordPart { text: a[old_index..old_index + old_len].concat(), changed: true });
                add.push(WordPart { text: b[new_index..new_index + new_len].concat(), changed: true });
            }
        }
    }
    (del, add)
}

fn strip_nl(s: &str) -> &str {
    s.strip_suffix('\n').unwrap_or(s)
}

/// 휴리스틱 없는(최소 편집) Myers — jsdiff 와 같은 성질. similar 3 의 기본 Myers 는 속도를 위해 최소가 아닐 수 있다.
fn raw_myers<T: PartialEq>(a: &[T], b: &[T]) -> Vec<DiffOp> {
    let mut hook = Replace::new(Capture::new());
    myers::diff_deadline_raw(&mut hook, a, 0..a.len(), b, 0..b.len(), None).expect("capture never fails");
    hook.into_inner().into_ops()
}

/// context = None 이면 같은 구간을 접지 않는다(검증용).
pub fn line_diff(a: &str, b: &str, context: Option<usize>) -> DiffResult {
    let old: Vec<&str> = a.split_inclusive('\n').collect();
    let new: Vec<&str> = b.split_inclusive('\n').collect();
    let ops = raw_myers(&old, &new);
    let mut lines = Vec::new();
    let (mut added, mut removed) = (0, 0);
    let n_ops = ops.len();
    for (i, op) in ops.iter().enumerate() {
        match *op {
            DiffOp::Equal { old_index, len, .. } => {
                let ls: Vec<&str> = old[old_index..old_index + len].iter().map(|l| strip_nl(l)).collect();
                match context {
                    Some(c) => {
                        let head = if i == 0 { 0 } else { c };
                        let tail = if i == n_ops - 1 { 0 } else { c };
                        if ls.len() > head + tail + 1 {
                            lines.extend(ls[..head].iter().map(|l| DiffLine::Ctx { text: (*l).to_string() }));
                            lines.push(DiffLine::Skip { count: ls.len() - head - tail });
                            lines.extend(ls[ls.len() - tail..].iter().map(|l| DiffLine::Ctx { text: (*l).to_string() }));
                        } else {
                            lines.extend(ls.iter().map(|l| DiffLine::Ctx { text: (*l).to_string() }));
                        }
                    }
                    None => lines.extend(ls.iter().map(|l| DiffLine::Ctx { text: (*l).to_string() })),
                }
            }
            DiffOp::Delete { old_index, old_len, .. } => {
                removed += old_len;
                lines.extend(old[old_index..old_index + old_len].iter().map(|l| DiffLine::Del { text: strip_nl(l).to_string(), words: None }));
            }
            DiffOp::Insert { new_index, new_len, .. } => {
                added += new_len;
                lines.extend(new[new_index..new_index + new_len].iter().map(|l| DiffLine::Add { text: strip_nl(l).to_string(), words: None }));
            }
            DiffOp::Replace { old_index, old_len, new_index, new_len } => {
                removed += old_len;
                added += new_len;
                let ls = &old[old_index..old_index + old_len];
                let ns = &new[new_index..new_index + new_len];
                if old_len == new_len {
                    for (l, n) in ls.iter().zip(ns) {
                        let (l, n) = (strip_nl(l), strip_nl(n));
                        let (dw, aw) = word_parts(l, n);
                        lines.push(DiffLine::Del { text: l.to_string(), words: Some(dw) });
                        lines.push(DiffLine::Add { text: n.to_string(), words: Some(aw) });
                    }
                } else {
                    lines.extend(ls.iter().map(|l| DiffLine::Del { text: strip_nl(l).to_string(), words: None }));
                    lines.extend(ns.iter().map(|l| DiffLine::Add { text: strip_nl(l).to_string(), words: None }));
                }
            }
        }
    }
    DiffResult { lines, added, removed }
}

// ── WebAssembly 경계: 입력 두 개를 선형 메모리로 받고, 결과 JSON 의 (포인터, 길이)를 돌려준다 ──
#[cfg(target_arch = "wasm32")]
mod wasm {
    use super::line_diff;

    #[unsafe(no_mangle)]
    pub extern "C" fn alloc(len: usize) -> *mut u8 {
        let mut v = Vec::<u8>::with_capacity(len);
        let p = v.as_mut_ptr();
        std::mem::forget(v);
        p
    }

    /// # Safety
    /// `p` 는 같은 `len` 으로 alloc 이 돌려준 포인터여야 한다.
    #[unsafe(no_mangle)]
    pub unsafe extern "C" fn dealloc(p: *mut u8, len: usize) {
        // SAFETY: alloc 이 같은 len 의 capacity 로 만든 버퍼다.
        drop(unsafe { Vec::from_raw_parts(p, 0, len) });
    }

    static mut OUT: (usize, usize) = (0, 0);

    /// # Safety
    /// 두 버퍼는 유효한 UTF-8 이어야 한다.
    #[unsafe(no_mangle)]
    pub unsafe extern "C" fn diff_json(a: *const u8, a_len: usize, b: *const u8, b_len: usize, context: usize) -> *const u8 {
        // SAFETY: 호출자가 alloc 으로 받은 버퍼에 UTF-8 을 써서 넘긴다.
        let (a, b) = unsafe {
            (std::str::from_utf8_unchecked(std::slice::from_raw_parts(a, a_len)),
             std::str::from_utf8_unchecked(std::slice::from_raw_parts(b, b_len)))
        };
        let out = serde_json::to_vec(&line_diff(a, b, Some(context))).unwrap_or_default().into_boxed_slice();
        let len = out.len();
        let p = Box::into_raw(out) as *const u8;
        // SAFETY: 단일 스레드 wasm. 결과 길이를 out_len 으로 읽어 간다.
        unsafe { OUT = (p as usize, len) };
        p
    }

    #[unsafe(no_mangle)]
    pub extern "C" fn out_len() -> usize {
        // SAFETY: 단일 스레드 wasm.
        unsafe { OUT.1 }
    }
}
