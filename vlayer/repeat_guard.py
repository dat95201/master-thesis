"""Module M1 (Repeat Guard): never validate the same patch twice in a session.

A candidate is normalised (comments, layout and local variable names
removed, see javasrc.normalise). A candidate whose normal form was already
seen is a repeat: it gets the stored result without running any test, and,
when the notice is on, the model is told that the patch was already tried.
"""

from . import javasrc


class RepeatGuard:
    def __init__(self, notice=True):
        self.notice = notice
        self.seen = {}            # key -> (round, result)

    @staticmethod
    def key(methods: dict) -> str:
        return javasrc.candidate_key(methods)

    def lookup(self, key):
        return self.seen.get(key)

    def store(self, key, round_no, result):
        self.seen.setdefault(key, (round_no, result))

    def feedback(self, key, stored_feedback: str) -> str:
        """Text shown to the model for a repeat."""
        first_round, result = self.seen[key]
        if not self.notice:
            return stored_feedback          # as if it had been validated again
        why = result.get("why", "it was rejected")
        return (f"You already proposed this fix in round {first_round} "
                f"(the same code up to formatting, comments and variable names). "
                f"It was rejected: {why}\n"
                f"Please provide a different fix.")
