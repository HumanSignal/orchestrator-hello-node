"""Being asked to stop, mid-transfer, with thirty seconds to say something useful.

When a run is cancelled — by a person, by a deadline, by the runner losing its lease —
the agent calls ``docker stop``: SIGTERM, then SIGKILL **thirty seconds later**. Those
thirty seconds are the step's entire opportunity to leave a record, and the contract is
explicit about what to leave: a marker with ``status: "cancelled"``, inventorying
whatever it managed to produce, and exit code 20.

Why it matters that a cancelled run still writes one: partial logs and partial outputs
are exactly what somebody will want to look at afterwards, and the collector's salvage
path can only publish what a marker names. A step that cannot be stopped converts
"cancelled after twenty minutes of work" into "cancelled, nothing to see" — or worse,
into "finished successfully" long after somebody pressed stop.

**A step must install a SIGTERM handler; it cannot rely on the default.** The step is
PID 1 in its own container, and the kernel does not apply a signal's DEFAULT action to
PID 1 at all — it delivers the signal only if a handler exists. So a program that
handles nothing does not "die on SIGTERM": SIGTERM is dropped on the floor, the program
carries on, and thirty seconds later SIGKILL ends it — which exits 137, a code the
contract classifies as TRANSIENT, so a deliberately cancelled job is queued for retry.

These tests are the slowest in the suite by design: each holds a transfer open, signals
the container in the middle of it, and waits. The last one deliberately pays the full
grace period, because that thirty seconds is the real cost of the defect.
"""

from __future__ import annotations

import time

from conformance import contract, docker
from conformance.fakes3 import delay_when, matching
from conformance.job import InputSpec
from conformance.markers import expected_red_until_fixed

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


@expected_red_until_fixed
def test_sigterm_during_a_download_leaves_a_cancelled_marker(make_job, sample_input):
    """Setup:    the store holds the first input's response open for twelve seconds.
    Action:   wait until the request has arrived, then ``docker stop`` with the agent's
              own thirty-second grace.
    Validate: a ``cancelled`` marker is in the store and the step exited 20.

    What happens today is not that the step dies without a marker — it is that nothing
    happens at all. SIGTERM is dropped (see the module docstring), the download
    completes, the step copies the file, writes ``result.json``, writes a marker saying
    ``succeeded`` and exits 0. A run somebody cancelled is recorded as a success.
    """
    job = make_job(inputs=[sample_input])
    job.endpoint.hooks.on_request.append(delay_when(matching('input', index=1), HELD_OPEN_SECONDS))

    container = job.start()
    assert job.endpoint.wait_for('input', timeout=60), 'the step never started reading'
    grace_used = container.stop(grace=docker.STOP_GRACE_SECONDS)
    result = container.collect()

    _assert_cancelled_cleanly(job, result, grace_used)


@expected_red_until_fixed
def test_sigterm_during_an_upload_leaves_a_cancelled_marker(make_job, sample_input):
    """Setup:    the store holds the FIRST upload open for twelve seconds.
    Action:   wait until the upload has arrived, then ``docker stop``.
    Validate: a ``cancelled`` marker, and exit 20.

    Cancelling during a write is the harder half: the step has produced something, so
    the marker it leaves is not merely a status — it is the inventory that decides
    whether that work is salvaged or abandoned.
    """
    job = make_job(inputs=[sample_input])
    job.endpoint.hooks.on_request.append(delay_when(matching('upload', index=1), HELD_OPEN_SECONDS))

    container = job.start()
    assert job.endpoint.wait_for('upload', timeout=60), 'the step never started uploading'
    grace_used = container.stop(grace=docker.STOP_GRACE_SECONDS)
    result = container.collect()

    _assert_cancelled_cleanly(job, result, grace_used)


@expected_red_until_fixed
def test_a_cancelled_step_stops_taking_on_new_work(make_job):
    """Cancellation has to change what the step does next, not just how it ends.

    Setup:    three inputs. The store holds the FIRST upload open for twelve seconds, so
              the step is mid-write with two inputs still unread when the signal lands.
    Action:   stop the container.
    Validate: the second and third inputs are never fetched, and the marker says
              ``cancelled`` while inventorying the object that did land.

    This is the test that separates "shuts down cleanly" from "ignores the request and
    happens to finish". Today the step reads and uploads all three inputs after being
    told to stop — the signal changes nothing at all — and the only reason the run ends
    is that it ran out of work to do.
    """
    job = make_job(inputs=THREE_INPUTS)
    job.endpoint.hooks.on_request.append(delay_when(matching('upload', index=1), HELD_OPEN_SECONDS))

    container = job.start()
    assert job.endpoint.wait_for('upload', timeout=60), 'the step never started uploading'
    container.stop(grace=docker.STOP_GRACE_SECONDS)
    result = container.collect()

    assert job.endpoint.count_of('input') == 1, (
        f'after being asked to stop, the step went on to fetch '
        f'{job.endpoint.count_of("input")} of {len(THREE_INPUTS)} inputs; it exited {result.exit_code}'
    )
    landed = [key for key in job.endpoint.keys_in_order() if key.startswith('outputs/')]
    marker = job.marker()
    assert marker['status'] == 'cancelled', f'the marker says {marker["status"]!r}'
    assert set(landed) <= {obj['relpath'] for obj in marker['objects']}


@expected_red_until_fixed
def test_a_step_that_cannot_be_stopped_costs_the_whole_grace_period(make_job, sample_input):
    """The price of ignoring SIGTERM, measured.

    Setup:    the store holds the first upload open for longer than the grace period, so
              the step cannot finish its way out of the situation.
    Action:   stop the container and time how long ``docker stop`` takes.
    Validate: it returns well inside the thirty seconds, and the exit code is 20.

    Today this test takes the full thirty seconds and then the container is SIGKILLed.
    Two costs follow, and neither is visible from inside the step: a runner slot is held
    for half a minute per cancelled job, and the exit code is 137 — which is not one of
    the five the contract knows, so it classifies as TRANSIENT and the orchestrator
    schedules a retry of work a human explicitly cancelled.
    """
    job = make_job(inputs=[sample_input])
    job.endpoint.hooks.on_request.append(delay_when(matching('upload', index=1), HELD_PAST_THE_GRACE))

    container = job.start()
    assert job.endpoint.wait_for('upload', timeout=60), 'the step never started uploading'
    began = time.monotonic()
    grace_used = container.stop(grace=docker.STOP_GRACE_SECONDS)
    result = container.collect()

    assert grace_used < docker.STOP_GRACE_SECONDS - 1, (
        f'the step ignored SIGTERM: docker had to wait the full {docker.STOP_GRACE_SECONDS}s grace '
        f'({time.monotonic() - began:.1f}s measured) and then SIGKILL it. It exited {result.exit_code}, '
        f'which classifies as {contract.classify_exit(result.exit_code)!r}'
    )
    assert result.exit_code == contract.EXIT_CANCELLED


def _assert_cancelled_cleanly(job, result, grace_used: float) -> None:
    uploaded = job.endpoint.keys_in_order()
    assert contract.MARKER_FILENAME in uploaded, (
        f'the step was stopped and left no marker; the store saw {uploaded or "nothing"}. '
        f'It exited {result.exit_code}, which classifies as {contract.classify_exit(result.exit_code)!r}'
    )
    marker = job.marker()
    assert marker['status'] == 'cancelled', (
        f'the step was asked to stop and its marker says {marker["status"]!r} — the request changed nothing'
    )
    assert result.exit_code == contract.EXIT_CANCELLED, (
        f'exit {result.exit_code} classifies as {contract.classify_exit(result.exit_code)!r}, '
        f'not {contract.CLASS_CANCELLED!r}'
    )
    assert marker['exit_code'] == contract.EXIT_CANCELLED
    assert grace_used < docker.STOP_GRACE_SECONDS - 1, 'the container had to be SIGKILLed'
