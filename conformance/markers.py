"""Two labels on every test: WHAT its subject is, and WHAT AUTHORITY it rests on.

A green test run means nothing unless you know what each test was holding to account.
Two independent questions have to be answered for that, and this harness once answered
only the first — which is how it came to assert preferences as though they were rules.

**The group says whose behaviour is on trial** (``node.py``, the platform, or this
harness) and what state it is in today. Four groups, disjoint, one per test.

**The basis says by what authority the assertion is made.** Three bases, disjoint, one
per test:

``basis_contract``
    The rule exists in the platform's own sources and can be QUOTED. The citation is
    not decoration: it is passed as an argument, ``tests/conftest.py`` refuses to run a
    test whose citation does not name one of :data:`AUTHORITATIVE_SOURCES`, and it is
    what makes "is this a real requirement?" answerable without re-deriving it.

``basis_reference_quality``
    What a REFERENCE implementation ought to demonstrate, where the contract permits
    otherwise. A node that does the opposite is still conformant. These exist because
    this repository is the file a customer copies, so "the contract allows it" is not
    the only bar — but they must never be read as conformance requirements, and the
    documentation built on this harness must not teach them as such.

``basis_our_policy``
    A compatibility or hygiene choice THIS repository makes, which the platform does
    not state. Keeping the legacy credentials variable working is the clearest one: no
    contract mentions it, and we honour it because images in the field bake it.

The distinction is the whole point of the round that produced it. A harness that
demands more than the contract forces the next author to write wrong code, and the
documentation written from that code teaches it to everybody after them.
"""

import pytest

EXPECTED_RED_REASON = (
    'expected to fail against node.py as it stands today; read the test\'s basis marker for '
    'whether this is a contract violation, a reference-quality gap, or our own policy'
)

# --------------------------------------------------------------------------- groups

#: The subject is node.py, and it is broken today. Strict xfail: an unexpected PASS
#: fails the run, because a defect that quietly went away is a fact we need told.
expected_red_until_fixed = pytest.mark.expected_red_until_fixed

#: The subject is the platform (runner / agent / collector), not the node.
subject_is_platform = pytest.mark.subject_is_platform

#: The subject is this harness.
harness_self_test = pytest.mark.harness_self_test

#: The subject is node.py, and it passes today. A regression guard.
conforms_today = pytest.mark.conforms_today

GROUPS = ('expected_red_until_fixed', 'subject_is_platform', 'harness_self_test', 'conforms_today')

# ---------------------------------------------------------------------------- basis

BASES = ('basis_contract', 'basis_reference_quality', 'basis_our_policy')

#: Files a ``basis_contract`` citation may name. All of them live in the orchestrator
#: repository at the deployed commit; each is a place where a rule is WRITTEN DOWN
#: rather than inferred. A citation naming anything else is refused at collection time,
#: which is what stops "the contract obviously means…" from creeping back in.
AUTHORITATIVE_SOURCES = (
    'external/contract.py',
    'external/io.py',
    'external/versioning.py',
    'external/hashing.py',
    'runners/credentials.py',
    'agent/runner.py',
    'agent/logbuf.py',
    'agent/creds.py',
    'agent/redact.py',
    'agent/executors/docker_exec.py',
    'pipelines/external_finalize.py',
)


def traces_to(citation: str):
    """The assertion restates a rule that exists in the platform's sources.

    Args:
        citation: The rule, quoted, with the file it lives in. Must name one of
            :data:`AUTHORITATIVE_SOURCES` — the collection hook enforces it.
    """
    return pytest.mark.basis_contract(citation)


def reference_quality(rationale: str):
    """What a reference implementation should demonstrate; the contract permits otherwise.

    Args:
        rationale: Why it is worth demonstrating, in the node author's terms. A node
            that does the opposite is still conformant, and the rationale is what stops
            a reader mistaking this for a rule.
    """
    return pytest.mark.basis_reference_quality(rationale)


def our_policy(rationale: str):
    """A compatibility or hygiene choice this repository makes, which nothing states.

    Args:
        rationale: What the choice buys and who would notice if it were dropped.
    """
    return pytest.mark.basis_our_policy(rationale)
