"""Unit tests that need neither Defects4J, Java nor a model:

    python -m unittest discover -s tests -v
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from tierbot import analysis as AN
from tierbot import audit as A
from tierbot import budget as B
from tierbot import config as C
from tierbot import controller as K
from tierbot import javasrc as J
from tierbot import llm as L
from tierbot import substrate as S
from tierbot.bot import TieredBot
from tierbot.memory import EvidenceMemory, describe, signature
from tierbot.prompts import first_prompt
from tierbot.session import run_session

SRC = """package p;
public class Foo {
    private int x;
    /** doc */
    @SuppressWarnings({"a", "b"})
    public int add(int a, java.util.List<String> b) throws Exception {
        int s = a + x; // comment
        return s;
    }
    public Foo(int y) { this(); x = y; }
    public Foo() { super(); }
    static String hex(int c) { return Integer.toHexString(c); }
}
"""

BUGGY = "static int twice(int a) {\n        int r = a + a + 1;\n        return r;\n    }"
FIXED = "static int twice(int a) {\n        int r = a + a;\n        return r;\n    }"

SETTINGS = {
    "seed": 1, "sessions": 3,
    "runner": {"cost_unit": "cpu", "class_timeout_s": 60, "order_check_fraction": 0.1, "cap_cpu_hours": 4},
    "substrate": {"paths": ["cold", "warm"], "reference": "cold", "confirm_plausible": True,
                  "candidates_per_runtime": 30},
    "tiers": [
        {"name": "cheap", "model": "mock-cheap", "mode": "blind", "n": 5, "budget": 20, "params": {},
         "usd_per_mtok_in": 0.10, "usd_per_mtok_out": 0.50},
        {"name": "strong", "model": "mock-strong", "mode": "conversation", "n": 1, "budget": 10, "params": {},
         "usd_per_mtok_in": 2.0, "usd_per_mtok_out": 10.0}],
    "step": {"stall_new_max": 1},
    "budget_guard": {"r": 3, "budget": 900, "budget_unit": "seconds"},
    "e2": {"policies": ["cheap-only", "strong-only", "fixed-5", "fixed-10", "step", "hindsight"],
           "equal_attempts": {"cheap": 5, "strong": 5}},
    "e3": {"handovers": ["evidence", "evidence+patches", "transcript"]},
    "e4": {"handover": "cold"},
    "prices": {"usd_per_cpu_hour": 0.05},
    "analysis": {"margin_pp": 10},
}


def make_info():
    suite = [f"p.T{i}Test" for i in range(6)]
    return {
        "bug": "Toy-1", "suite": suite, "modified_class": "p.Twice", "test_dir": "src/test",
        "ci": {c: {"cpu": 1.0 + i, "wall": 1.0 + i, "status": "pass"} for i, c in enumerate(suite)},
        "covering": ["p.T4Test", "p.T2Test", "p.T5Test", "p.T3Test"], "triggering_classes": ["p.T5Test"],
        "faulty": [{"key": "twice(int)", "name": "twice", "nparams": 1}],
        "buggy_methods": {"twice(int)": BUGGY}, "fixed_methods": {"twice(int)": FIXED},
        "trigger_tests": [{"test": "p.T5Test::testTwice", "error": "AssertionError: expected:<4> but was:<5>"}],
    }


class JavaSourceTest(unittest.TestCase):
    def test_find_methods(self):
        keys = [m.key for m in J.find_methods(SRC)]
        self.assertEqual(keys, ["add(int,java.util.List<String>)", "Foo(int)", "Foo()", "hex(int)"])
        add = J.find_methods(SRC)[0]
        self.assertTrue(SRC[add.start:].startswith("@SuppressWarnings"))
        self.assertTrue(SRC[:add.end].endswith("return s;\n    }"))

    def test_faulty_methods(self):
        fixed = SRC.replace("a + x", "a - x")
        self.assertEqual(J.faulty_methods(SRC, fixed)[0], ["add(int,java.util.List<String>)"])
        self.assertIsNone(J.faulty_methods(SRC, SRC.replace("private int x;", "int x;"))[0])

    def test_normalise_is_safe(self):
        a = "int f(int n) { int total = n + 1; /* c */ return total; }"
        b = "int f(int m) {\n  int t = m + 1;\n  return t; // done\n}"
        c = "int f(int n) { int total = 1 + n; return total; }"     # equivalent, not merged
        self.assertEqual(J.normalise(a), J.normalise(b))
        self.assertNotEqual(J.normalise(a), J.normalise(c))
        self.assertNotEqual(J.normalise("void f() { this.x = 1; }"), J.normalise("void f() { this.y = 1; }"))

    def test_parse_answer_and_replace(self):
        faulty = [("hex(int)", "hex", 1)]
        ans = "Sure:\n```java\nstatic String hex(int c) {\n  return \"0x\" + c;\n}\n```\n"
        got = J.parse_answer(ans, faulty)
        self.assertIn("0x", got["hex(int)"])
        new = J.replace_methods(SRC, got)
        self.assertIn('"0x" + c', new)
        self.assertIn("int s = a + x", new)
        self.assertIsNone(J.parse_answer("I cannot help", faulty))

    def test_probe_after_this_call(self):
        out = J.insert_probes(SRC, ["Foo(int)", "add(int,java.util.List<String>)"])
        self.assertIn('this(); System.setProperty("tierbot.hit", "1");', out)
        self.assertIn('throws Exception { System.setProperty', out)

    def test_dedent_and_source_encoding(self):
        self.assertEqual(J.dedent("void f() {\n        g();\n    }"), "void f() {\n    g();\n}")
        d = Path(tempfile.mkdtemp())
        try:
            (d / "A.java").write_bytes("class A { String s = \"caf\xe9\"; }".encode("latin-1"))
            text, enc = J.read_source(d / "A.java")
            self.assertEqual(enc, "ISO-8859-1")
            J.write_source(d / "B.java", text, enc)
            self.assertEqual((d / "A.java").read_bytes(), (d / "B.java").read_bytes())
        finally:
            shutil.rmtree(d)


class CheckoutTest(unittest.TestCase):
    """Every candidate is built from the source in work/<bug>/b, so the
    harness refuses a checkout that is no longer the recorded faulty program."""

    def setUp(self):
        self.work, self.old = Path(tempfile.mkdtemp(prefix="tierbot-work-")), C.WORK
        C.WORK = self.work
        self.src = self.work / "T-1" / "b" / "src" / "p" / "Foo.java"
        self.src.parent.mkdir(parents=True)
        self.src.write_text(SRC)
        key = next(k for k in J.method_texts(SRC) if k.startswith("add"))
        self.info = {"bug": "T-1", "pid": "T", "bid": 1, "src_file": "src/p/Foo.java",
                     "buggy_methods": {key: J.method_texts(SRC)[key]}}

    def tearDown(self):
        C.WORK = self.old
        shutil.rmtree(self.work, ignore_errors=True)

    def test_unchanged_checkout_is_accepted(self):
        from tierbot.bugs import verify_checkout
        self.assertEqual(verify_checkout(self.info), "")

    def test_changed_checkout_is_refused(self):
        from tierbot.bugs import verify_checkout
        self.src.write_text(SRC.replace("int s = a + x;", "int s = a - x;"))
        self.assertIn("is not the recorded faulty method", verify_checkout(self.info))
        self.src.write_text(SRC)
        self.assertEqual(verify_checkout(self.info), "")


class ScheduleTest(unittest.TestCase):
    def test_static_order(self):
        # the classes that fail on the faulty program, then the covering classes, fastest first
        self.assertEqual(S.scheduled(make_info()), ["p.T5Test", "p.T2Test", "p.T3Test", "p.T4Test"])

    def test_stored_failing_classes_move_up(self):
        order = S.schedule(make_info(), {"p.T4Test": 2, "p.T3Test": 1, "p.T5Test": 9})
        self.assertEqual(order, ["p.T5Test", "p.T4Test", "p.T3Test", "p.T2Test"])

    def test_without_coverage_every_class_is_scheduled(self):
        info = make_info()
        info["covering"] = None
        self.assertEqual(len(S.scheduled(info)), 6)
        self.assertEqual(S.scheduled(info)[0], "p.T5Test")


def rec(status="pass", t=1.0, **kw):
    return {"status": status, "wall": t, "cpu": t, "test": kw.get("test", "t"), "exc": kw.get("exc", "E"),
            "line": 1, "msg": kw.get("msg", "m")}


def make_audit(fails=(), compiled=True, warm=True):
    classes = ["p.T5Test", "p.T2Test", "p.T3Test", "p.T4Test"]
    res = {c: rec("fail" if c in fails else "pass", 1.0 + i) for i, c in enumerate(classes)}
    a = {"key": "k", "bug": "Toy-1", "classes": classes,
         "cold": {"compiled": compiled, "compile": {"wall": 2.7, "cpu": 2.7}, "compile_error": "" if compiled else
                  "Twice.java:3: error: cannot find symbol", "jvm": {"wall": 0.5, "cpu": 0.5},
                  "classes": res if compiled else {}}}
    if warm:
        a["warm"] = {"compiled": compiled, "compile": {"wall": 0.05, "cpu": 0.05}, "compile_error": "",
                     "classes": {c: dict(r, wall=r["wall"] / 10, cpu=r["cpu"] / 10) for c, r in res.items()}
                     if compiled else {}, "restarts": 0, "restart": {"wall": 0.0, "cpu": 0.0}}
    return a


class AuditTest(unittest.TestCase):
    def test_charge_stops_at_first_failure_on_both_paths(self):
        a = make_audit(fails={"p.T2Test", "p.T4Test"})
        cold = A.charge(a, "cold", a["classes"])
        self.assertEqual(cold["fail"], "p.T2Test")
        self.assertAlmostEqual(cold["cpu"], 2.7 + 0.5 + 1.0 + 2.0)
        warm = A.charge(a, "warm", a["classes"])
        self.assertAlmostEqual(warm["cpu"], 0.05 + 0.1 + 0.2)       # no start-up on the warm path
        self.assertAlmostEqual(warm["start"]["cpu"], 0.0)
        # a stored failing class scheduled first rejects with one class
        self.assertAlmostEqual(A.charge(a, "warm", ["p.T5Test", "p.T4Test", "p.T2Test"])["test"]["cpu"], 0.1 + 0.4)
        self.assertAlmostEqual(A.best_order_cost(a, "cold"), 2.0)

    def test_verdict_reported_failure_and_agreement(self):
        a = make_audit(fails={"p.T3Test", "p.T4Test"})
        self.assertEqual(A.record_verdict(a, SETTINGS), "rejected")
        # reported failure: the first failing class in the static order, whatever the session's order
        self.assertEqual(A.reported_failure(a, make_info(), SETTINGS)["class"], "p.T3Test")
        self.assertTrue(A.agree(a))
        a["warm"]["classes"]["p.T3Test"]["status"] = "pass"
        self.assertFalse(A.agree(a))
        self.assertEqual(A.record_verdict(make_audit(), SETTINGS), "plausible")
        self.assertEqual(A.record_verdict(make_audit(compiled=False), SETTINGS), "compile_error")
        self.assertAlmostEqual(A.charge(make_audit(compiled=False), "cold", [])["cpu"], 2.7)

    def test_whole_suite_check_overrides_a_plausible_verdict(self):
        a = make_audit()
        a["cold"]["whole_suite"] = {"passed": False, "failing": ["p.T0Test"]}
        self.assertEqual(A.record_verdict(a, SETTINGS), "rejected")
        self.assertEqual(A.reported_failure(a, make_info(), SETTINGS)["class"], "p.T0Test")

    def test_untrusted_warm_path_is_charged_at_the_cold_price(self):
        a = make_audit(fails={"p.T5Test"}, warm=False)
        a["warm"] = {"fallback": "outcome differs from the failing CI run"}
        self.assertAlmostEqual(A.charge(a, "warm", a["classes"])["cpu"], 2.7 + 0.5 + 1.0)
        self.assertIs(A.reference(a, SETTINGS), a["cold"])

    def test_a_hung_candidate_pays_for_a_new_runtime(self):
        a = make_audit(fails={"p.T5Test"})
        a["warm"]["classes"]["p.T5Test"]["status"] = "timeout"
        a["warm"].update({"restarts": 1, "restart": {"wall": 0.4, "cpu": 0.3}})
        self.assertAlmostEqual(A.charge(a, "warm", a["classes"])["start"]["cpu"], 0.3)


class MemoryTest(unittest.TestCase):
    fail = {"class": "p.T5Test", "test": "testTwice", "exc": "java.lang.AssertionError", "msg": "expected:<4> but was:<5>"}

    def test_signature_drops_line_numbers_and_identities(self):
        s = signature("compile_error", None, "Foo.java:12: error: cannot find symbol\n  symbol: x")
        self.assertEqual(s, ("compile", "error: cannot find symbol"))
        a = signature("rejected", dict(self.fail, msg="was <Foo@1a2b3c4d>"))
        b = signature("rejected", dict(self.fail, msg="was  <Foo@99ffee00>"))
        self.assertEqual(a, b)
        self.assertEqual(describe(s), ("compile error: cannot find symbol", ""))
        self.assertEqual(describe(a)[0], "T5Test.testTwice")

    def test_progress_repeats_and_counts(self):
        m = EvidenceMemory(make_info())
        _, p1 = m.add("k1", "cheap", "rejected", self.fail, methods={"twice(int)": "aaaa"})
        _, p2 = m.add("k2", "cheap", "rejected", self.fail, methods={"twice(int)": "aa"})
        _, p3 = m.add("k3", "cheap", "compile_error", None, "A.java:1: error: x")
        self.assertEqual((p1, p2, p3), (True, False, True))     # new code that fails as before: no progress
        self.assertEqual(m.repeat("k1", "strong")["verdict"], "rejected")
        self.assertEqual(m.count("cheap"), {"candidates": 3, "distinct": 3, "repeats": 0, "unreadable": 0})
        self.assertEqual(m.count("strong")["repeats"], 1)
        self.assertEqual(m.nth_by("k1", "strong"), 1)           # the strong tier's first proposal
        self.assertIsNone(m.nth_by("k2", "strong"))
        self.assertEqual(m.nth_by("k2", "cheap"), 2)
        rows = m.failures()
        self.assertEqual([(r[1], r[2]["key"]) for r in rows], [(2, "k2"), (1, "k3")])   # shortest example
        self.assertEqual(m.schedule()[0], "p.T5Test")
        self.assertFalse(m.passed_trigger())
        m.add("k4", "strong", "rejected", dict(self.fail, **{"class": "p.T3Test"}))
        self.assertTrue(m.passed_trigger())
        self.assertEqual(m.schedule()[:2], ["p.T5Test", "p.T3Test"])    # the stored failing class moved up


class ControllerTest(unittest.TestCase):
    def run_calls(self, name, calls):
        """calls: per call, the progress flags of its candidates. Returns the decisions."""
        c = K.Controller(K.policy(name, SETTINGS))
        out = []
        for flags in calls:
            for f in flags:
                c.candidate(f)
            out.append(c.call_done()[0])
        return out, c

    def test_step_steps_up_after_a_stalled_call(self):
        out, c = self.run_calls("step", [[1, 0, 1, 0, 0], [0, 1, 0, 0, 0]])
        self.assertEqual(out, ["same", "step_up"])           # two new failures, then only one
        self.assertEqual(c.tier, "strong")
        self.assertEqual(self.run_calls("step-strict", [[1, 0, 0, 0, 0], [0] * 5])[0], ["same", "step_up"])

    def test_step_steps_up_when_the_cheap_budget_is_spent(self):
        out, _ = self.run_calls("step", [[1, 1, 0, 0, 0]] * 4)
        self.assertEqual(out, ["same", "same", "same", "step_up"])

    def test_baseline_policies(self):
        self.assertEqual(self.run_calls("fixed-10", [[1] * 5, [1] * 5])[0], ["same", "step_up"])
        self.assertEqual(self.run_calls("cheap-only", [[0] * 5] * 4)[0], ["same", "same", "same", "stop"])
        out, _ = self.run_calls("strong-only", [[0]] * 10)
        self.assertEqual(out, ["same"] * 9 + ["stop"])
        eq = K.policy("equal-5-5", SETTINGS)
        self.assertEqual((eq.k, eq.strong_rounds), (5, 5))
        self.assertEqual(K.policy("fixed-0", SETTINGS).cheap, "none")
        with self.assertRaises(ValueError):
            K.policy("fixed-7", SETTINGS)                       # not a multiple of the call size


class BudgetGuardTest(unittest.TestCase):
    cost = {"seconds": 10.0, "tokens": 100, "usd": 0.01, "cpu": 1.0}

    def test_stops_when_the_top_tier_stalls(self):
        g = B.BudgetGuard(r=2, budget=None)
        self.assertFalse(g.step("cheap", False, self.cost, False)[0])     # the cheap tier never counts
        self.assertFalse(g.step("strong", True, self.cost, True)[0])
        self.assertFalse(g.step("strong", True, self.cost, False)[0])
        stop, why = g.step("strong", True, self.cost, False)
        self.assertTrue(stop)
        self.assertIn("no progress for 2 rounds", why)

    def test_budget_and_log_only(self):
        g = B.BudgetGuard(r=0, budget=25, log_only=True)
        self.assertFalse(g.step("strong", True, self.cost, True)[0])
        self.assertFalse(g.step("strong", True, self.cost, True)[0])      # 20 + 10 > 25: would stop
        self.assertEqual(g.would_stop[0], 2)

    def test_replay(self):
        step = lambda v, prog: {"tier": "strong", "cost": self.cost,  # noqa: E731
                                "candidates": [{"verdict": v, "progress": prog}]}
        steps = [step("rejected", True), step("rejected", False), step("plausible", False)]
        self.assertEqual(B.replay(steps, 1, None, "seconds")[0], 2)       # cut before the plausible patch
        self.assertEqual(B.replay(steps, 3, None, "seconds"), (3, None))


class PromptCacheTest(unittest.TestCase):
    """Money in the second reading: a tier re-reads, within a session, the
    prefix it already sent; what is new is written."""
    PRICES = {"usd_per_cpu_hour": 0.05, "cache_min_prompt_tokens": 1024}
    TIER = {"usd_per_mtok_in": 2.0, "usd_per_mtok_in_cached": 0.2, "usd_per_mtok_cache_write": 2.5,
            "usd_per_mtok_out": 10.0}

    @staticmethod
    def step(tier, mode, tok_in, requests=1):
        return {"tier": tier, "mode": mode, "gen": {"tok_in": tok_in, "tok_out": 100, "requests": requests}}

    def test_blind_calls_repeat_one_prompt(self):
        a, b = self.step("cheap", "blind", 1500), self.step("cheap", "blind", 1500)
        self.assertEqual(B.cache_split([], a, self.PRICES), {"read": 0, "write": 1500})
        self.assertEqual(B.cache_split([a], b, self.PRICES), {"read": 1500, "write": 0})
        five = self.step("cheap", "blind", 7500, requests=5)          # five requests for five candidates
        self.assertEqual(B.cache_split([], five, self.PRICES), {"read": 6000, "write": 1500})

    def test_a_conversation_reads_its_previous_prompt(self):
        cheap = self.step("cheap", "blind", 1500)
        r1, r2 = self.step("strong", "conversation", 1500), self.step("strong", "conversation", 2100)
        self.assertEqual(B.cache_split([cheap], r1, self.PRICES), {"read": 0, "write": 1500})   # tiers share nothing
        self.assertEqual(B.cache_split([cheap, r1], r2, self.PRICES), {"read": 1500, "write": 600})

    def test_short_prompts_are_not_cached(self):
        a, b = self.step("strong", "conversation", 800), self.step("strong", "conversation", 1300)
        self.assertEqual(B.cache_split([], a, self.PRICES), {"read": 0, "write": 0})
        self.assertEqual(B.cache_split([a], b, self.PRICES), {"read": 0, "write": 1300})
        self.assertEqual(B.cache_split([], self.step("strong", "conversation", 5000), {"usd_per_cpu_hour": 0.05}),
                         {"read": 0, "write": 0})                      # no threshold set: no cache

    def test_two_readings_of_money(self):
        gen = {"wall": 1.0, "tok_in": 2100, "tok_out": 100}
        c = B.call_cost(gen, [], self.TIER, self.PRICES, {"read": 1500, "write": 600})
        self.assertAlmostEqual(c["usd"], (2100 * 2.0 + 100 * 10.0) / 1e6)
        self.assertAlmostEqual(c["usd_cached"], (1500 * 0.2 + 600 * 2.5 + 100 * 10.0) / 1e6)
        plain = B.call_cost(gen, [], {"usd_per_mtok_in": 2.0, "usd_per_mtok_out": 10.0}, self.PRICES,
                            {"read": 1500, "write": 600})
        self.assertAlmostEqual(plain["usd_cached"], plain["usd"])     # a tier without cache prices


class FakeValidator:
    """Judges a candidate by its text; every validation costs the same."""

    def __init__(self, info):
        self.info, self.n = info, 0

    def validate(self, methods, order):
        self.n += 1
        text = methods["twice(int)"]
        cost = {"wall": 0.1, "cpu": 0.1, "build": {"wall": 0.05, "cpu": 0.05},
                "start": {"wall": 0.0, "cpu": 0.0}, "test": {"wall": 0.05, "cpu": 0.05}, "fail": None, "ran": 1}
        cold = dict(cost, wall=3.3, cpu=3.3)
        if "undefinedSymbol" in text:
            return {"verdict": "compile_error", "fail": None, "agree": True,
                    "compile_error": "Twice.java:2: error: cannot find symbol", "cost": {"warm": cost, "cold": cold}}
        if J.normalise(text) == J.normalise(FIXED):
            return {"verdict": "plausible", "fail": None, "compile_error": "", "agree": True,
                    "cost": {"warm": cost, "cold": cold}}
        fail = {"class": "p.T5Test", "test": "testTwice", "exc": "java.lang.AssertionError", "line": 0,
                "msg": "expected:<4> but was:<5>"}
        return {"verdict": "rejected", "fail": fail, "compile_error": "", "agree": True,
                "cost": {"warm": dict(cost, fail="p.T5Test"), "cold": dict(cold, fail="p.T5Test")}}

    def session_start(self):
        return {"wall": 1.0, "cpu": 1.0}

    def close(self):
        pass


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cache, C.CACHE = C.CACHE, self.tmp / "cache"
        self.info = make_info()

    def tearDown(self):
        C.CACHE = self.cache
        shutil.rmtree(self.tmp)

    def run_policy(self, policy, session, handover="cold", guard="off", cache_only=False):
        return run_session(self.info, SETTINGS, policy, session, handover, guard,
                           L.tiers(SETTINGS, cache_only), FakeValidator(self.info))

    def test_step_escalates_after_a_stall_and_the_memory_spans_the_tiers(self):
        s = self.run_policy("step", 0)
        self.assertEqual([x["tier"] for x in s["steps"]], ["cheap", "cheap", "strong", "strong", "strong"])
        self.assertEqual(s["plausible_tier"], "strong")
        first = s["steps"][0]["candidates"]
        self.assertEqual([c["verdict"] for c in first], ["rejected"] * 3 + ["compile_error", "rejected"])
        self.assertTrue(first[1]["repeat"])                               # same code, other layout
        self.assertEqual([c["progress"] for c in first], [True, False, False, True, False])
        anchored = s["steps"][3]["candidates"][0]                         # the strong tier repeats a cheap candidate
        self.assertEqual((anchored["repeat"], anchored["repeat_of"]), (True, "cheap"))
        self.assertEqual(anchored["val"]["warm"]["cpu"], 0.0)             # answered from the memory
        self.assertTrue(s["steps"][2]["candidates"][0]["refuted_again"])
        self.assertTrue(s["steps"][-1]["candidates"][0]["matches_fix"])

    def test_cheap_tier_repairs_without_a_strong_call(self):
        s = self.run_policy("step", 1)
        self.assertEqual(s["plausible_tier"], "cheap")
        self.assertEqual({x["tier"] for x in s["steps"]}, {"cheap"})
        self.assertEqual(s["steps"][-1]["unvalidated"], 3)                # drawn and paid for, not validated

    def test_cold_escalation_replays_the_recorded_sessions_exactly(self):
        """Proposition 2.2 and the replay of E2: with a cold handover the
        strong part of a tiered session is the strong model's own session."""
        for k in range(3):
            alone = self.run_policy("strong-only", k)
            self.run_policy("cheap-only", k)
            for policy in ("step", "fixed-5", "fixed-10", "step-strict"):
                s = self.run_policy(policy, k, cache_only=True)          # no new model call is needed
                strong = [c["key"] for x in s["steps"] if x["tier"] == "strong" for c in x["candidates"]]
                rec = [c["key"] for x in alone["steps"] for c in x["candidates"]]
                self.assertEqual(strong, rec[:len(strong)])
                if alone["plausible"]:
                    self.assertTrue(s["plausible"])                       # no repair is lost
        with self.assertRaises(L.CacheMiss):                              # a note changes the prompt: not a replay
            self.run_policy("step", 0, handover="evidence", cache_only=True)

    def test_handover_notes(self):
        s = self.run_policy("step", 0, handover="evidence")
        note = s["note"]
        self.assertTrue(note.startswith("Handover note (10 candidates from the first model, none plausible)"))
        self.assertIn("7x T5Test.testTwice", note)
        self.assertIn("1x compile error: cannot find symbol", note)
        self.assertIn("2 candidates repeated an earlier one. No candidate passed T5Test.", note)
        self.assertNotIn("int r", note)                                   # facts, none of the first model's code
        self.assertIn("int r", self.run_policy("step", 0, handover="evidence+patches")["note"])
        t = self.run_policy("step", 0, handover="transcript")["note"]
        self.assertIn("Candidate 2: the same as candidate 1", t)
        self.assertIsNone(self.run_policy("step", 0)["note"])             # cold: nothing is handed over

    def test_first_prompt_is_unchanged_without_a_note(self):
        p = first_prompt(self.info)
        self.assertEqual(p, first_prompt(self.info, ""))
        self.assertIn("Test:  T5Test::testTwice", p)
        with_note = first_prompt(self.info, "Handover note (...)")
        self.assertTrue(with_note.endswith("Return the complete fixed method in one Java code block."))
        self.assertIn("Handover note", with_note)

    def test_budget_guard_stops_a_stalled_strong_tier_and_reports(self):
        s = self.run_policy("step", 2, guard="on")
        self.assertFalse(s["plausible"])
        self.assertEqual(sum(x["tier"] == "strong" for x in s["steps"]), 3)
        self.assertIn("no progress for 3 rounds at the strong tier", s["stopped"])
        r = s["report"]
        self.assertIn("Give-up report (bug Toy-1, session 2)", r)
        self.assertIn("cheap 5 candidates (4 distinct, 1 repeat); strong 3 rounds", r)
        self.assertIn("T5Test.testTwice", r)
        logged = self.run_policy("step", 2, guard="log")
        self.assertEqual(logged["would_stop"][0], 4)                      # one cheap call, three strong rounds
        self.assertEqual(sum(x["tier"] == "strong" for x in logged["steps"]), 10)
        self.assertEqual(len(AN.cut(logged, SETTINGS)["steps"]), 4)       # the same stop, applied by replay

    def test_own_repeat_gets_a_notice_and_the_other_tiers_repeat_does_not(self):
        info, v = self.info, FakeValidator(self.info)
        bot = TieredBot(info, SETTINGS, v, K.policy("fixed-5", SETTINGS))
        bot.begin_call({"wall": 1.0, "tok_in": 10, "tok_out": 10})
        for i in range(5):
            res = bot.validate({"twice(int)": BUGGY}, last=(i == 4))
        self.assertEqual((res.verdict, res.next), ("repeat", "step_up"))
        bot.begin_call({"wall": 1.0, "tok_in": 10, "tok_out": 10})
        res = bot.validate({"twice(int)": BUGGY})                          # first seen from the cheap tier
        self.assertEqual(res.verdict, "repeat")
        self.assertTrue(res.feedback.startswith("Your fix is still incorrect"))
        bot.begin_call({"wall": 1.0, "tok_in": 10, "tok_out": 10})
        res = bot.validate({"twice(int)": BUGGY})                          # now the strong tier repeats itself
        self.assertTrue(res.feedback.startswith("You already proposed this fix in round 1"))
        self.assertEqual(v.n, 1)                                           # one validation in all

    def test_costs_and_analysis(self):
        runs = {p: [self.run_policy(p, k) for k in range(3)]
                for p in ("cheap-only", "strong-only", "fixed-5", "fixed-10", "fixed-15", "fixed-20", "step")}
        s = runs["step"][0]
        usd = AN.cost(s, SETTINGS, "usd")
        model = sum(x["cost"]["model_usd"] for x in s["steps"])
        cpu = AN.cost(s, SETTINGS, "cpu")
        self.assertAlmostEqual(usd, model + cpu * 0.05 / 3600)
        self.assertAlmostEqual(cpu, 1.0 + 0.1 * 10)                        # session start + 10 validations
        self.assertGreater(AN.cost(s, SETTINGS, "cpu", plain=True), cpu)   # a plain loop pays the build each time
        self.assertAlmostEqual(sum(AN.parts(s, SETTINGS, "usd").values()), usd)
        hind = AN.hindsight(runs, SETTINGS)
        for h in hind:      # never worse than any switch point that repairs
            same = [x for p in runs if p != "cheap-only" and p != "step" for x in runs[p]
                    if x["session"] == h["session"] and x["plausible"] == h["plausible"]]
            self.assertLessEqual(AN.cost(h, SETTINGS, "usd"), min(AN.cost(x, SETTINGS, "usd") for x in same) + 1e-12)
        margin, ratio = AN.break_even(runs["step"], runs["strong-only"], SETTINGS)
        self.assertLess(ratio, 1.0)
        self.assertEqual(AN.repaired_bugs(runs["step"]), 1)


class StatisticsTest(unittest.TestCase):
    def test_holm_ratio_and_non_inferiority(self):
        adj = AN.holm({"a": 0.01, "b": 0.04, "c": float("nan")})
        self.assertAlmostEqual(adj["a"], 0.02)
        self.assertAlmostEqual(adj["b"], 0.04)
        t = AN.ratio_test({f"b{i}": 1.0 for i in range(8)}, {f"b{i}": 2.0 for i in range(8)})
        self.assertAlmostEqual(t["ratio"], 0.5)
        self.assertEqual(t["wins"], 1.0)
        mean, lo, ok = AN.non_inferior([0.0] * 30, 10)
        self.assertTrue(ok)
        self.assertFalse(AN.non_inferior([-0.5] * 30, 10)[2])


if __name__ == "__main__":
    unittest.main()
