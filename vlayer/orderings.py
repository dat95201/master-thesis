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
  arc        Adaptive Rate-over-Cost (proposed), Algorithm 1 of the proposal:
             Thompson sampling from a Beta posterior with a location prior
  oracle     the cheapest failing class, known in hindsight (lower bound)

RQ4 variants of ARC are built from the same class with one part changed.

`order(in_faulty, key)` takes the candidate's key; ARC seeds its posterior
draws with it, so the order of a candidate in a session is reproducible.
"""

import random
from collections import Counter

GROUPS = ("name", "package", "other")
DEFAULT_SHARES = {"name": 0.60, "package": 0.25, "other": 0.15}


def _simple(fqcn: str) -> str:
    return fqcn.rsplit(".", 1)[-1].split("$")[0]


def _package(fqcn: str) -> str:
    return fqcn.rsplit(".", 1)[0] if "." in fqcn else ""


def location_group(c: str, target: str) -> str:
    """'name' if the class is named after the target class (its simple name
    starts with the target's), 'package' if it is in the target's package,
    'other' otherwise."""
    if _simple(c).startswith(_simple(target)):
        return "name"
    if _package(c) == _package(target):
        return "package"
    return "other"


def location_prior(suite, target: str, shares: dict, merge_name=False) -> dict:
    """pi_t: each non-empty group gets its share, split equally among its
    classes; shares of empty groups are redistributed, so the priors sum to
    one. merge_name treats classes named after the target as package classes
    (the class-name hint removed, the package kept)."""
    shares = {g: float(shares.get(g, 0.0)) for g in GROUPS}
    if merge_name:
        shares = {"name": 0.0, "package": shares["name"] + shares["package"], "other": shares["other"]}
    group = {}
    for c in suite:
        g = location_group(c, target)
        group[c] = "package" if merge_name and g == "name" else g
    size = Counter(group.values())
    total = sum(shares[g] for g in size)
    if total <= 0:
        return {c: 1.0 / len(suite) for c in suite}
    return {c: shares[g] / total / size[g] for c, g in group.items()}


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

    def order(self, in_faulty=True, key=None):
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

    def order(self, in_faulty=True, key=None):
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

    def order(self, in_faulty=True, key=None):
        head = sorted(self.covering, key=lambda c: (not self.named_after_modified(c), self.ci[c]))
        rest = sorted((c for c in self.suite if c not in set(head)), key=lambda c: self.ci[c])
        # classes that never reach the faulty methods cannot be affected by a
        # change inside them, so they are skipped for such changes
        return head + rest, (len(head) if in_faulty else None)


class ARC(Ordering):
    """Adaptive Rate-over-Cost.

    K_t = (pi_t + k_t) / (1 + n_t)        posterior mean of the failure rate,
                                           Beta(pi_t + k_t, 1 - pi_t + n_t - k_t)
    theta_t ~ that Beta posterior          drawn anew for each candidate
    m_t = s_t / n_t, or the CI time         running time of t
    order: decreasing theta_t / m_t       (Thompson sampling; K_t / m_t with
                                           thompson=False, variant arc-mean)
    """
    name = "arc"

    def __init__(self, info, unit="cpu", rng=None, shares=None, prior="location",
                 use_prob=True, use_cost=True, start="ci", thompson=True, covering_only=False):
        super().__init__(info, unit, rng)
        self.use_prob, self.use_cost = use_prob, use_cost
        self.start, self.thompson = start, thompson
        self.n = {c: 0 for c in self.suite}
        self.k = {c: 0 for c in self.suite}
        self.s = {c: 0.0 for c in self.suite}
        self.pi = self._prior(shares or DEFAULT_SHARES, prior)
        self._seed = self.rng.getrandbits(64)     # base of the per-candidate draws
        cov = info.get("covering")
        self.covering = ([c for c in self.suite if c in set(cov)] if cov else list(self.suite)) \
            if covering_only else None

    def _prior(self, shares, prior):
        if prior == "flat":
            return {c: 1.0 / len(self.suite) for c in self.suite}
        target = self.info["modified_class"]
        if prior == "wrong":                 # pretend the patch is somewhere else
            others = [c for c in self.suite
                      if _package(c) != _package(target) and not self.named_after_modified(c)]
            others = others or [c for c in self.suite if not self.named_after_modified(c)]
            pick = self.rng.choice(others or self.suite)
            target = f"{_package(pick)}.{_simple(pick).replace('Test', '')}"
        return location_prior(self.suite, target, shares, merge_name=(prior == "no-name"))

    def _beta(self, c):
        a = self.pi[c] + self.k[c]
        b = 1 - self.pi[c] + self.n[c] - self.k[c]
        return max(a, 1e-9), max(b, 1e-9)

    def K(self, c):
        """Posterior mean of the failure rate of class c."""
        return (self.pi[c] + self.k[c]) / (1 + self.n[c])

    def m(self, c):
        if self.n[c] > 0:
            return max(self.s[c] / self.n[c], 1e-4)
        if self.start == "size":
            seen = [x for x in self.suite if self.n[x] > 0]
            size_seen = sum(self.info["sizes"].get(x, 0) for x in seen)
            rate = sum(self.s[x] for x in seen) / size_seen if size_seen else 1e-6
            return max(self.info["sizes"].get(c, 0) * rate, 1e-4)
        return self.ci[c]

    def scores(self, key=None) -> dict:
        """theta_t / m_t for every class (K_t / m_t without sampling)."""
        rng = random.Random(f"{self._seed}-{key}") if key is not None else self.rng
        out = {}
        for c in self.suite:
            if not self.use_prob:
                p = 1.0
            elif self.thompson:
                p = rng.betavariate(*self._beta(c))
            else:
                p = self.K(c)
            out[c] = p / (self.m(c) if self.use_cost else 1.0)
        return out

    def order(self, in_faulty=True, key=None):
        score = self.scores(key)
        rank = lambda c: (-score[c], self.index[c])  # noqa: E731
        if self.covering is None:
            return sorted(self.suite, key=rank), None
        head = sorted(self.covering, key=rank)
        rest = sorted((c for c in self.suite if c not in set(head)), key=rank)
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

    def order(self, in_faulty=True, key=None):
        order, stop = self.inner.order(in_faulty, key)
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
            "arc-mean", "arc-size", "frtp-cost", "arc-cov"]


def make(name: str, info: dict, settings: dict, rng=None, trigger=False) -> Ordering:
    unit = settings["runner"]["cost_unit"]
    w = dict(settings.get("arc", {}).get("shares", DEFAULT_SHARES))
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
        "arc-mean": lambda: ARC(info, unit, rng, w, thompson=False),
        "arc-size": lambda: ARC(info, unit, rng, w, start="size"),
        "arc-cov": lambda: ARC(info, unit, rng, w, covering_only=True),
    }
    if name not in table:
        raise ValueError(f"unknown ordering {name}; choose from {sorted(table)}")
    o = table[name]()
    return TriggerFirst(o) if trigger else o
