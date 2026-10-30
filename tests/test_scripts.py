"""Tests for AOK v1 schemas, budget defaults and scripts (stdlib unittest only).

Run from the repo root:  python3 -m unittest discover -s tests -v

Fake secrets are assembled at runtime from fragments so that this file itself
stays clean under scripts/secret_scan.py.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # keep scripts/ free of __pycache__ when loading modules

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "schemas"
SCRIPTS = ROOT / "scripts"
SCHEMA_NAMES = ["aok1", "claim", "tree", "ledger", "cleanroom-report", "card", "seed"]
GRADES = ["확인", "도출", "추론", "미확인", "기록됨", "문서"]


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_script(name: str, *args: str):
    """Run a script in isolated mode; return (exit_code, stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPTS / f"{name}.py"), *map(str, args)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    return proc.returncode, proc.stdout, proc.stderr


def load_schema(name: str) -> dict:
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Minimal JSON Schema subset validator (only the keywords our schemas use).
# --------------------------------------------------------------------------
KNOWN_KEYWORDS = {
    "$schema", "$id", "$ref", "$defs", "title", "description", "type", "properties",
    "required", "additionalProperties", "items", "enum", "const", "anyOf", "allOf",
    "if", "then", "minimum", "maximum", "exclusiveMinimum", "minLength", "minItems",
    "pattern", "format", "default", "examples",
}
SUBSCHEMA_KEYS = ("items", "if", "then")
SUBSCHEMA_LIST_KEYS = ("anyOf", "allOf")
SUBSCHEMA_MAP_KEYS = ("properties", "$defs")


def iter_schema_nodes(node, path="#"):
    """Yield (path, node) for every subschema."""
    if not isinstance(node, dict):
        return
    yield path, node
    for k in SUBSCHEMA_KEYS:
        if isinstance(node.get(k), dict):
            yield from iter_schema_nodes(node[k], f"{path}/{k}")
    for k in SUBSCHEMA_LIST_KEYS:
        for i, sub in enumerate(node.get(k, [])):
            yield from iter_schema_nodes(sub, f"{path}/{k}/{i}")
    for k in SUBSCHEMA_MAP_KEYS:
        for name, sub in (node.get(k) or {}).items():
            yield from iter_schema_nodes(sub, f"{path}/{k}/{name}")


class MiniValidator:
    def __init__(self, root: dict):
        self.root = root

    def resolve(self, ref: str) -> dict:
        if not ref.startswith("#"):
            raise AssertionError(f"non-local $ref not allowed (schemas are self-contained): {ref}")
        node = self.root
        for part in [p for p in ref[1:].split("/") if p]:
            node = node[part.replace("~1", "/").replace("~0", "~")]
        return node

    @staticmethod
    def is_type(inst, t: str) -> bool:
        if t == "object":
            return isinstance(inst, dict)
        if t == "array":
            return isinstance(inst, list)
        if t == "string":
            return isinstance(inst, str)
        if t == "boolean":
            return isinstance(inst, bool)
        if t == "null":
            return inst is None
        if t == "integer":
            return isinstance(inst, int) and not isinstance(inst, bool)
        if t == "number":
            return isinstance(inst, (int, float)) and not isinstance(inst, bool)
        raise AssertionError(f"unknown type {t}")

    def errors(self, inst, schema, path="$") -> list[str]:
        errs: list[str] = []
        if "$ref" in schema:
            errs += self.errors(inst, self.resolve(schema["$ref"]), path)
        if "type" in schema:
            types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            if not any(self.is_type(inst, t) for t in types):
                return errs + [f"{path}: expected type {schema['type']}"]
        if "const" in schema and inst != schema["const"]:
            errs.append(f"{path}: expected const {schema['const']!r}")
        if "enum" in schema and inst not in schema["enum"]:
            errs.append(f"{path}: {inst!r} not in enum")
        if isinstance(inst, (int, float)) and not isinstance(inst, bool):
            if "minimum" in schema and inst < schema["minimum"]:
                errs.append(f"{path}: below minimum")
            if "maximum" in schema and inst > schema["maximum"]:
                errs.append(f"{path}: above maximum")
            if "exclusiveMinimum" in schema and inst <= schema["exclusiveMinimum"]:
                errs.append(f"{path}: not above exclusiveMinimum")
        if isinstance(inst, str):
            if len(inst) < schema.get("minLength", 0):
                errs.append(f"{path}: too short")
            if "pattern" in schema and not re.search(schema["pattern"], inst):
                errs.append(f"{path}: does not match {schema['pattern']}")
        if isinstance(inst, list):
            if len(inst) < schema.get("minItems", 0):
                errs.append(f"{path}: too few items")
            if isinstance(schema.get("items"), dict):
                for i, item in enumerate(inst):
                    errs += self.errors(item, schema["items"], f"{path}[{i}]")
        if isinstance(inst, dict):
            props = schema.get("properties", {})
            for key in schema.get("required", []):
                if key not in inst:
                    errs.append(f"{path}: missing required {key!r}")
            for key, val in inst.items():
                if key in props:
                    errs += self.errors(val, props[key], f"{path}.{key}")
                elif schema.get("additionalProperties") is False:
                    errs.append(f"{path}: unexpected property {key!r}")
        for sub in schema.get("allOf", []):
            errs += self.errors(inst, sub, path)
        if "anyOf" in schema and all(self.errors(inst, sub, path) for sub in schema["anyOf"]):
            errs.append(f"{path}: matches none of anyOf")
        if "if" in schema and not self.errors(inst, schema["if"], path) and "then" in schema:
            errs += self.errors(inst, schema["then"], path)
        return errs


def validate(schema_name: str, inst, ref: str | None = None) -> list[str]:
    root = load_schema(schema_name)
    v = MiniValidator(root)
    return v.errors(inst, v.resolve(ref) if ref else root)


def mini_yaml(text: str) -> dict:
    """Parse the block-mapping/scalar YAML subset used by aok/budget.yaml."""
    def scalar(v: str):
        if v in ("null", "~"):
            return None
        if v in ("true", "false"):
            return v == "true"
        if v == "[]":
            return []
        for cast in (int, float):
            try:
                return cast(v)
            except ValueError:
                pass
        return v.strip("\"'")

    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        key, _, val = line.strip().partition(":")
        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if val.strip() == "":
            parent[key] = {}
            stack.append((indent, parent[key]))
        else:
            parent[key] = scalar(val.strip())
    return root


# --------------------------------------------------------------------------
# Example documents (placeholders only; public repo)
# --------------------------------------------------------------------------
def aok1_example() -> dict:
    return {
        "aok": "1", "msg_id": "123e4567-e89b-12d3-a456-426614174000",
        "tree_id": "tree-example", "task_id": "sa0-20261020-003", "kind": "result",
        "from": "session_<id>", "role": "reviewer", "status": "ok",
        "budget": {"cap_usd": 10, "used_usd": 1.1},
        "loop": {"loop_id": "example-org/example-repo#123", "round": 2, "head_sha": "abc123"},
        "body": {"summary": "review r2: high 0 · medium 1",
                 "findings": [{"id": "F1", "sev": "medium", "loc": "src/app.py:10",
                               "claim": "Error path is not covered by a test.", "grade": "추론"}],
                 "next": "dev applies F1"},
        "refs": [{"type": "pr", "ref": "example-org/example-repo#123"}],
        "reply_by": "2026-10-20T16:30:00Z",
    }


def tree_example() -> dict:
    return {
        "tree_id": "tree-example", "hub_session": "session_<id>", "cap_usd": 50, "soft": 0.7,
        "max_depth": 1, "max_concurrent": 3, "on_overrun": "notify",
        "hub_rotate": {"context_tokens": 250000, "hub_cost_usd": 25},
        "children": [{"session_id": "session_<id>", "role": "reviewer", "model": None,
                      "cap_usd": 10, "spent_usd": 0, "read_at": "2026-10-20T16:00:00Z"}],
    }


def cleanroom_example() -> dict:
    return {
        "repo": "example-org/example-repo", "sha": "0123456789abcdef0123456789abcdef01234567",
        "env": {"env_id": "env_<id>", "cc_version": "2.0.0", "session_start_tail": "ok"},
        "steps": [{"n": 1, "doc_ref": "README.md#setup", "cmd": "make test", "exit": 2,
                   "sec": 12.3, "tail": "missing tool", "deviation": None}],
        "first_failure": {"n": 1, "cmd": "make test", "stderr_head": "make: not found"},
        "findings": [{"type": "doc_missing", "step": 1, "evidence": "make is not installed by the docs"}],
        "result": "fail", "cost_usd": 0.31,
    }


def card_example() -> dict:
    return {
        "id": "card-0001", "type": "pitfall", "claim": "A child that ends cleanly sends no report.",
        "grade": "문서", "evidence": [{"session_id": "session_<id>", "event_uuid": "<event_uuid>"}],
        "first_seen": "2026-10-20", "last_confirmed": None, "hits": 0, "status": "candidate",
    }


def seed_example() -> dict:
    return {
        "id": "seed-0001", "idea": "Cheaper watchdog model.", "origin": "plan §3②",
        "value_hypothesis": "A smaller model detects stalls equally well.",
        "cheapest_experiment": "One watchdog run on a test tree.", "est_cost_usd": 0.1,
        "status": "dormant", "revisit_by": "2026-12-01",
    }


LEDGER_EXAMPLES = {
    "trees": {"tree_id": "tree-example", "hub_session": "session_<id>", "cap_usd": 50,
              "spent_usd": 3.2, "status": "active"},
    "sessions": {"session_id": "session_<id>", "tree_id": "tree-example", "role": "dev",
                 "model": None, "cap_usd": 15, "spent_usd": 1.0, "status": "working",
                 "created_at": "2026-10-20T16:00:00Z", "ended_at": None, "verdict": None},
    "alerts": {"alert_id": "a1", "session_id": "session_<id>", "kind": "stale",
               "since": "2026-10-20T16:00:00Z", "evidence": "updated_at unchanged 42m",
               "dedupe_key": "session_<id>:stale", "acked": False},
    "verifications": {"run_id": "brd-1", "level": "L1+L0", "claims": 12, "disagreements": 2,
                      "confirmed_errors": 1, "cost": {"tokens": 500000, "minutes": 10},
                      "unverifiable": 1, "canary_leak": False},
    "triggers": {"trigger_id": "trig_example", "purpose": "watchdog tree-example", "armed": False,
                 "last_audit": None},
    "audit_day": {"day": "2026-10-20", "entries": [
        {"at": "2026-10-20T16:00:00Z", "actor": "hub", "action": "create_session",
         "target": "session_<id>", "result": "ok", "detail": "admission check passed"}]},
    "config_policy": {"daily_cap_usd": None, "tree_default_cap_usd": 50, "child_cap_usd": 15,
                      "hub_rotate_context_tokens": 250000, "hub_rotate_cost_usd": 25,
                      "stale_minutes": 30, "loop_cost_jump_usd": 3, "soft_ratio": 0.7,
                      "refresh_seconds": 60},
}


# --------------------------------------------------------------------------
# Schema tests
# --------------------------------------------------------------------------
class SchemaStructureTests(unittest.TestCase):
    def test_all_schemas_load_and_have_headers(self):
        for name in SCHEMA_NAMES:
            with self.subTest(schema=name):
                s = load_schema(name)
                self.assertEqual(s["$schema"], "https://json-schema.org/draft/2020-12/schema")
                self.assertEqual(
                    s["$id"], f"https://github.com/d-j-lee/agent-ops-kit/schemas/{name}.schema.json")
                self.assertTrue(s.get("title"))
                self.assertTrue(s.get("description"))

    def test_only_known_keywords(self):
        for name in SCHEMA_NAMES:
            for path, node in iter_schema_nodes(load_schema(name)):
                with self.subTest(schema=name, path=path):
                    self.assertLessEqual(set(node) - KNOWN_KEYWORDS, set(), "unknown keyword")

    def test_required_keys_exist_in_properties(self):
        for name in SCHEMA_NAMES:
            root = load_schema(name)
            # Nodes with required but no properties (if/then branches) are
            # checked against the nearest enclosing node with properties.
            def walk(node, props, path):
                if not isinstance(node, dict):
                    return
                own = node.get("properties")
                scope = own if own is not None else props
                for key in node.get("required", []):
                    with self.subTest(schema=name, path=path, key=key):
                        self.assertIsNotNone(scope, "required without properties")
                        self.assertIn(key, scope)
                for k in SUBSCHEMA_KEYS:
                    if isinstance(node.get(k), dict):
                        walk(node[k], scope, f"{path}/{k}")
                for k in SUBSCHEMA_LIST_KEYS:
                    for i, sub in enumerate(node.get(k, [])):
                        walk(sub, scope, f"{path}/{k}/{i}")
                for k in SUBSCHEMA_MAP_KEYS:
                    for pname, sub in (node.get(k) or {}).items():
                        walk(sub, None, f"{path}/{k}/{pname}")
            walk(root, None, "#")

    def test_every_property_has_description(self):
        for name in SCHEMA_NAMES:
            for path, node in iter_schema_nodes(load_schema(name)):
                for pname, sub in (node.get("properties") or {}).items():
                    with self.subTest(schema=name, path=f"{path}/properties/{pname}"):
                        self.assertTrue(sub.get("description"), "missing description")

    def test_local_refs_resolve(self):
        for name in SCHEMA_NAMES:
            root = load_schema(name)
            v = MiniValidator(root)
            for path, node in iter_schema_nodes(root):
                if "$ref" in node:
                    with self.subTest(schema=name, path=path):
                        self.assertIsInstance(v.resolve(node["$ref"]), dict)

    def test_plan_enums(self):
        a = load_schema("aok1")
        self.assertEqual(a["properties"]["kind"]["enum"], ["task", "result", "blocked", "error", "abort"])
        self.assertEqual(a["properties"]["status"]["enum"], ["ok", "partial", "blocked", "failed"])
        self.assertEqual(a["$defs"]["finding"]["properties"]["sev"]["enum"],
                         ["critical", "high", "medium", "low"])
        self.assertEqual(a["properties"]["refs"]["items"]["properties"]["type"]["enum"],
                         ["pr", "branch", "ledger", "file"])
        self.assertEqual(load_schema("claim")["$defs"]["grade"]["enum"], GRADES)
        self.assertEqual(a["$defs"]["grade"]["enum"], GRADES)
        self.assertEqual(load_schema("card")["properties"]["grade"]["enum"], GRADES)
        self.assertEqual(load_schema("claim")["$defs"]["evidence_item"]["properties"]["kind"]["enum"],
                         ["cmd", "event", "file", "url"])
        c = load_schema("cleanroom-report")
        self.assertEqual(c["properties"]["findings"]["items"]["properties"]["type"]["enum"],
                         ["doc_missing", "doc_wrong", "env_limit", "flaky", "test_fail"])
        self.assertEqual(c["properties"]["result"]["enum"], ["pass", "partial", "fail"])
        card = load_schema("card")
        self.assertEqual(card["properties"]["type"]["enum"], ["pitfall", "procedure", "fact", "decision"])
        self.assertEqual(card["properties"]["status"]["enum"], ["candidate", "active", "stale", "retired"])
        self.assertEqual(load_schema("seed")["properties"]["status"]["enum"],
                         ["dormant", "proposed", "trial", "adopted", "dropped"])
        self.assertEqual(load_schema("ledger")["$defs"]["sessions"]["properties"]["status"]["enum"],
                         ["spawned", "working", "idle", "done", "archive-candidate", "archived", "blocked"])

    def test_ledger_defs_present_with_plan_fields(self):
        defs = load_schema("ledger")["$defs"]
        expected = {
            "trees": {"tree_id", "hub_session", "cap_usd", "spent_usd", "status"},
            "sessions": {"session_id", "tree_id", "role", "model", "cap_usd", "spent_usd", "status",
                         "created_at", "ended_at", "verdict"},
            "alerts": {"alert_id", "session_id", "kind", "since", "evidence", "dedupe_key", "acked"},
            "verifications": {"run_id", "level", "claims", "disagreements", "confirmed_errors", "cost"},
            "triggers": {"trigger_id", "purpose", "armed", "last_audit"},
            "audit_day": {"day", "entries"},
            "config_policy": {"daily_cap_usd", "tree_default_cap_usd", "child_cap_usd",
                              "hub_rotate_context_tokens", "hub_rotate_cost_usd", "stale_minutes",
                              "loop_cost_jump_usd", "soft_ratio", "refresh_seconds"},
        }
        for coll, fields in expected.items():
            with self.subTest(collection=coll):
                self.assertLessEqual(fields, set(defs[coll]["properties"]))
        self.assertEqual(set(defs["audit_day"]["properties"]["entries"]["items"]["properties"]),
                         {"at", "actor", "action", "target", "result", "detail"})


class SchemaExampleTests(unittest.TestCase):
    def assertValid(self, schema, inst, ref=None):
        errs = validate(schema, inst, ref)
        self.assertEqual(errs, [], f"{schema} {ref or ''}: {errs}")

    def assertInvalid(self, schema, inst, ref=None):
        self.assertNotEqual(validate(schema, inst, ref), [], f"{schema} accepted an invalid doc")

    def test_aok1_plan_example_valid(self):
        self.assertValid("aok1", aok1_example())

    def test_aok1_rejects_bad_docs(self):
        doc = aok1_example()
        doc["extra"] = 1
        self.assertInvalid("aok1", doc)
        doc = aok1_example()
        doc["kind"] = "ack"
        self.assertInvalid("aok1", doc)
        doc = aok1_example()
        doc["body"]["findings"][0]["sev"] = "info"
        self.assertInvalid("aok1", doc)
        doc = aok1_example()
        del doc["budget"]
        self.assertInvalid("aok1", doc)

    def test_aok1_task_requires_reply_by(self):
        doc = aok1_example()
        doc["kind"] = "task"
        self.assertValid("aok1", doc)
        del doc["reply_by"]
        self.assertInvalid("aok1", doc)
        doc["kind"] = "result"
        self.assertValid("aok1", doc)

    def test_aok1_summary_line_pattern(self):
        ref = "#/$defs/summary_line"
        self.assertValid("aok1", "[AOK/1 result sa0-20261020-003] review r2: high 0 · medium 1", ref)
        self.assertValid("aok1", "[AOK/1 abort t-1] stop now", ref)
        self.assertInvalid("aok1", "[AOK/1 ack t-1] hi", ref)
        self.assertInvalid("aok1", "AOK/1 result t-1 summary", ref)
        self.assertInvalid("aok1", "[AOK/1 result t-1]", ref)

    def test_claim_confirmed_needs_evidence(self):
        claim = {"id": "C7", "claim": "Idle parent woke after 9.3 s.", "grade": "확인",
                 "scope": "cloud, 2026-10-20"}
        self.assertInvalid("claim", claim)
        claim["evidence"] = []
        self.assertInvalid("claim", claim)
        claim["evidence"] = [{"kind": "event", "ref": "session_<id>/<event_uuid>", "excerpt": "..."}]
        self.assertValid("claim", claim)
        self.assertValid("claim", {"id": "C8", "claim": "x", "grade": "추론"})
        self.assertInvalid("claim", {"id": "C9", "claim": "x", "grade": "maybe"})

    def test_tree_example(self):
        self.assertValid("tree", tree_example())
        doc = tree_example()
        doc["on_overrun"] = "slowdown"
        self.assertInvalid("tree", doc)
        doc = tree_example()
        doc["soft"] = 1.5
        self.assertInvalid("tree", doc)

    def test_ledger_examples(self):
        for coll, doc in LEDGER_EXAMPLES.items():
            with self.subTest(collection=coll):
                self.assertValid("ledger", doc, f"#/$defs/{coll}")
                self.assertValid("ledger", doc)  # root anyOf accepts any collection doc
        bad = dict(LEDGER_EXAMPLES["sessions"], status="running")
        self.assertInvalid("ledger", bad, "#/$defs/sessions")
        bad = dict(LEDGER_EXAMPLES["verifications"], level="L3")
        self.assertInvalid("ledger", bad, "#/$defs/verifications")
        self.assertValid("ledger", dict(LEDGER_EXAMPLES["verifications"], cost=0.42),
                         "#/$defs/verifications")
        bad = dict(LEDGER_EXAMPLES["triggers"], trigger_id="t1")
        self.assertInvalid("ledger", bad, "#/$defs/triggers")

    def test_cleanroom_example(self):
        self.assertValid("cleanroom-report", cleanroom_example())
        doc = cleanroom_example()
        doc["first_failure"] = None
        doc["result"] = "pass"
        self.assertValid("cleanroom-report", doc)
        doc["findings"] = [{"type": "typo", "evidence": "x"}]
        self.assertInvalid("cleanroom-report", doc)

    def test_card_and_seed_examples(self):
        self.assertValid("card", card_example())
        doc = card_example()
        doc["evidence"] = []
        self.assertInvalid("card", doc)
        self.assertValid("seed", seed_example())
        self.assertValid("seed", {"seeds": [seed_example()]}, "#/$defs/seeds_file")
        self.assertInvalid("seed", dict(seed_example(), status="maybe"))


class BudgetAndKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.budget = mini_yaml((ROOT / "aok" / "budget.yaml").read_text(encoding="utf-8"))

    def test_budget_defaults_match_plan(self):
        b = self.budget
        self.assertEqual(b["tree"]["cap_usd"], 50)
        self.assertIs(b["tree"]["include_hub_cost"], True)
        self.assertEqual(b["tree"]["soft"], 0.7)
        self.assertEqual(b["tree"]["max_depth"], 1)
        self.assertEqual(b["tree"]["max_concurrent"], 3)
        self.assertEqual(b["tree"]["on_overrun"], "notify")
        self.assertEqual(b["child"]["cap_usd"], 15)
        self.assertEqual(b["hub_rotate"], {"context_tokens": 250000, "hub_cost_usd": 25})
        self.assertEqual(b["watch"]["stale_minutes"], 30)
        self.assertEqual(b["watch"]["loop_cost_jump_usd"], 3)
        self.assertIn("model_tier", b)
        for role, cap in b["child"]["role_caps_usd"].items():
            with self.subTest(role=role):
                self.assertLessEqual(cap, b["child"]["cap_usd"])

    def test_budget_marked_as_inferred_defaults(self):
        text = (ROOT / "aok" / "budget.yaml").read_text(encoding="utf-8")
        self.assertIn("[추론]", text)
        self.assertIn("D3", text)

    def test_schema_defaults_agree_with_budget(self):
        b = self.budget
        tree = load_schema("tree")["properties"]
        self.assertEqual(tree["cap_usd"]["default"], b["tree"]["cap_usd"])
        self.assertEqual(tree["soft"]["default"], b["tree"]["soft"])
        self.assertEqual(tree["max_depth"]["default"], b["tree"]["max_depth"])
        self.assertEqual(tree["max_concurrent"]["default"], b["tree"]["max_concurrent"])
        self.assertEqual(tree["on_overrun"]["default"], b["tree"]["on_overrun"])
        self.assertEqual(tree["hub_rotate"]["properties"]["context_tokens"]["default"],
                         b["hub_rotate"]["context_tokens"])
        self.assertEqual(tree["hub_rotate"]["properties"]["hub_cost_usd"]["default"],
                         b["hub_rotate"]["hub_cost_usd"])
        cp = load_schema("ledger")["$defs"]["config_policy"]["properties"]
        pairs = {
            "tree_default_cap_usd": b["tree"]["cap_usd"], "child_cap_usd": b["child"]["cap_usd"],
            "soft_ratio": b["tree"]["soft"],
            "hub_rotate_context_tokens": b["hub_rotate"]["context_tokens"],
            "hub_rotate_cost_usd": b["hub_rotate"]["hub_cost_usd"],
            "stale_minutes": b["watch"]["stale_minutes"],
            "loop_cost_jump_usd": b["watch"]["loop_cost_jump_usd"],
        }
        for key, val in pairs.items():
            with self.subTest(key=key):
                self.assertEqual(cp[key]["default"], val)

    def test_knowledge_files(self):
        k = ROOT / "knowledge"
        self.assertTrue((k / "cards" / ".gitkeep").exists())
        self.assertEqual(mini_yaml((k / "seeds.yaml").read_text(encoding="utf-8")), {"seeds": []})
        readme = (k / "README.md").read_text(encoding="utf-8")
        self.assertIn("PUBLIC REPO GUARD", readme)
        oq = (k / "open-questions.md").read_text(encoding="utf-8")
        for tid in ("T1", "T2", "T3", "T4", "T5", "T6"):
            self.assertIn(f"| {tid} |", oq)


# --------------------------------------------------------------------------
# sanitize_packet.py
# --------------------------------------------------------------------------
class SanitizePacketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.packet = base / "packet"
        self.withheld = base / "withheld"
        self.packet.mkdir()
        self.withheld.mkdir()
        (self.withheld / "conclusion.md").write_text(
            "# Conclusion\n\n- The root cause is stale cache invalidation.\nYes.\n----------------\n",
            encoding="utf-8")
        (self.packet / "question.md").write_text(
            "# Question\nWhy does the second request return old data?\nYes.\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_sp(self, *args):
        return run_script("sanitize_packet", *args)

    def test_clean_packet(self):
        code, out, _ = self.run_sp("--packet", self.packet, "--withheld", self.withheld)
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip(), "")

    def test_leak_detected_case_and_wrap_insensitive(self):
        (self.packet / "notes.txt").write_text(
            "intro\nWe think THE ROOT CAUSE is   stale cache\ninvalidation, see above.\n",
            encoding="utf-8")
        code, out, _ = self.run_sp("--packet", self.packet, "--withheld", self.withheld)
        self.assertEqual(code, 1)
        self.assertRegex(out, r"notes\.txt:2: the root cause is stale cache invalidation$")

    def test_short_and_punctuation_lines_are_not_terms(self):
        sp = load_module("sanitize_packet")
        terms = sp.collect_terms(self.withheld, None, 12)
        self.assertIn("the root cause is stale cache invalidation", terms)
        self.assertNotIn("yes.", terms)
        self.assertFalse(any(set(t) <= set("-") for t in terms))

    def test_terms_file(self):
        terms = Path(self.tmp.name) / "terms.txt"
        terms.write_text("# conclusion vocabulary\n\nready_for_merge\n", encoding="utf-8")
        (self.packet / "plan.md").write_text("status: READY_FOR_MERGE\n", encoding="utf-8")
        code, out, _ = self.run_sp("--packet", self.packet, "--withheld", self.withheld,
                                   "--terms", terms)
        self.assertEqual(code, 1)
        self.assertIn("plan.md:1: ready_for_merge", out)

    def test_binary_files_skipped(self):
        (self.packet / "blob.bin").write_bytes(b"\x00\x01The root cause is stale cache invalidation.")
        code, _, _ = self.run_sp("--packet", self.packet, "--withheld", self.withheld)
        self.assertEqual(code, 0)

    def test_canary_written_and_checked(self):
        code, out, _ = self.run_sp("--packet", self.packet, "--withheld", self.withheld, "--canary")
        self.assertEqual(code, 0, out)
        token = out.strip().splitlines()[0]
        self.assertRegex(token, r"^CANARY-[0-9a-f]{32}$")
        self.assertTrue((self.withheld / f"{token}.txt").exists())

        clean = Path(self.tmp.name) / "verifier-clean.md"
        clean.write_text("All claims checked.\n", encoding="utf-8")
        code, out, _ = self.run_sp("--withheld", self.withheld, "--check-output", clean)
        self.assertEqual(code, 0, out)

        leaked = Path(self.tmp.name) / "verifier-leaked.md"
        leaked.write_text("line one\nI also saw " + token.lower() + " in a file.\n", encoding="utf-8")
        code, out, err = self.run_sp("--withheld", self.withheld, "--check-output", clean, leaked)
        self.assertEqual(code, 1)
        self.assertIn(f"verifier-leaked.md:2: {token}", out)
        self.assertIn("void", err)

    def test_canary_leak_into_packet(self):
        code, out, _ = self.run_sp("--packet", self.packet, "--withheld", self.withheld, "--canary")
        token = out.strip().splitlines()[0]
        (self.packet / "copied.md").write_text(token + "\n", encoding="utf-8")
        code, out, _ = self.run_sp("--packet", self.packet, "--withheld", self.withheld)
        self.assertEqual(code, 1)
        self.assertIn("copied.md:1: " + token.lower(), out)

    def test_check_output_without_canary_is_usage_error(self):
        f = Path(self.tmp.name) / "out.md"
        f.write_text("x\n", encoding="utf-8")
        code, _, err = self.run_sp("--withheld", self.withheld, "--check-output", f)
        self.assertEqual(code, 2)
        self.assertIn("--canary", err)

    def test_usage_errors(self):
        self.assertEqual(self.run_sp("--packet", self.packet)[0], 2)
        self.assertEqual(self.run_sp("--withheld", self.withheld)[0], 2)
        self.assertEqual(self.run_sp("--packet", Path(self.tmp.name) / "nope",
                                     "--withheld", self.withheld)[0], 2)
        nested = self.withheld / "packet"
        nested.mkdir()
        self.assertEqual(self.run_sp("--packet", nested, "--withheld", self.withheld)[0], 2)
        self.assertEqual(self.run_sp("--bogus")[0], 2)

    def test_help(self):
        code, out, _ = self.run_sp("--help")
        self.assertEqual(code, 0)
        self.assertIn("--check-output", out)


# --------------------------------------------------------------------------
# harvest_events.py
# --------------------------------------------------------------------------
def fake_email() -> str:
    return "alice.kim" + "@" + "corp-mail" + ".io"


def events_payload() -> dict:
    return {"ccr": {
        "first_id": "e1", "last_id": "e5", "has_more": False,
        "data": [
            {"id": "e1", "created_at": "2026-10-20T10:00:00Z",
             "user": {"internal_anthropic_catchall": {
                 "message": {"role": "user", "content": "Please review. Contact " + fake_email()}}}},
            {"id": "e2", "created_at": "2026-10-20T10:00:05Z",
             "user": {"internal_anthropic_catchall": {
                 "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]}}}},
            {"id": "e3", "created_at": "2026-10-20T10:01:00Z",
             "assistant": {"internal_anthropic_catchall": {"message": {"content": "thinking"}}}},
            {"id": "e4", "created_at": "2026-10-20T10:02:00Z",
             "result": {"internal_anthropic_catchall": {
                 "result": "Done. " + "y" * 5000, "total_cost_usd": 0.27,
                 "duration_ms": 12345, "num_turns": 3}}},
            {"id": "e5", "created_at": "2026-10-19T09:00:00Z",
             "user": {"internal_anthropic_catchall": {"message": {"content": "old prompt"}}}},
        ]}}


def sessions_payload() -> dict:
    return {"ccr": {"data": [
        {"id": "session_<child>", "title": "[proj/dev] task for " + fake_email(),
         "status_bucket": "SESSION_STATUS_BUCKET_WORKING", "updated_at": "2026-10-20T11:00:00Z",
         "tags": ["aok:v1", "role:dev"], "parent_session_id": "session_<hub>",
         "external_metadata": {"usage": {"cost_usd": 1.5, "context_used": 42000}}},
        {"id": "session_<other>", "title": "plain", "status_bucket": "SESSION_STATUS_BUCKET_IDLE",
         "updated_at": "2026-10-18T11:00:00Z", "tags": [], "cost_usd": 0.2},
    ], "has_more": False}}


def envelope(payload: dict) -> str:
    return ('<other-session nonce="n0nce" untrusted="true">Session listing follows. '
            'Header may contain {braces} and "quotes".\n    '
            + json.dumps(payload, ensure_ascii=False)
            + '\n</other-session nonce="n0nce">\n')


class HarvestEventsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.out = self.base / "out" / "rows.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name: str, text: str) -> Path:
        p = self.base / name
        p.write_text(text, encoding="utf-8")
        return p

    def rows(self):
        return [json.loads(l) for l in self.out.read_text(encoding="utf-8").splitlines()]

    def test_events_in_untrusted_envelope(self):
        f = self.write("events.txt", envelope(events_payload()))
        code, _, err = run_script("harvest_events", f, "--out", self.out, "--session-id", "session_<x>")
        self.assertEqual(code, 0, err)
        rows = self.rows()
        self.assertEqual([r["kind"] for r in rows], ["user_prompt", "user_prompt", "result"])
        self.assertTrue(all(r["untrusted"] is True for r in rows))
        self.assertTrue(all(r["session_id"] == "session_<x>" for r in rows))
        prompt = rows[1]
        self.assertEqual(prompt["event_uuid"], "e1")
        self.assertIn("<email>", prompt["text"])
        self.assertNotIn("@", prompt["text"])
        res = rows[2]
        self.assertEqual(res["cost_usd"], 0.27)
        self.assertEqual(res["duration_ms"], 12345)
        self.assertEqual(res["num_turns"], 3)
        self.assertEqual(len(res["text"]), 4000)
        self.assertTrue(res["truncated"])

    def test_since_filter(self):
        f = self.write("events.json", json.dumps(events_payload()))
        code, _, _ = run_script("harvest_events", f, "--out", self.out, "--since", "2026-10-20T00:00:00Z")
        self.assertEqual(code, 0)
        texts = [r["text"] for r in self.rows()]
        self.assertNotIn("old prompt", texts)
        self.assertEqual(len(texts), 2)

    def test_sessions_raw_json(self):
        f = self.write("sessions.json", json.dumps(sessions_payload()))
        code, _, err = run_script("harvest_events", f, "--out", self.out)
        self.assertEqual(code, 0, err)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        child = next(r for r in rows if r["id"] == "session_<child>")
        self.assertEqual(child["cost_usd"], 1.5)
        self.assertEqual(child["context_used"], 42000)
        self.assertEqual(child["parent_session_id"], "session_<hub>")
        self.assertEqual(child["tags"], ["aok:v1", "role:dev"])
        self.assertEqual(child["status_bucket"], "SESSION_STATUS_BUCKET_WORKING")
        self.assertIn("<email>", child["title"])
        self.assertTrue(child["untrusted"])
        self.assertEqual(set(child), {"source", "id", "title", "status_bucket", "updated_at", "tags",
                                      "parent_session_id", "cost_usd", "context_used", "untrusted"})

    def test_sessions_in_envelope_and_content_blocks(self):
        f1 = self.write("s1.txt", envelope(sessions_payload()))
        blocks = [{"type": "text", "text": envelope(sessions_payload())}]
        f2 = self.write("s2.json", json.dumps(blocks))
        code, _, err = run_script("harvest_events", f1, f2, "--out", self.out)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.rows()), 2)  # de-duplicated by session id

    def test_extract_json_prefers_ccr(self):
        he = load_module("harvest_events")
        text = 'header {"decoy": 1} more\n{"ccr": {"data": []}}'
        self.assertEqual(he.extract_json(text), {"ccr": {"data": []}})
        self.assertEqual(he.extract_json('noise {"data": [{"id": "x"}]} tail'), {"data": [{"id": "x"}]})
        with self.assertRaises(ValueError):
            he.extract_json("no json here")

    def test_mask(self):
        he = load_module("harvest_events")
        self.assertEqual(he.mask("mail " + fake_email() + " now"), "mail <email> now")
        self.assertEqual(he.mask("no address @ here"), "no address @ here")

    def test_bad_input_exit_1_but_writes_rest(self):
        good = self.write("good.json", json.dumps(events_payload()))
        bad = self.write("bad.txt", "<other-session>nothing useful</other-session>")
        code, _, err = run_script("harvest_events", good, bad, "--out", self.out)
        self.assertEqual(code, 1)
        self.assertIn("bad.txt", err)
        self.assertEqual(len(self.rows()), 3)

    def test_usage_errors(self):
        self.assertEqual(run_script("harvest_events", self.base / "missing.json", "--out", self.out)[0], 2)
        f = self.write("e.json", json.dumps(events_payload()))
        self.assertEqual(run_script("harvest_events", f, "--out", self.out, "--since", "yesterday")[0], 2)
        self.assertEqual(run_script("harvest_events", f)[0], 2)  # --out missing

    def test_help(self):
        code, out, _ = run_script("harvest_events", "--help")
        self.assertEqual(code, 0)
        self.assertIn("--since", out)


# --------------------------------------------------------------------------
# secret_scan.py
# --------------------------------------------------------------------------
ALNUM = "aB3dE5gH7jK9mN1pQ2rS4tU6vW8xY0zC1fG3hJ5kL7"


def fake_secrets() -> dict[str, str]:
    """rule -> one realistic-looking fake value, assembled from fragments."""
    return {
        "github_token": "gh" + "p_" + ALNUM[:36],
        "github_pat": "github" + "_pat_" + "11ABCDEFG0" + ALNUM[:30],
        "anthropic_key": "sk-" + "ant-" + "api03-" + ALNUM[:30],
        "aws_access_key": "AK" + "IA" + "Q3EGRT5YH7JK9LM2",
        "private_key": "-----BEGIN " + "RSA PRIVATE KEY-----",
        "slack_token": "xo" + "xb-" + "1234567890-" + ALNUM[:12],
        "google_api_key": "AI" + "za" + ALNUM[:35],
        "jwt": "ey" + "JhbGciOiJIUzI1NiJ9" + ".ey" + "JzdWIiOiIxMjM0NTY3ODkwIn0" + "." + ALNUM[:20],
    }


class SecretScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_each_error_rule_detected(self):
        ss = load_module("secret_scan")
        for rule, value in fake_secrets().items():
            with self.subTest(rule=rule):
                found = [(r, s) for _, _, r, s, _ in ss.scan_text(f"x = '{value}'\n")]
                self.assertIn((rule, "error"), found)

    def test_cli_reports_without_printing_secret(self):
        values = fake_secrets()
        f = self.base / "config.env"
        f.write_text("\n".join(f"K{i}={v}" for i, v in enumerate(values.values())) + "\n",
                     encoding="utf-8")
        code, out, _ = run_script("secret_scan", self.base)
        self.assertEqual(code, 1)
        self.assertIn(f"{f}:1: github_token (error)", out)
        self.assertIn(f"{f}:5: private_key (error)", out)
        for v in values.values():
            if v.startswith("-----"):
                continue
            self.assertNotIn(v, out)

    def test_placeholders_and_reserved_emails_ignored(self):
        text = "\n".join([
            "token: " + "gh" + "p_" + "x" * 36,
            "aws: " + "AK" + "IA" + "IOSFODNN7EXAMPLE",
            "key: " + "sk-" + "ant-" + "REDACTED" + "0" * 20,
            "mail: user@example.com, dev@service.test",
            "remote: git@github.com:example-org/example-repo.git",
            "asset: logo@2x.png",
            "id: session_<id>, cse_<id>, trig_<id>, trig_example, claude.ai/artifact/<id>",
            "Co-Authored-By: Bot <noreply@anthropic.com>",
        ])
        f = self.base / "doc.md"
        f.write_text(text + "\n", encoding="utf-8")
        code, out, err = run_script("secret_scan", f)
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip(), "")
        self.assertIn("0 error(s), 0 warning(s)", err)

    def test_warn_rules_do_not_fail(self):
        f = self.base / "notes.md"
        f.write_text("owner: " + fake_email() + "\n"
                     "child: session_0" + "1AbCdEfGhIjKlMnOpQrStUv" + "\n"
                     "env: cse_" + "0AbCdEfGhIjKlMnOpQ" + "\n"
                     "task: trig_" + "01AbCdEfGhIjKlMnOp" + "\n"
                     "page: https://claude.ai/" + "artifact/" + "3f2a9c1e-77aa-4b1c" + "\n",
                     encoding="utf-8")
        code, out, _ = run_script("secret_scan", f)
        self.assertEqual(code, 0)
        self.assertIn(":1: email (warn)", out)
        self.assertIn(":2: claude_session_id (warn)", out)
        self.assertIn(":3: claude_cse_id (warn)", out)
        self.assertIn(":4: claude_trigger_id (warn)", out)
        self.assertIn(":5: claude_artifact_url (warn)", out)

    def test_allow_marker(self):
        f = self.base / "a.md"
        f.write_text("x " + fake_secrets()["github_token"] + "  # secret-scan: allow\n", encoding="utf-8")
        self.assertEqual(run_script("secret_scan", f)[0], 0)

    def test_skips_git_and_binary(self):
        token = fake_secrets()["github_token"]
        (self.base / ".git").mkdir()
        (self.base / ".git" / "config").write_text(token + "\n", encoding="utf-8")
        (self.base / "img.bin").write_bytes(b"\x00\x00" + token.encode())
        (self.base / "ok.txt").write_text("nothing here\n", encoding="utf-8")
        code, out, err = run_script("secret_scan", self.base)
        self.assertEqual(code, 0, out)
        self.assertIn("1 file(s)", err)

    def test_json_output(self):
        f = self.base / "k.txt"
        f.write_text(fake_secrets()["aws_access_key"] + "\n" + fake_email() + "\n", encoding="utf-8")
        code, out, _ = run_script("secret_scan", f, "--json")
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertEqual(data["errors"], 1)
        self.assertEqual(data["warnings"], 1)
        self.assertEqual({x["rule"] for x in data["findings"]}, {"aws_access_key", "email"})
        self.assertNotIn(fake_secrets()["aws_access_key"], out)

    def test_usage_error(self):
        self.assertEqual(run_script("secret_scan", self.base / "missing")[0], 2)
        self.assertEqual(run_script("secret_scan")[0], 2)

    def test_help(self):
        code, out, _ = run_script("secret_scan", "--help")
        self.assertEqual(code, 0)
        self.assertIn("--json", out)

    def test_own_files_are_clean(self):
        """Public repo guard: this layer's files contain no secrets and no real ids/emails."""
        dirs = [ROOT / d for d in ("schemas", "scripts", "tests", "aok", "knowledge")]
        code, out, _ = run_script("secret_scan", *dirs)
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip(), "", out)


if __name__ == "__main__":
    unittest.main()


class SecretScanStrictTests(unittest.TestCase):
    """--strict turns public-repo hygiene warnings into errors (review fix)."""

    def test_strict_promotes_warnings(self):
        import subprocess, sys, tempfile, os
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "note.md")
            with open(f, "w", encoding="utf-8") as fh:
                fh.write("hub env_" + "01FAKEtq7WmZx9LbVc3NrKd" + " container_0" + "1FAKEtq7WmZx9LbVc\n")
            script = os.path.join(os.path.dirname(__file__), "..", "scripts", "secret_scan.py")
            loose = subprocess.run([sys.executable, "-I", script, f], capture_output=True, text=True)
            strict = subprocess.run([sys.executable, "-I", script, "--strict", f], capture_output=True, text=True)
            self.assertEqual(loose.returncode, 0, loose.stdout + loose.stderr)
            self.assertIn("claude_environment_id (warn)", loose.stdout)
            self.assertIn("claude_container_id (warn)", loose.stdout)
            self.assertEqual(strict.returncode, 1, strict.stdout + strict.stderr)
