"""How a step fails, which is most of what anyone will ever read about it.

Two things are load-bearing when a run goes wrong, and one thing is NOT.

**The exit code is load-bearing**, because it is the only signal available if the step
dies before it can write anything, and because it decides whether the orchestrator
retries. The numbers mean what ``external/contract.py`` says they mean: ``1`` asks for a
retry, ``10`` says never retry me.

**The marker is load-bearing**, because "the step said why" is a far better failure
report than "the process died", and because a failure marker's inventory is the only
thing the collector's salvage path can publish.

**In-node retry is NOT required.** This file used to demand that a step retry a 503
itself. It does not have to: the contract's whole mechanism for a transient fault is to
exit ``1`` and let the ORCHESTRATOR re-run the attempt. Retrying in-process is a
performance choice (it avoids redoing the work already done), not a conformance one.
What the contract does require is that the step tell the truth about which kind of
failure it had — and that is where the real defect is.
"""

from __future__ import annotations

from conformance import contract
from conformance.fakes3 import matching, refuse_when
from conformance.job import InputSpec
from conformance.markers import conforms_today, expected_red_until_fixed, reference_quality, traces_to

THREE_INPUTS = [
    InputSpec(relpath='one.csv', data=b'one\n'),
    InputSpec(relpath='two.csv', data=b'two\n'),
    InputSpec(relpath='three.csv', data=b'three\n'),
]


@conforms_today
@traces_to(
    'external/contract.py classify_exit: "Unknown codes classify as transient … a step that means "do '
    'not retry me" must say so with EXIT_PERMANENT" — with EXIT_PERMANENT = 10.'
)
def test_a_permanent_failure_writes_a_marker_and_exits_ten(make_job):
    """A failed run still writes a marker. Setup: a corrupted input. Action: run.
    Validate: exit 10, a ``failed`` marker, and a reason."""
    job = make_job(inputs=[InputSpec(relpath='bad.csv', data=b'pinned', served=b'pinnEd')])
    result = job.run()

    assert result.exit_code == contract.EXIT_PERMANENT
    assert contract.classify_exit(result.exit_code) == contract.CLASS_PERMANENT
    marker = job.marker()
    assert marker['status'] == 'failed'
    assert marker['error'], 'the marker gives no reason'
    assert contract.MARKER_FILENAME == job.endpoint.keys_in_order()[-1]


@conforms_today
@traces_to(
    'external/contract.py: EXIT_TRANSIENT = 1, and classify_exit maps it to "transient" — the code that '
    'asks the orchestrator to run the attempt again. The step does not have to retry anything itself.'
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
    Validate: **if** the marker carries an ``exit_code``, it equals the process's.

    ``exit_code`` is optional (``ContractInt | None``), and a marker that omits it is
    perfectly conformant — so this asserts nothing about markers that stay silent. Today
    the node is not silent: it writes 10 for every unsuccessful run whatever the process
    returned, and here the process returns 1. The orchestrator then retries the attempt
    (correct, from the exit code) while the sentence attached to the run says the step
    exited 10 — a permanent failure — which is the document anybody investigating reads
    first.
    """
    job = make_job(inputs=THREE_INPUTS)
    job.endpoint.hooks.on_request.append(refuse_when(matching('input', name='input/two.csv'), status=503))

    result = job.run()

    assert result.exit_code not in (0, None), 'the run was supposed to fail'
    marker = job.marker()
    stated = marker.get('exit_code')
    if stated is None:
        return  # silence is legal; nothing to contradict
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
    Validate: the failure marker inventories what really landed — the first output — so
              collection can salvage and publish it.

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

    inventoried = {obj['relpath'] for obj in job.marker()['objects']}
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
