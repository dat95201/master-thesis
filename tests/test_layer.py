"""Unit tests that need neither Defects4J nor a model:  python -m unittest -v"""

import unittest

from vlayer import audit as A
from vlayer import javasrc as J
from vlayer import orderings as O
from vlayer.budget_guard import BudgetGuard, replay, signature

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
        self.assertIn('this(); System.setProperty("vlayer.hit", "1");', out)
        self.assertIn('throws Exception { System.setProperty', out)


def make_info(n=6):
    suite = [f"p.T{i}Test" for i in range(n)]
    return {
        "suite": suite, "modified_class": "p.T3",
        "ci": {c: {"cpu": 1.0 + i, "wall": 1.0 + i} for i, c in enumerate(suite)},
        "covering": suite[2:5], "triggering_classes": ["p.T5Test"],
        "sizes": {c: 100 for c in suite},
    }


def make_audit(fails, n=6):
    return {"key": "k", "compiled": True, "compile": {"wall": 2.0, "cpu": 2.0},
            "jvm": {"wall": 0.5, "cpu": 0.5},
            "classes": {f"p.T{i}Test": {"status": "fail" if i in fails else "pass",
                                        "wall": 1.0 + i, "cpu": 1.0 + i, "test": "t", "exc": "E",
                                        "line": 1, "msg": "m"} for i in range(n)}}


SETTINGS = {"seed": 1, "runner": {"cost_unit": "cpu"},
            "arc": {"w_name": 20, "w_package": 3, "w_other": 1}}


class OrderingTest(unittest.TestCase):
    def test_charge_stops_at_first_failure(self):
        a = make_audit({1, 4})
        ch = A.charge(a, a["classes"].keys())
        self.assertEqual(ch["verdict"], "rejected")
        self.assertEqual(ch["fail"], "p.T1Test")
        self.assertAlmostEqual(ch["cost"]["cpu"], 2.0 + 0.5 + 1.0 + 2.0)
        orc = A.charge(a, A.oracle_order(a))
        self.assertAlmostEqual(orc["tests"]["cpu"], 2.0)

    def test_arc_prefers_named_class_then_learns(self):
        o = O.make("arc", make_info(), SETTINGS)
        order, _ = o.order()
        self.assertEqual(order[0], "p.T3Test")          # named after the modified class
        o.observe([("p.T3Test", False, 4.0)] * 5 + [("p.T0Test", True, 1.0)] * 3)
        self.assertEqual(o.order()[0][0], "p.T0Test")   # fails often and is fast

    def test_arc_matches_proposal_example(self):
        o = O.make("arc", make_info(), SETTINGS)
        o.n["p.T1Test"], o.k["p.T1Test"], o.s["p.T1Test"] = 10, 8, 0.4
        self.assertAlmostEqual(o.K("p.T1Test"), (o.pi["p.T1Test"] + 8) / 11)

    def test_frtp_and_trigger_prefix(self):
        info = make_info()
        f = O.make("frtp", info, SETTINGS, trigger=True)
        f.observe([("p.T2Test", True, 1.0)])
        order, _ = f.order()
        self.assertEqual(order[:2], ["p.T5Test", "p.T2Test"])

    def test_coverage_skips_only_for_changes_in_faulty_methods(self):
        c = O.make("coverage", make_info(), SETTINGS)
        order, stop = c.order(in_faulty=True)
        self.assertEqual(order[0], "p.T3Test")
        self.assertEqual(stop, 3)
        self.assertIsNone(c.order(in_faulty=False)[1])

    def test_all_variants_build(self):
        for name in O.MAIN + O.VARIANTS:
            o = O.make(name, make_info(), SETTINGS)
            if not o.needs_audit:
                self.assertEqual(sorted(o.order()[0]), sorted(make_info()["suite"]))


class BudgetGuardTest(unittest.TestCase):
    cost = {"seconds": 10.0, "tokens": 100, "usd": 0.01}

    def test_stops_after_r_rounds_without_progress(self):
        g = BudgetGuard(r=2, budget=None)
        self.assertTrue(g.step(self.cost, ("A",), False, False)[0])   # progress
        self.assertTrue(g.step(self.cost, ("A",), False, False)[0])   # same failure: 1
        go_on, why = g.step(self.cost, ("B",), True, False)             # repeat: 2
        self.assertFalse(go_on)
        self.assertIn("no progress", why)

    def test_budget_and_log_only(self):
        g = BudgetGuard(r=0, budget=25, log_only=True)
        self.assertTrue(g.step(self.cost, ("A",), False, False)[0])
        self.assertTrue(g.step(self.cost, ("B",), False, False)[0])   # 20 + 10 > 25: would stop
        self.assertEqual(g.would_stop[0], 2)

    def test_replay(self):
        rounds = [{"cost": self.cost, "signature": ["A"], "repeat": False, "verdict": "rejected"},
                  {"cost": self.cost, "signature": ["A"], "repeat": False, "verdict": "rejected"},
                  {"cost": self.cost, "signature": ["A"], "repeat": False, "verdict": "plausible"}]
        self.assertEqual(replay(rounds, 1, None, "seconds")[0], 2)    # plausible lost
        self.assertEqual(replay(rounds, 3, None, "seconds"), (3, None))

    def test_signature_drops_line_numbers(self):
        s = signature("compile_error", None, "Foo.java:12: error: cannot find symbol")
        self.assertEqual(s, ("compile", "error: cannot find symbol"))


if __name__ == "__main__":
    unittest.main()
