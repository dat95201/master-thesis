"""Module M2 (Test Prioritiser): test orderings.

An ordering returns, for each new candidate, the order of the test classes
and optionally `stop_after` (Coverage may declare a patch plausible after
the classes that reach the change). After the run it observes the classes
that actually ran, as (class, failed, cost); classes after the first failure
are never observed (censoring).

  default    Defects4J's own order (reference)
  frtp       classes that rejected more patches in the session first [Qi 2013]
  coverage   PIT rule: classes that reach the faulty methods, the class named
             after the modified class first, then fastest first
  arc        Adaptive Rate-over-Cost (proposed), Algorithm 1 of the proposal
  oracle     the cheapest failing class, known in hindsight (lower bound)

RQ4 variants of ARC are built from the same class with one part changed.
"""

import random


def _simple(fqcn: str) -> str:
    return fqcn.rsplit(".", 1)[-1].split("$")[0]


def _package(fqcn: str) -> str:
    return fqcn.rsplit(".", 1)[0] if "." in fqcn else ""


class Ordering:
    name = "base"
    needs_audit = False

    def __init__(self, info, unit="cpu", rng=None):
        self.info = info
        self.suite = list(info["suite"])
        self.index = {c: i for i, c in enumerate(self.suite)}
        self.unit = unit
        self.rng = rng or random.Random(0)
        self.ci = {c: max(info["ci"].get(c, {}).get(unit, 0.0), 1e-4) for c in self.suite}

    def named_after_modified(self, c) -> bool:
        return _simple(c).startswith(_simple(self.info["modified_class"]))

    def order(self, in_faulty=True):
        return list(self.suite), None

    def observe(self, run):
        pass


class Default(Ordering):
    name = "default"


class FRTP(Ordering):
    """Fault-recorded test prioritisation: most rejections first."""
    name = "frtp"

    def __init__(self, info, unit="cpu", rng=None, cost_tiebreak=False):
        super().__init__(info, unit, rng)
        self.cost_tiebreak = cost_tiebreak
        self.rejections = {c: 0 for c in self.suite}

    def order(self, in_faulty=True):
        tie = (lambda c: self.ci[c]) if self.cost_tiebreak else (lambda c: self.index[c])
        return sorted(self.suite, key=lambda c: (-self.rejections[c], tie(c))), None

    def observe(self, run):
        for c, failed, _ in run:
            if failed:
                self.rejections[c] += 1


class Coverage(Ordering):
    """PIT rule. Needs one instrumented run of the faulty program."""
    name = "coverage"

    def __init__(self, info, unit="cpu", rng=None):
        super().__init__(info, unit, rng)
        cov = info.get("covering")
        self.covering = [c for c in self.suite if c in set(cov)] if cov else list(self.suite)

    def order(self, in_faulty=True):
        head = sorted(self.covering, key=lambda c: (not self.named_after_modified(c), self.ci[c]))
        rest = sorted((c for c in self.suite if c not in set(head)), key=lambda c: self.ci[c])
        # classes that never reach the faulty methods cannot be affected by a
        # change inside them, so they are skipped for such changes
        return head + rest, (len(head) if in_faulty else None)


class ARC(Ordering):
    """Adaptive Rate-over-Cost.

    K_t = (pi_t + k_t) / (1 + n_t)        chance that class t fails
    m_t = s_t / n_t, or the CI time         running time of t
    order: decreasing K_t / m_t
    """
    name = "arc"

    def __init__(self, info, unit="cpu", rng=None, weights=(20, 3, 1), prior="location",
                 use_prob=True, use_cost=True, start="ci", thompson=False, covering_only=False):
        super().__init__(info, unit, rng)
        self.use_prob, self.use_cost = use_prob, use_cost
        self.start, self.thompson = start, thompson
        self.n = {c: 0 for c in self.suite}
        self.k = {c: 0 for c in self.suite}
        self.s = {c: 0.0 for c in self.suite}
        self.pi = self._prior(weights, prior)
        cov = info.get("covering")
        self.covering = ([c for c in self.suite if c in set(cov)] if cov else list(self.suite)) \
            if covering_only else None

    def _prior(self, weights, prior):
        w_name, w_pkg, w_other = weights
        target = self.info["modified_class"]
        if prior == "flat":
            w = {c: 1.0 for c in self.suite}
        else:
            if prior == "no-name":           # drop the hint from the class name
                w_name = w_pkg
            if prior == "wrong":             # pretend the patch is somewhere else
                others = [c for c in self.suite
                          if _package(c) != _package(target) and not self.named_after_modified(c)]
                others = others or [c for c in self.suite if not self.named_after_modified(c)]
                pick = self.rng.choice(others or self.suite)
                target = f"{_package(pick)}.{_simple(pick).replace('Test', '')}"
            simple, pkg = _simple(target), _package(target)
            w = {c: (w_name if _simple(c).startswith(simple)
                     else w_pkg if _package(c) == pkg else w_other) for c in self.suite}
        total = sum(w.values())
        return {c: w[c] / total for c in self.suite}

    def K(self, c):
        a = self.pi[c] + self.k[c]
        if self.thompson:
            return self.rng.betavariate(a, 1 - self.pi[c] + self.n[c] - self.k[c])
        return a / (1 + self.n[c])

    def m(self, c):
        if self.n[c] > 0:
            return max(self.s[c] / self.n[c], 1e-4)
        if self.start == "size":
            seen = [x for x in self.suite if self.n[x] > 0]
            size_seen = sum(self.info["sizes"].get(x, 0) for x in seen)
            rate = sum(self.s[x] for x in seen) / size_seen if size_seen else 1e-6
            return max(self.info["sizes"].get(c, 0) * rate, 1e-4)
        return self.ci[c]

    def score(self, c):
        return (self.K(c) if self.use_prob else 1.0) / (self.m(c) if self.use_cost else 1.0)

    def order(self, in_faulty=True):
        score = {c: self.score(c) for c in self.suite}
        key = lambda c: (-score[c], self.index[c])  # noqa: E731
        if self.covering is None:
            return sorted(self.suite, key=key), None
        head = sorted(self.covering, key=key)
        rest = sorted((c for c in self.suite if c not in set(head)), key=key)
        return head + rest, (len(head) if in_faulty else None)

    def observe(self, run):
        for c, failed, cost in run:
            self.n[c] += 1
            self.k[c] += int(failed)
            self.s[c] += cost


class Oracle(Ordering):
    """Cheapest failing class in hindsight; ordered from the audit itself."""
    name = "oracle"
    needs_audit = True


class TriggerFirst(Ordering):
    """Prefix: run the triggering classes first, then the inner ordering."""

    def __init__(self, inner: Ordering):
        self.inner = inner
        self.name = inner.name + "+trigger"
        self.needs_audit = inner.needs_audit
        self.triggering = set(inner.info["triggering_classes"])

    def order(self, in_faulty=True):
        order, stop = self.inner.order(in_faulty)
        trig = [c for c in order if c in self.triggering]
        rest = [c for c in order if c not in self.triggering]
        if stop is not None:
            selected = set(order[:stop]) - self.triggering
            stop = len(trig) + len(selected)
        return trig + rest, stop

    def observe(self, run):
        self.inner.observe(run)


# ---------------------------------------------------------------- registry

MAIN = ["default", "frtp", "coverage", "arc", "oracle"]
VARIANTS = ["arc-nocost", "arc-costonly", "arc-flat", "arc-noname", "arc-wrong",
            "arc-thompson", "arc-size", "frtp-cost", "arc-cov"]


def make(name: str, info: dict, settings: dict, rng=None, trigger=False) -> Ordering:
    unit = settings["runner"]["cost_unit"]
    a = settings["arc"]
    w = (a["w_name"], a["w_package"], a["w_other"])
    rng = rng or random.Random(settings["seed"])
    table = {
        "default": lambda: Default(info, unit, rng),
        "frtp": lambda: FRTP(info, unit, rng),
        "frtp-cost": lambda: FRTP(info, unit, rng, cost_tiebreak=True),
        "coverage": lambda: Coverage(info, unit, rng),
        "oracle": lambda: Oracle(info, unit, rng),
        "arc": lambda: ARC(info, unit, rng, w),
        "arc-nocost": lambda: ARC(info, unit, rng, w, use_cost=False),
        "arc-costonly": lambda: ARC(info, unit, rng, w, use_prob=False),
        "arc-flat": lambda: ARC(info, unit, rng, w, prior="flat"),
        "arc-noname": lambda: ARC(info, unit, rng, w, prior="no-name"),
        "arc-wrong": lambda: ARC(info, unit, rng, w, prior="wrong"),
        "arc-thompson": lambda: ARC(info, unit, rng, w, thompson=True),
        "arc-size": lambda: ARC(info, unit, rng, w, start="size"),
        "arc-cov": lambda: ARC(info, unit, rng, w, covering_only=True),
    }
    if name not in table:
        raise ValueError(f"unknown ordering {name}; choose from {sorted(table)}")
    o = table[name]()
    return TriggerFirst(o) if trigger else o
