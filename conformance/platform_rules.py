"""Rules that belong to the PLATFORM, written down here so they are executable.

Nothing in this file can be made to pass or fail by editing ``node.py``. It exists
because a node is judged against these rules, and a rule nobody has written down is a
rule that drifts. Each function is a faithful re-statement of one the agent applies —
``agent/runner.py`` for the environment allowlist, ``agent/logbuf.py`` for progress
absorption, ``external/contract.py`` for exit classification.

They are re-stated rather than imported for the same reason as ``conformance/contract.py``:
a harness that shares code with the system it checks cannot detect that system drifting.
"""

from __future__ import annotations

import fnmatch
import json

from conformance.contract import PROGRESS_PREFIX


def refused_env(requested: list[str], allowlist: list[str]) -> list[str]:
    """Names the agent will NOT inject, given the operator's allowlist.

    The manifest NAMES variables; the values come from the machine the agent runs on.
    So the manifest is a request, and a name outside the allowlist fails the job
    visibly rather than being skipped — a step that runs without a credential it was
    written to need fails somewhere deep inside, and nobody learns the agent refused it.
    Entries may be exact names or shell-style patterns.
    """
    return [
        name for name in requested if not any(fnmatch.fnmatchcase(name, pattern) for pattern in allowlist)
    ]


def absorb_progress(line: str) -> dict | None:
    """Read one log line as a progress sample, or return ``None`` if it is not one.

    The prefix is ``'@lspo:progress '`` — the trailing space is part of it, so
    ``@lspo:progress{...}`` is an ordinary log line.

    A MALFORMED progress line is not consumed: it goes to the log as ordinary output,
    where its author can see what they wrote. Swallowing it would make a typo look
    identical to a step that simply reports no progress. ``fraction`` must be a number
    between 0 and 1 inclusive; ``phase`` is optional and truncated to 64 characters.
    """
    if not line.startswith(PROGRESS_PREFIX):
        return None
    try:
        payload = json.loads(line[len(PROGRESS_PREFIX):])
        fraction = float(payload['fraction'])
    except (ValueError, TypeError, KeyError):
        return None
    if not 0.0 <= fraction <= 1.0:
        return None
    return {'fraction': fraction, 'phase': str(payload.get('phase') or '')[:64]}


def split_log(lines: list[str]) -> tuple[list[str], list[dict]]:
    """Split a container's output the way the agent's log buffer does.

    Returns ``(ordinary lines, progress samples)``. A line that parses as progress is
    CONSUMED — it is instrumentation, not output, and it is reported on the heartbeat
    instead of in the log.
    """
    ordinary: list[str] = []
    samples: list[dict] = []
    for line in lines:
        sample = absorb_progress(line)
        if sample is None:
            ordinary.append(line)
        else:
            samples.append(sample)
    return ordinary, samples
