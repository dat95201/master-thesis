"""tierbot: a tiered repair bot that tries a cheap model first and steps up
to a strong model only when the tests show that the session has stalled.

Modules of the bot:  substrate (M0, Warm Substrate), memory (M1, Evidence
Memory), controller (M2, Escalation Controller and the rule Step), budget
(M3, Budget Guard), bot (the single validate() call).
Experiments:          experiments (E0-E4), analysis.
"""
