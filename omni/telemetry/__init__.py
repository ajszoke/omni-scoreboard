"""Our own telemetry, mined from what a game left behind before it has to be deleted.

A provider's recording and the runner's log hold its words, which its terms don't let us keep. What we measured
about them is ours: `nflmeta_mine` turns an NFLMeta recording, and `runner_log_mine` the NFL runner's log, into a
digest of counts, our own kinds and spans (`digest`), checked so no provider string gets in. `runner_log_redact`
keeps the runner's log itself line for line in our own words, with NFLMeta's ids numbered and its words replaced,
and checks a log for anything that isn't ours.
"""
