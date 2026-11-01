"""work-atlas 의 실제 Index 코드로 같은 질의를 한다(비교 기준). 사용: py_sim.py <index.json> <exclude|-> <k> <json|orjson> < query"""
import os, sys, time
t_start = time.perf_counter()
WA = os.environ.get("WORK_ATLAS", "")  # work-atlas 체크아웃 경로(필수)
if not WA or not os.path.isdir(os.path.join(WA, "atlas")):
    sys.exit(f"WORK_ATLAS={WA!r} 에 atlas 패키지가 없다 — 설치된 다른 work-atlas 를 조용히 쓰지 않도록 멈춘다")
sys.path.insert(0, WA)
from atlas.mine import text as textmod
path, exclude, k, lib = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
q = sys.stdin.read()
t0 = time.perf_counter()
if lib == "orjson":
    import orjson; d = orjson.loads(open(path, "rb").read())
else:
    import json; d = json.load(open(path, encoding="utf-8"))
t1 = time.perf_counter()
idx = textmod.Index.load(d["idf"], d["vecs"], d["n"])
t2 = time.perf_counter()
res = idx.query(q, k=k, exclude={exclude})
t3 = time.perf_counter()
for wu, s in res: print(f"{wu}\t{s:.4f}")
print(f"n={d['n']} load_ms={(t1-t0)*1e3:.1f} postings_ms={(t2-t1)*1e3:.1f} query_ms={(t3-t2)*1e3:.1f} import_ms={(t0-t_start)*1e3:.1f}", file=sys.stderr)
