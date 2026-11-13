"""Model clients, one per tier, with a shared answer cache.

Answers are cached under the full prompt and a draw index (bug, session,
tier, call). The index omits the policy, so two policies that send the same
prompt at the same point of a session receive the same answer, and a cached
answer is charged with the latency and tokens recorded when it was made.
This is what lets every escalation policy be replayed on the candidates of
the recorded single-tier sessions: with `cache_only`, a policy that asks for
a draw nobody recorded raises CacheMiss instead of calling the model.

Providers (settings.tiers[*].model):
  * any name       an OpenAI-compatible API: OPENAI_API_KEY and optionally
                   OPENAI_BASE_URL in .env (OpenAI, OpenRouter, vLLM, Ollama ...)
  * mock-cheap,    no API; scripted answers for the smoke test and the unit
    mock-strong    tests (see _mock)
"""

import hashlib
import json
import os
import time

from . import config as C
from . import javasrc
from .prompts import SYSTEM


class CacheMiss(Exception):
    """A replay needed a model answer that was never recorded."""


class LLM:
    def __init__(self, tier: dict, cache_only=False):
        self.tier, self.model = tier, tier["model"]
        self.params = dict(tier.get("params", {}))
        self.cache_only = cache_only
        self.client = None

    def _key(self, messages, n, index):
        blob = json.dumps([self.model, self.params, n, messages, list(index)],
                          ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    def draw(self, messages: list, n: int, index, info: dict = None):
        """Ask for n candidates. Returns (answers, gen) with gen = {wall,
        tok_in, tok_out, requests, tok_reason, tok_in_cached, tok_in_write,
        cached}. tok_out includes the reasoning tokens (tok_reason), as the
        bill does; the last two are what the provider's prompt cache reported
        for this very request, which depends on what was sent before it."""
        key = self._key(messages, n, index)
        path = C.CACHE / "llm" / key[:2] / f"{key}.json"
        hit = C.read_json(path)
        if hit is not None:
            return hit["answers"], {**hit["gen"], "cached": True}
        if self.cache_only:
            raise CacheMiss(f"no recorded answer of {self.model} for draw {list(index)}")
        t0 = time.perf_counter()
        if self.model.startswith("mock"):
            answers, gen = _mock(self.model, messages, n, index, info)
        else:
            answers, usage = self._call(messages, n)
            gen = {"wall": time.perf_counter() - t0, **usage}
        C.write_json(path, {"model": self.model, "index": list(index), "answers": answers, "gen": gen})
        return answers, {**gen, "cached": False}

    def _call(self, messages, n):
        if self.client is None:
            C.load_dotenv()
            from openai import OpenAI
            self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", "").strip() or "none",
                                 base_url=os.getenv("OPENAI_BASE_URL", "").strip() or None)
        msgs = [{"role": "system", "content": SYSTEM}] + messages
        # one request with n choices pays for the prompt once; an endpoint that
        # cannot do that gets n requests
        batches = [n] if self.tier.get("n_per_request", True) else [1] * n
        answers = []
        use = {"tok_in": 0, "tok_out": 0, "requests": len(batches),
               "tok_reason": 0, "tok_in_cached": 0, "tok_in_write": 0}
        for k in batches:
            extra = {"n": k} if k > 1 else {}
            try:
                resp = self.client.chat.completions.create(model=self.model, messages=msgs,
                                                           **extra, **self.params)
            except Exception as e:  # noqa: BLE001
                if getattr(e, "status_code", None) == 400:
                    raise RuntimeError(
                        f"{self.model} rejected the request ({e}). Check this tier's `params` and "
                        f"`n_per_request` in settings.json: a reasoning model may refuse "
                        f"`temperature` or several choices per request.") from e
                raise
            answers += [c.message.content or "" for c in resp.choices]
            u = resp.usage
            use["tok_in"] += getattr(u, "prompt_tokens", 0) or 0
            use["tok_out"] += getattr(u, "completion_tokens", 0) or 0
            use["tok_reason"] += _detail(u, "completion_tokens_details", "reasoning_tokens")
            use["tok_in_cached"] += _detail(u, "prompt_tokens_details", "cached_tokens")
            use["tok_in_write"] += _detail(u, "prompt_tokens_details", "cache_write_tokens")
        return answers, use


def _detail(usage, group: str, name: str) -> int:
    """A count from the usage details of a response; 0 when the endpoint does not report it."""
    d = getattr(usage, group, None)
    v = d.get(name) if isinstance(d, dict) else getattr(d, name, None)
    return v or 0


def tiers(settings: dict, cache_only=False, models: dict = None) -> dict:
    """{tier name: LLM}; `models` overrides model names, e.g. {'cheap': 'mock-cheap'}."""
    out = {}
    for t in settings["tiers"]:
        t = dict(t)
        if models and models.get(t["name"]):
            t["model"] = models[t["name"]]
        out[t["name"]] = LLM(t, cache_only)
    return out


# ---------------------------------------------------------------- mock models

def _variant(text: str, kind: str, i: int = 0) -> str:
    """A scripted change to a faulty method: a statement with no effect (a
    new patch that fails as before) or one that does not compile."""
    m = javasrc.find_methods(text)[0]
    pos = m.body_open + 1
    if m.is_ctor:           # stay behind an explicit this(...) / super(...) call
        toks = javasrc.tokens(text)
        j = next(k for k, t in enumerate(toks) if t.start == m.body_open)
        if j + 2 < len(toks) and toks[j + 1].value in ("this", "super") and toks[j + 2].value == "(":
            depth = 0
            for t in toks[j + 2:]:
                depth += (t.value == "(") - (t.value == ")")
                if depth == 0 and t.value == ";":
                    pos = t.end
                    break
    stmt = f" {{ int mock = {i}; }}" if kind == "noop" else " int mock = undefinedSymbol;"
    return text[:pos] + stmt + text[pos:]


def _mock(model, messages, n, index, info):
    """Scripted answers. The session number picks the story:
      0  the cheap tier stalls after two calls; the strong tier repeats one
         cheap candidate, then returns the developer's fix in round 3
      1  the cheap tier finds the fix in its second call
      2  the cheap tier stalls at once; the strong tier never finds a fix
    """
    session = index[1]
    buggy, fixed = info["buggy_methods"], info["fixed_methods"]
    first = next(iter(buggy))

    def cand(kind, i=0):
        if kind == "fix":
            methods = fixed
        elif kind == "same":
            methods = buggy
        elif kind == "layout":      # the faulty code again, laid out differently: a repeat
            methods = {k: "  // retry\n" + v.replace("    ", "  ") for k, v in buggy.items()}
        else:
            methods = {**buggy, first: _variant(buggy[first], kind, i)}
        return "```java\n" + "\n\n".join(methods.values()) + "\n```"

    N = lambda i: ("noop", i)  # noqa: E731
    if model == "mock-cheap":
        call = index[3]
        story = {
            0: [[("same",), ("layout",), N(1), ("error",), N(2)], [N(3), N(4), ("same",), N(5), N(6)]],
            1: [[("same",), ("layout",), N(1), ("error",), N(2)], [N(3), ("fix",), N(4), N(5), N(6)]],
            2: [[("same",), ("layout",), N(1), N(2), N(3)]],
        }[session % 3]
        plan = story[call] if call < len(story) else [N(100 * (call + 1) + j) for j in range(n)]
        plan = (plan * n)[:n]
        wall = 2.0
    else:
        rnd = sum(1 for m in messages if m["role"] == "user")
        story = {0: [N(7), ("same",), ("fix",)], 1: [N(7), ("fix",)], 2: [N(7), N(8), N(8)]}[session % 3]
        plan = [story[rnd - 1] if rnd <= len(story) else N(8 + rnd // 4)]
        wall = 6.0
    answers = [cand(*p) for p in plan]
    words = sum(len(m["content"].split()) for m in messages)
    return answers, {"wall": wall, "tok_in": int(words * 1.3),
                     "tok_out": int(sum(len(a.split()) for a in answers) * 1.3), "mock": True}
