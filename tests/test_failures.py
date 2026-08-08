"""How a step fails, which is most of what anyone will ever read about it.

Three things are load-bearing when a run goes wrong:

* **the exit code**, because it is the only signal available if the step dies before it
  can write anything — and it decides whether the orchestrator retries;
* **the marker**, because "the step said why" is a far better failure report than "the
  process died", and because a failure marker is what makes partial output salvageable;
* **the two agreeing**, because a marker whose ``exit_code`` contradicts the process's
  is a document that will be believed over the truth.
"""

from __future__ import annotations

from conformance import contract
from conformance.fakes3 import matching, refuse_when
from conformance.job import InputSpec
from conformance.markers import conforms_today, expected_red_until_fixed

THREE_INPUTS = [
    InputSpec(relpath='one.csv', data=b'one\n'),
    InputSpec(relpath='two.csv', data=b'two\n'),
    InputSpec(relpath='three.csv', data=b'three\n'),
]


@conforms_today
def test_a_permanent_failure_writes_a_marker_and_exits_ten(make_job):
    """A failed run still writes a marker. Setup: a corrupted input. Action: run.
    Validate: exit 10, a ``failed`` marker, and an error sentence naming the object."""
    job = make_job(inputs=[InputSpec(relpath='bad.csv', data=b'pinned', served=b'pinnEd')])
    result = job.run()

    assert result.exit_code == contract.EXIT_PERMANENT
    assert contract.classify_exit(result.exit_code) == contract.CLASS_PERMANENT
    marker = job.marker()
    assert marker['status'] == 'failed'
    assert marker['error'], 'the marker gives no reason'
    assert contract.MARKER_FILENAME == job.endpoint.keys_in_order()[-1]


@expected_red_until_fixed
def test_what_a_failed_run_already_produced_is_still_salvageable(make_job):
    """A failure after three successful uploads must not throw those three away.

    Setup:    three inputs; the SECOND is served with bytes that do not match its pin,
              so the step fails after it has already copied the first one.
    Action:   run.
    Validate: the failure marker inventories what really landed — the first output —
              so collection can salvage and publish it.

    The collector's salvage path reads the marker's ``objects`` list and copies exactly
    what is named there. A failure marker with an empty inventory therefore does not
    mean "nothing was produced": it means "everything produced is abandoned in a staging
    directory nobody will ever look at again". On a wide batch that is hours of work,
    and the run's own log is the only trace it happened.
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
def test_a_single_transient_refusal_is_retried(make_job):
    """One 503 is not a reason to fail a job.

    Setup:    three inputs; the store refuses the FIRST read once with 503 and serves
              everything afterwards.
    Action:   run.
    Validate: the step finishes normally.

    Object storage answers 503 (``SlowDown``) under load and 500 on its own internal
    faults, and both are documented as retryable. A step that gives up on the first one
    turns a momentary hiccup into a failed pipeline run, and the orchestrator's own
    retry then re-does the ENTIRE step — every read, every write — instead of the one
    request that stumbled.
    """
    job = make_job(inputs=THREE_INPUTS)
    job.endpoint.hooks.on_request.append(refuse_when(matching('input', index=1), status=503, code='SlowDown'))

    result = job.run()

    assert result.exit_code == 0, f'a single 503 ended the run:\n{result.output}'
    assert contract.MARKER_FILENAME in job.endpoint.keys_in_order()


@expected_red_until_fixed
def test_a_transiently_refused_upload_is_retried(make_job, sample_input):
    """The same rule on the way out. Setup: the first upload is refused once with 503.
    Action: run. Validate: the object lands and the run succeeds.

    Uploads are where a step has the most to lose from giving up: everything before this
    point has already been paid for. Today ANY non-2xx on an upload — 503 included — is
    raised as the step's own permanent-failure type, so a momentary refusal is reported
    to the orchestrator as "do not retry me".
    """
    job = make_job(inputs=[sample_input])
    job.endpoint.hooks.on_request.append(refuse_when(matching('upload', index=1), status=503, code='SlowDown'))

    result = job.run()

    assert result.exit_code == 0, f'a single 503 on an upload ended the run:\n{result.output}'
    assert 'outputs/data.csv' in job.endpoint.keys_in_order()


@expected_red_until_fixed
def test_a_persistent_transient_fault_is_reported_as_transient_in_both_places(make_job):
    """The marker's exit code and the process's exit code must be the same number.

    Setup:    a store that refuses one input's reads with 503 every time, so no amount
              of retrying helps.
    Action:   run.
    Validate: the process exits with a code the contract classifies as transient, and
              the marker's own ``exit_code`` is that same code.

    The marker currently reports 10 (permanent) for every unsuccessful run, whatever the
    process actually returned. The orchestrator reads the exit code for its retry
    decision and the marker for its report, so the run is retried while the record
    attached to it says it never should be — and whoever reads that record afterwards
    is looking at a document that contradicts what happened.
    """
    job = make_job(inputs=THREE_INPUTS)
    job.endpoint.hooks.on_request.append(refuse_when(matching('input', name='input/two.csv'), status=503))

    result = job.run()

    assert result.exit_code not in (0, None), 'the run was supposed to fail'
    assert contract.classify_exit(result.exit_code) == contract.CLASS_TRANSIENT, (
        f'exit {result.exit_code} classifies as {contract.classify_exit(result.exit_code)!r}, '
        f'but a store that keeps answering 503 is a transient fault'
    )
    marker = job.marker()
    assert marker['exit_code'] == result.exit_code, (
        f'the marker says the step exited {marker["exit_code"]}, the process exited {result.exit_code}'
    )


@expected_red_until_fixed
def test_a_failure_before_the_manifest_is_read_still_writes_a_marker(make_job, sample_input):
    """The marker is how a step reports; it must not depend on the step having got far.

    Setup:    the store refuses every read of ``invocation.json``.
    Action:   run.
    Validate: a ``failed`` marker is written anyway, carrying the launch's identity.

    The step's own docstring says it never raises and that every failure becomes a
    marker plus an exit code — but the two calls that load the credentials and fetch the
    manifest sit OUTSIDE the block that makes that true. A failure there produces a
    Python traceback and nothing else, and the orchestrator is left inferring what
    happened from an exit code.

    The identity fields are available without the manifest: they are four of the nine
    variables the agent injects (``LSPO_EXECUTION_ID``, ``LSPO_ATTEMPT``,
    ``LSPO_GENERATION``, ``LSPO_IDEMPOTENCY_KEY``). That is what they are FOR — a step
    that can only identify itself by quoting a document it may not have been able to
    read cannot report the failure to read it.
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
