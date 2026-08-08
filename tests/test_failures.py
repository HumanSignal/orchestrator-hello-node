"""How a step fails, which is most of what anyone will ever read about it.

Two things are load-bearing when a run goes wrong, and one thing is NOT.

**The exit code is load-bearing**, because it is the only signal available if the step
dies before it can write anything, and because it decides whether the orchestrator
retries. The numbers mean what ``external/contract.py`` says they mean: ``1`` asks for a
retry, ``10`` says never retry me.

**The marker is load-bearing but it is not REQUIRED**, and this file used to blur the
two. ``pipelines/external_finalize.py`` ``_step_account`` says a failed step is "invited
by the contract to write a marker", ``_marker_for_this_attempt`` treats an absent one as
an ordinary outcome, and ``error`` is declared ``str | None = None``. So "a failed run
writes a marker, carrying a reason" is what a reference node should do, not a rule — and
it is labelled that way now. What the contract does constrain is the marker a step
CHOOSES to write: every test below that reads one asks first whether one exists.

**In-node retry is NOT required.** This file used to demand that a step retry a 503
itself. It does not have to: the contract's whole mechanism for a transient fault is to
exit ``1`` and let the ORCHESTRATOR re-run the attempt. Retrying in-process is a
performance choice (it avoids redoing the work already done), not a conformance one.
What the contract does require is that the step tell the truth about which kind of
failure it had — and that is where the real defect is.
"""

from __future__ import annotations

import pytest

from conformance import contract
from conformance.fakes3 import matching, refuse_when
from conformance.job import InputSpec
from conformance.markers import conforms_today, expected_red_until_fixed, reference_quality, traces_to

THREE_INPUTS = [
    InputSpec(relpath='one.csv', data=b'one\n'),
    InputSpec(relpath='two.csv', data=b'two\n'),
    InputSpec(relpath='three.csv', data=b'three\n'),
]

_A_MARKER_IS_INVITED = (
    'The contract INVITES a failure marker rather than requiring one, and its error field is optional. '
    'pipelines/external_finalize.py _step_account: "A failed or cancelled step is invited by the '
    'contract to write a marker carrying its error and exit_code, and that is the most useful sentence '
    'anyone will read about the run — the orchestrator can only ever say \'it failed\', while the step '
    'can say what failed." _marker_for_this_attempt treats an absent marker as an ordinary outcome, and '
    'external/contract.py declares "error: str | None = None". So a silent failure is conformant. A '
    'reference node should still explain itself, because the alternative is that every failure of every '
    'node copied from this one is diagnosed from an exit code alone.'
)


def _marker_if_the_step_wrote_one(job):
    """The marker, or a skip. Absence is legal, so it must not read as a pass or a fail.

    Returning early would count as a pass — and under strict xfail an expected-red test
    that passes turns the run red, which would be a false alarm about a step that did
    something the contract permits. Skipping says the honest thing: the rule this test
    restates had nothing to constrain in this run.
    """
    if contract.MARKER_FILENAME not in job.endpoint.keys_in_order():
        pytest.skip(
            'the step wrote no completion marker, which the contract permits — the rule this test '
            'restates constrains the marker a step CHOOSES to write, so there is nothing to check here. '
            'That the marker is missing at all is a reference-quality gap, measured by '
            'test_a_failed_run_explains_itself_in_a_marker'
        )
    return job.marker()


@conforms_today
@traces_to(
    'external/contract.py: "The process exit code is the ONLY signal available when a job dies before it '
    'can write a marker, so the numbers carry meaning", with EXIT_PERMANENT = 10 and classify_exit: '
    '"Unknown codes classify as transient … a step that means "do not retry me" must say so with '
    'EXIT_PERMANENT."'
)
def test_a_permanent_failure_exits_ten(make_job):
    """The code a step returns for a failure it knows will not go away.

    Setup:    an input whose bytes do not match its pin — a failure re-running cannot fix.
    Action:   run.
    Validate: the process exits 10, which the contract classifies as ``permanent``.

    Only the exit code. Whether a marker was written, and what it says, is a separate
    question with a separate authority, and this test used to answer both at once under
    the citation for one of them.
    """
    job = make_job(inputs=[InputSpec(relpath='bad.csv', data=b'pinned', served=b'pinnEd')])
    result = job.run()

    assert result.exit_code == contract.EXIT_PERMANENT
    assert contract.classify_exit(result.exit_code) == contract.CLASS_PERMANENT


@conforms_today
@reference_quality(_A_MARKER_IS_INVITED)
def test_a_failed_run_explains_itself_in_a_marker(make_job):
    """"The step said why" is a far better failure report than "the process died".

    Setup:    the same unverifiable input.
    Action:   run.
    Validate: a marker was written, it says ``failed``, and it carries a reason.

    Every one of those three is optional as far as the contract goes. Together they are
    the difference between a run somebody can diagnose and one they cannot.
    """
    job = make_job(inputs=[InputSpec(relpath='bad.csv', data=b'pinned', served=b'pinnEd')])
    result = job.run()
    assert result.exit_code != 0, 'the run was supposed to fail'

    uploaded = job.endpoint.keys_in_order()
    assert contract.MARKER_FILENAME in uploaded, f'the step failed silently; the store saw {uploaded}'
    marker = job.marker()
    assert marker['status'] == 'failed'
    assert marker['error'], 'the marker gives no reason, so the run is diagnosable only from an exit code'


@conforms_today
@traces_to(
    'external/contract.py, on the exit codes: "The process exit code is the ONLY signal available when a '
    'job dies before it can write a marker, so the numbers carry meaning. Anything unrecognised is '
    'treated as transient (retry) — the safe default, because a step that failed permanently is expected '
    'to say so explicitly." EXIT_TRANSIENT = 1 is the code that asks the orchestrator to run the attempt '
    'again; nothing asks the step to retry anything itself.'
)
def test_a_transient_read_refusal_is_reported_as_transient(make_job):
    """A store that keeps answering 503 must end the run with a retryable exit code.

    Setup:    three inputs; the store refuses every read of the second with 503.
    Action:   run.
    Validate: the process exits with a code the contract classifies as ``transient``, so
              the orchestrator schedules another attempt.

    This is the half the node already gets right, and it is asserted here as a regression
    guard because the fix for the upload half (below) is the obvious place to break it.
    Nothing here requires the step to retry the request itself; a step that gave up
    immediately and exited 1 is behaving exactly as the contract intends.
    """
    job = make_job(inputs=THREE_INPUTS)
    job.endpoint.hooks.on_request.append(refuse_when(matching('input', name='input/two.csv'), status=503))

    result = job.run()

    assert result.exit_code not in (0, None), 'the run was supposed to fail'
    assert contract.classify_exit(result.exit_code) == contract.CLASS_TRANSIENT, (
        f'exit {result.exit_code} classifies as {contract.classify_exit(result.exit_code)!r}, but a store '
        f'answering 503 SlowDown is a transient fault and the orchestrator should retry the attempt'
    )


@expected_red_until_fixed
@traces_to(
    'external/contract.py: EXIT_PERMANENT = 10 and classify_exit — "a step that means "do not retry me" '
    'must say so with EXIT_PERMANENT". Reporting a momentary store refusal that way tells the platform '
    'never to retry work that would have succeeded on the next attempt.'
)
def test_a_transient_upload_refusal_is_not_reported_as_permanent(make_job, sample_input):
    """The step must not tell the orchestrator "never retry" about a 503.

    Setup:    one input; the store refuses the FIRST upload once with 503 (``SlowDown``),
              and accepts everything afterwards.
    Action:   run.
    Validate: whatever the step does about it, the exit code does not classify as
              ``permanent``.

    This is deliberately weaker than the test it replaces, which demanded that the step
    retry the upload in-process. It need not: exiting 1 and letting the orchestrator
    re-run the attempt is a correct response, and so is retrying. What is NOT correct is
    today's behaviour — ``_post_object`` raises the step's own permanent-failure type for
    every status outside 200/201/204, so a momentary refusal from object storage is
    recorded as a step that must never be run again. Object storage answers 503 under
    load; permanent is the one answer that cannot be walked back.
    """
    job = make_job(inputs=[sample_input])
    job.endpoint.hooks.on_request.append(refuse_when(matching('upload', index=1), status=503, code='SlowDown'))

    result = job.run()

    assert contract.classify_exit(result.exit_code) != contract.CLASS_PERMANENT, (
        f'a single 503 on an upload ended the run with exit {result.exit_code}, which classifies as '
        f'{contract.classify_exit(result.exit_code)!r} — the orchestrator will not retry it:\n{result.output}'
    )


@expected_red_until_fixed
@traces_to(
    'external/contract.py CompletionMarker.exit_code: "Process exit code, when the runner observed one." '
    'pipelines/external_finalize.py _step_account renders it verbatim into the run\'s failure reason: '
    '"The step exited with code {marker.exit_code} and reported: …", so a marker that states a code the '
    'process did not return puts a false sentence in the record a human reads.'
)
def test_a_stated_exit_code_matches_the_one_the_process_returned(make_job):
    """If the marker states an exit code, it must be the code the process really used.

    Setup:    a store that refuses one input's reads with 503 every time.
    Action:   run.
    Validate: **if** there is a marker and **if** it carries an ``exit_code``, that code
              equals the process's.

    Both conditions are real. A failed step need not write a marker at all, and
    ``exit_code`` is optional within one (``ContractInt | None``) — a marker that stays
    silent about it is perfectly conformant, so this asserts nothing about one. Today the
    node is not silent: it writes 10 for every unsuccessful run whatever the process
    returned, and here the process returns 1. The orchestrator then retries the attempt
    (correct, from the exit code) while the sentence attached to the run says the step
    exited 10 — a permanent failure — which is the document anybody investigating reads
    first.
    """
    job = make_job(inputs=THREE_INPUTS)
    job.endpoint.hooks.on_request.append(refuse_when(matching('input', name='input/two.csv'), status=503))

    result = job.run()

    assert result.exit_code not in (0, None), 'the run was supposed to fail'
    marker = _marker_if_the_step_wrote_one(job)
    stated = marker.get('exit_code')
    if stated is None:
        pytest.skip('the marker states no exit_code, which the contract permits — there is nothing to contradict')
    assert stated == result.exit_code, (
        f'the marker says the step exited {stated}, the process exited {result.exit_code}. '
        f'The failure reason attached to this run will read "The step exited with code {stated}"'
    )


@expected_red_until_fixed
@traces_to(
    'external/contract.py CompletionMarker.objects: "Every object produced, with hash and size". '
    'pipelines/external_finalize.py _salvage_what_the_step_produced iterates "for obj in marker.objects" '
    'and publishes exactly those — "Publish what a FAILED or cancelled step managed to write."'
)
def test_what_a_failed_run_already_produced_is_still_salvageable(make_job):
    """A failure after a successful upload must not throw that upload away.

    Setup:    three inputs; the SECOND is served with bytes that do not match its pin,
              so the step fails after it has already copied the first one.
    Action:   run.
    Validate: **if** the step wrote a marker, its inventory names what really landed —
              the first output — so collection can salvage and publish it.

    The condition matters: no rule requires a failed step to write a marker. What the
    rule requires is that a marker's ``objects`` be "every object produced", so an
    inventory that omits something the step really wrote is a document contradicting its
    own definition.

    The collector's salvage path reads the marker's ``objects`` list and copies exactly
    what is named there. A failure marker with an empty inventory therefore does not mean
    "nothing was produced": it means "everything produced is abandoned in a staging
    directory nobody will ever look at again". On a wide batch that is hours of work, and
    the run's own log is the only trace it happened.
    """
    job = make_job(
        inputs=[
            THREE_INPUTS[0],
            InputSpec(relpath='two.csv', data=b'pinned', served=b'pinnEd'),
            THREE_INPUTS[2],
        ]
    )
    result = job.run()
    assert result.exit_code != 0, 'the run was supposed to fail'

    landed = [key for key in job.endpoint.keys_in_order() if key.startswith('outputs/')]
    assert landed, 'nothing was uploaded before the failure, so this test proves nothing'

    inventoried = {obj['relpath'] for obj in _marker_if_the_step_wrote_one(job)['objects']}
    assert set(landed) <= inventoried, (
        f'{sorted(set(landed) - inventoried)} were uploaded and then abandoned: the failure marker '
        f'inventories {sorted(inventoried) or "nothing"}, so salvage will publish none of them'
    )


@expected_red_until_fixed
@reference_quality(
    'The contract INVITES a failure marker rather than requiring one — pipelines/external_finalize.py '
    '_step_account: "A failed or cancelled step is invited by the contract to write a marker carrying '
    'its error and exit_code", and _marker_for_this_attempt treats an absent marker as an ordinary '
    'outcome. So this is not a conformance failure. It is a promise THIS file makes and breaks: '
    'main()\'s own docstring says "Never raises: every failure becomes a marker plus an exit code", '
    'while the two calls that load the credentials and fetch the manifest sit outside the block that '
    'would make it true — and the identity a marker needs is in the injected environment, so the '
    'promise is keepable.'
)
def test_a_failure_before_the_manifest_is_read_still_writes_a_marker(make_job, sample_input):
    """The step's own promise, tested where it is hardest to keep.

    Setup:    the store refuses every read of ``invocation.json``.
    Action:   run.
    Validate: a ``failed`` marker is written anyway, carrying the launch's identity.

    The orchestrator survives this fine — it records a failed attempt with no step
    account. What is lost is the step's own explanation, on precisely the failures that
    are hardest to diagnose from the outside: a credential problem, an unreachable store,
    an unreadable manifest. ``LSPO_EXECUTION_ID``, ``LSPO_ATTEMPT``, ``LSPO_GENERATION``
    and ``LSPO_IDEMPOTENCY_KEY`` are four of the variables the agent injects, and this is
    what they are for — a step that can only identify itself by quoting a document it may
    not have been able to read cannot report its failure to read it.
    """
    job = make_job(inputs=[sample_input])
    job.endpoint.hooks.on_request.append(refuse_when(matching('manifest'), status=503))

    result = job.run()

    assert result.exit_code != 0, 'the run was supposed to fail'
    uploaded = job.endpoint.keys_in_order()
    assert contract.MARKER_FILENAME in uploaded, (
        f'the step died before its manifest and wrote no marker at all; the store saw {uploaded or "nothing"}'
    )
    marker = job.marker()
    assert marker['status'] == 'failed'
    assert marker['execution_id'] == job.execution_id
    assert marker['generation'] == job.generation
