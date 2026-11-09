"""Model client with a shared answer cache.

Answers are cached by the full prompt (model, temperature, session number and
the whole conversation). Configurations of the same bug and session therefore
get the same patches until their conversations first differ, and a cached
answer is charged with the latency and tokens recorded when it was made.

Providers (settings.model.name):
  * any name     -> an OpenAI-compatible API: OPENAI_API_KEY and optionally
                    OPENAI_BASE_URL in .env (OpenAI, OpenRouter, vLLM, Ollama ...)
  * "mock"       -> no API; for smoke tests. Rounds 1-2 return the faulty
                    methods unchanged (a rejected patch, then a repeat), round
                    3 returns the developer's fix.
"""

import hashlib
import json
import os
import time

from . import config as C

SYSTEM = ("You are an automated program repair tool for Java. "
          "You fix the given methods and reply with code only.")


class LLM:
    def __init__(self, settings):
        m = settings["model"]
        self.name, self.temperature, self.max_tokens = m["name"], m["temperature"], m["max_tokens"]
        self.client = None
        if self.name != "mock":
            C.load_dotenv()
            from openai import OpenAI
            key = os.getenv("OPENAI_API_KEY", "").strip() or "none"
            base = os.getenv("OPENAI_BASE_URL", "").strip() or None
            self.client = OpenAI(api_key=key, base_url=base)

    def _key(self, messages, session):
        blob = json.dumps([self.name, self.temperature, self.max_tokens, session, messages],
                          ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()

    def chat(self, messages: list, session: int, info: dict = None):
        """Return (answer text, gen record {wall, tok_in, tok_out, cached})."""
        key = self._key(messages, session)
        path = C.CACHE / "llm" / key[:2] / f"{key}.json"
        hit = C.read_json(path)
        if hit is not None:
            return hit["answer"], {**hit["gen"], "cached": True}
        t0 = time.perf_counter()
        if self.name == "mock":
            answer, tin, tout = self._mock(messages, info)
        else:
            resp = self.client.chat.completions.create(
                model=self.name, messages=[{"role": "system", "content": SYSTEM}] + messages,
                temperature=self.temperature, max_tokens=self.max_tokens)
            answer = resp.choices[0].message.content or ""
            usage = resp.usage
            tin = getattr(usage, "prompt_tokens", 0) or 0
            tout = getattr(usage, "completion_tokens", 0) or 0
        gen = {"wall": time.perf_counter() - t0, "tok_in": tin, "tok_out": tout}
        C.write_json(path, {"answer": answer, "gen": gen})
        return answer, {**gen, "cached": False}

    @staticmethod
    def _mock(messages, info):
        rnd = sum(1 for m in messages if m["role"] == "user")
        methods = info["fixed_methods"] if rnd >= 3 else info["buggy_methods"]
        if rnd == 2:   # same code, different layout: a repeat
            methods = {k: "  // retry\n" + v.replace("    ", "  ") for k, v in methods.items()}
        code = "\n\n".join(methods.values())
        words = sum(len(m["content"].split()) for m in messages)
        return f"```java\n{code}\n```", int(words * 1.3), int(len(code.split()) * 1.3)
