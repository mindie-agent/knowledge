"""Shared organizer lifetime; admission must outlive the owned process tree.

An actual 44 KiB Codex GPT-6-Luna/max increment needed 140 seconds after
twice exceeding the former 120 second cap. Leave room for that supported
profile without adding attempts or changing input/output/tool limits.
"""

ORGANIZER_TIMEOUT = 300
ORGANIZER_PROCESS_TIMEOUT = ORGANIZER_TIMEOUT + 5
ORGANIZER_LEASE_SECONDS = ORGANIZER_PROCESS_TIMEOUT + 10
