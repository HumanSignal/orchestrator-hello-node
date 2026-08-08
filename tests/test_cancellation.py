"""Being asked to stop, mid-transfer, with thirty seconds to say something useful.

When a run is cancelled — by a person, by a deadline, by the runner losing its lease —
the agent calls its executor's ``stop``: SIGTERM, then SIGKILL **thirty seconds later**
(``agent/executors/docker_exec.py``: ``def stop(self, handle, timeout: float = 30.0)``).

**What is NOT at stake, contrary to what this file used to say.** The run is recorded as
cancelled either way. ``agent/runner.py`` ``_classify`` asks whether cancellation was
requested BEFORE it looks at the exit code::

    if context.cancel_requested.is_set() or exit_code == EXIT_CANCELLED:
        return 'cancelled', 'cancelled on request'

so a step that ignores SIGTERM and is SIGKILLed still ends as ``cancelled``, not as a
retry. Exit code 20 and a marker saying ``exit_code: 20`` are therefore NOT required, and
the tests here no longer demand them. That was the harness inventing a rule.

**What is genuinely at stake** is three things, none of them invisible:

* **Thirty seconds of a runner slot, per cancelled job.** The grace period is paid in
  full by a step that cannot be stopped. Cancel a batch of two hundred and that is over
  an hour and a half of capacity spent on work somebody already said they did not want.
* **A receipt that contradicts the record.** The step keeps working after the stop
  request and writes ``status: "succeeded"``. The launch is cancelled; the document
  inside it says the step finished its work. ``_step_account`` quotes that document into
  what a human reads about the run.
* **Work that was explicitly cancelled being done anyway** — every remaining input
  fetched, copied and paid for after the request to stop.

**A step must install a SIGTERM handler; it cannot rely on the default.** The step is
PID 1 in its own container, and the kernel does not apply a signal's DEFAULT action to
PID 1 — it delivers the signal only if a handler exists. So a program that handles
nothing does not "die on SIGTERM": the signal is dropped on the floor and the program
carries on.

These tests are the slowest in the suite by design. The last one deliberately pays the
full grace period, because that thirty seconds IS the finding.
"""

from __future__ import annotations

import time

from conformance import contract, docker
from conformance.fakes3 import delay_when, matching
from conformance.job import InputSpec
from conformance.markers import expected_red_until_fixed, reference_quality

#: Long enough that the signal lands squarely inside the transfer, short enough that a
#: correct implementation finishes well inside the grace period.
HELD_OPEN_SECONDS = 12.0

#: Longer than the grace, so a step that ignores the signal is SIGKILLed rather than
#: getting away with finishing the work it was told to abandon.
HELD_PAST_THE_GRACE = docker.STOP_GRACE_SECONDS + 15.0

THREE_INPUTS = [
    InputSpec(relpath='one.csv', data=b'one\n'),
    InputSpec(relpath='two.csv', data=b'two\n'),
    InputSpec(relpath='three.csv', data=b'three\n'),
]

_COOPERATIVE_SHUTDOWN = (
    'Cooperative shutdown is not a contract rule and this harness no longer pretends it is: '
    'agent/runner.py _classify checks cancel_requested BEFORE the exit code, so a SIGKILLed cancelled '
    'container is still recorded as cancelled, and external/contract.py makes exit_code and status '
    '"cancelled" available without requiring them. It is a reference-quality expectation with a measured '
    'price: the full 30s grace of a runner slot per cancelled job '
    '(agent/executors/docker_exec.py stop(timeout=30.0)), and a marker claiming "succeeded" inside a '
    'launch the orchestrator has recorded as cancelled.'
)


@expected_red_until_fixed
@reference_quality(_COOPERATIVE_SHUTDOWN)
def test_a_step_stopped_during_a_download_does_not_claim_it_succeeded(make_job, sample_input):
    """Setup:    the store holds the first input's response open for twelve seconds.
    Action:   wait until the request has arrived, then stop the container with the
              agent's own thirty-second grace.
    Validate: the container stops well inside the grace, and whatever marker it leaves
              does not say ``succeeded``.

    What happens today is not that the step dies without a marker — it is that nothing
    happens at all. SIGTERM is dropped (see the module docstring), the download
    completes, the step copies the file, writes ``result.json``, writes a marker saying
    ``succeeded`` and exits 0. The run is recorded as cancelled, and the receipt inside
    it says the work was finished.
    """
    job = make_job(inputs=[sample_input])
    job.endpoint.hooks.on_request.append(delay_when(matching('input', index=1), HELD_OPEN_SECONDS))

    container = job.start()
    assert job.endpoint.wait_for('input', timeout=60), 'the step never started reading'
    grace_used = container.stop(grace=docker.STOP_GRACE_SECONDS)
    result = container.collect()

    _assert_stopped_cleanly(job, result, grace_used)


@expected_red_until_fixed
@reference_quality(_COOPERATIVE_SHUTDOWN)
def test_a_step_stopped_during_an_upload_inventories_what_it_left_behind(make_job):
    """Setup:    TWO inputs, with the store holding the SECOND upload open for twelve
              seconds — so the first output has genuinely landed and been acknowledged
              before anything is signalled.
    Action:   wait until the second upload has arrived, then stop the container.
    Validate: it stops inside the grace, does not claim success, and the marker it leaves
              inventories every object that actually landed.

    **Two inputs, and that is the whole design of this test.** With one, the object whose
    upload is being held has not been recorded yet — this store, like a real one, records
    an upload when it answers, not when the bytes arrive — so "everything that landed is
    inventoried" would compare an empty set against whatever the marker says and be true
    however the step behaved. It failed today only because of the assertions above it.
    Holding the SECOND upload puts one acknowledged object on the ledger, which is what
    the step then has to account for.

    Cancelling during a write is the harder half: the step has produced something, so the
    marker it leaves is not merely a status — it is the inventory that decides whether
    that work is salvaged or abandoned (``_salvage_what_the_step_produced`` publishes
    exactly what the marker names).
    """
    job = make_job(inputs=THREE_INPUTS[:2])
    job.endpoint.hooks.on_request.append(delay_when(matching('upload', index=2), HELD_OPEN_SECONDS))

    container = job.start()
    assert _wait_for_upload_number(job, 2), 'the step never reached its second upload'
    grace_used = container.stop(grace=docker.STOP_GRACE_SECONDS)
    result = container.collect()

    _assert_stopped_cleanly(job, result, grace_used)
    landed = [key for key in job.endpoint.keys_in_order() if key.startswith('outputs/')]
    assert landed, (
        'no upload had been acknowledged when the step stopped, so there is nothing an inventory could '
        'be missing and this test would prove nothing'
    )
    uploaded = job.endpoint.keys_in_order()
    assert contract.MARKER_FILENAME in uploaded, (
        f'the step left no marker at all, so the {len(landed)} object(s) it had already written are '
        f'abandoned: salvage publishes exactly what a marker names. The store holds {uploaded}'
    )
    inventoried = {obj['relpath'] for obj in job.marker()['objects']}
    assert set(landed) <= inventoried, (
        f'{sorted(set(landed) - inventoried)} landed before the step stopped but its marker does not '
        f'inventory them, so salvage will publish none of them'
    )


@expected_red_until_fixed
@reference_quality(_COOPERATIVE_SHUTDOWN)
def test_a_cancelled_step_stops_taking_on_new_work(make_job):
    """Cancellation has to change what the step does next, not just how it ends.

    Setup:    three inputs. The store holds the FIRST upload open for twelve seconds, so
              the step is mid-write with two inputs still unread when the signal lands.
    Action:   stop the container.
    Validate: the second and third inputs are never fetched.

    This is the test that separates "shuts down cleanly" from "ignores the request and
    happens to finish". Today the step reads and uploads all three inputs after being
    told to stop — the signal changes nothing at all — and the only reason the run ends
    is that it ran out of work to do. Inputs are counted as DISTINCT objects rather than
    as requests, so a step that retried one of them would not accidentally satisfy this.
    """
    job = make_job(inputs=THREE_INPUTS)
    job.endpoint.hooks.on_request.append(delay_when(matching('upload', index=1), HELD_OPEN_SECONDS))

    container = job.start()
    assert job.endpoint.wait_for('upload', timeout=60), 'the step never started uploading'
    container.stop(grace=docker.STOP_GRACE_SECONDS)
    result = container.collect()

    _assert_it_really_stopped(result)
    fetched = job.endpoint.names_of('input')
    assert fetched == ['input/one.csv'], (
        f'after being asked to stop, the step went on to fetch {fetched} — {len(fetched)} of '
        f'{len(THREE_INPUTS)} inputs; it exited {result.exit_code}'
    )


@expected_red_until_fixed
@reference_quality(_COOPERATIVE_SHUTDOWN)
def test_a_step_that_cannot_be_stopped_costs_the_whole_grace_period(make_job, sample_input):
    """The price of ignoring SIGTERM, measured.

    Setup:    the store holds the first upload open for longer than the grace period, so
              the step cannot finish its way out of the situation.
    Action:   stop the container and time it.
    Validate: it returns well inside the thirty seconds.

    Today this takes the full thirty seconds and then the container is SIGKILLed. The
    exit code that follows is 137, and this test deliberately does NOT assert anything
    about it: the run is recorded as cancelled regardless, because the agent asks whether
    cancellation was requested before it looks at any exit code. What is real is the
    stopwatch — half a minute of a runner slot, per cancelled job, spent waiting for a
    process that was never going to answer.
    """
    job = make_job(inputs=[sample_input])
    job.endpoint.hooks.on_request.append(delay_when(matching('upload', index=1), HELD_PAST_THE_GRACE))

    container = job.start()
    assert job.endpoint.wait_for('upload', timeout=60), 'the step never started uploading'
    began = time.monotonic()
    grace_used = container.stop(grace=docker.STOP_GRACE_SECONDS)
    result = container.collect()

    _assert_it_really_stopped(result)
    assert grace_used < docker.STOP_GRACE_SECONDS - 1, (
        f'the step ignored SIGTERM: docker had to wait the full {docker.STOP_GRACE_SECONDS}s grace '
        f'({time.monotonic() - began:.1f}s measured) and then SIGKILL it. It exited {result.exit_code}, '
        f'and every cancelled job costs that same half minute of a runner slot'
    )


def _wait_for_upload_number(job, wanted: int, timeout: float = 60.0) -> bool:
    """Block until the store has RECEIVED its ``wanted``-th upload request.

    ``Endpoint.wait_for`` answers "has any upload arrived yet?" and stays answered, which
    is not the question when a test needs to signal the container during a specific one.
    The counter is stamped when the request arrives, before any hook runs, so this
    returns while the upload is still in flight.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if job.endpoint.count_of('upload') >= wanted:
            return True
        time.sleep(0.05)
    return False


def _assert_it_really_stopped(result) -> None:
    """The container exited. Not "docker stop returned" — exited.

    ``Container.stop`` now raises if ``docker stop`` fails, which closes half of this,
    and this closes the other half: a container that is still running when it is
    collected has no exit code, and every assertion about what it did next is being made
    about a process that has not finished doing it.
    """
    assert result.still_running is False, (
        f'the container was still running when this test read its result, so nothing below is a '
        f'statement about a finished step. Its output so far:\n{result.output}'
    )


def _assert_stopped_cleanly(job, result, grace_used: float) -> None:
    """The two things a stopped step owes: a prompt exit, and no false claim of success.

    Deliberately silent about the exit CODE and about the marker's ``exit_code`` field.
    Exit 20 is available (``EXIT_CANCELLED``) and is the clearest thing to return, but
    the platform does not need it: the launch is already known to be cancelled.
    """
    _assert_it_really_stopped(result)
    assert grace_used < docker.STOP_GRACE_SECONDS - 1, (
        f'the container had to be SIGKILLed after the full {docker.STOP_GRACE_SECONDS}s grace '
        f'({grace_used:.1f}s); it exited {result.exit_code}'
    )
    uploaded = job.endpoint.keys_in_order()
    if contract.MARKER_FILENAME not in uploaded:
        return  # stopped before it could write one: no false claim, nothing to check
    status = job.marker()['status']
    assert status != 'succeeded', (
        f'the step was asked to stop and its marker says {status!r} — the launch is recorded as '
        f'cancelled, and the receipt inside it claims the work was finished'
    )
