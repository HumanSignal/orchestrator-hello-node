"""The three groups every conformance test must declare membership of.

A green test run means nothing unless you know WHAT each test was holding to account.
These three markers are disjoint, and ``tests/test_markers_are_declared.py`` fails the
suite if any test carries none of them or more than one of them.

``expected_red_until_fixed``
    The subject is ``node.py``, and today it fails. Each one is a written-down defect:
    it must go green when that defect is fixed, and it must NOT be possible to make it
    green any other way. These run as expected failures (``xfail(strict=True)``), so
    CI is green today and turns red the moment a fix lands while the marker is still
    on the test — which is the signal we want, because it is the reminder to promote
    the test out of the expected-failure group.

``subject_is_platform``
    The subject is the runner/agent/collector, not the node. No edit to ``node.py``
    can make one of these pass or fail; they exist so that the rules the node is
    judged against are themselves written down and executable, and so that a future
    change to the platform's side of the contract breaks something visible here.

``harness_self_test``
    The subject is this harness. Proves the fake endpoint really refuses what it
    claims to refuse, that credential rotation really rotates, and that upload order
    is really recorded — because every ``expected_red_until_fixed`` verdict is only
    as trustworthy as the instrument that produced it.

``conforms_today``
    The subject is ``node.py``, and it already gets this right. These are regression
    guards: the behaviour is load-bearing, it works now, and a future fix must not
    break it while fixing something else.

    This fourth group is a deliberate addition to the three the harness was specified
    with. Half the required cases — zero inputs, a corrupted input refused, ``params``
    reaching the step unchanged, the marker written last — describe behaviour the node
    ALREADY has, and the specified three groups have no home for them. Leaving them
    unlabelled would have broken the rule the labels exist for: every test says what
    its subject is and what it is claiming about it.
"""

import pytest

EXPECTED_RED_REASON = (
    'expected to fail against node.py as it stands today; this is a written-down defect, '
    'and this test is the proof that fixing it fixed something real'
)

#: The subject is node.py, and it is broken today. Strict xfail: an unexpected PASS
#: fails the run, because a defect that quietly went away is a fact we need told.
expected_red_until_fixed = pytest.mark.expected_red_until_fixed

#: The subject is the platform (runner/agent/collector), not the node.
subject_is_platform = pytest.mark.subject_is_platform

#: The subject is this harness.
harness_self_test = pytest.mark.harness_self_test

#: The subject is node.py, and it passes today. A regression guard.
conforms_today = pytest.mark.conforms_today

GROUPS = ('expected_red_until_fixed', 'subject_is_platform', 'harness_self_test', 'conforms_today')
