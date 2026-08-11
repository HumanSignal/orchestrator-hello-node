"""Being asked to stop: what the step does next, and what it leaves behind.

When a run is cancelled — by a person, by a deadline, by the runner losing its lease —
the agent calls its executor's ``stop``: SIGTERM, then SIGKILL after a timeout
(``agent/executors/docker_exec.py``: ``def stop(self, handle: Any, timeout: float =
30.0)``). Thirty is that method's own default and every caller in today's build passes
nothing else — which makes it the interval this suite can observe, and **not** an interval
anybody is promised. Nothing guarantees any of it: a fence kills outright, a cancellation
may not be noticed until a heartbeat comes round, and the value passed to ``docker stop``
is the agent's to choose. That is the standing statement at the top of section 7 of
``docs/PROTOCOL.md`` and every test here inherits it.

**And the workload is never told.** Nothing in the nine injected variables, the credentials
envelope or the job description carries a stop deadline or a remaining time, and the stop
object the orchestrator composes on its heartbeat reaches the agent and stops there. So
every "deadline" a node sets for its own shutdown is a guess at a number it is not given —
best-effort by construction, and worth having only because the alternative is no bound at
all. Exposing the real one is an open platform task.

**What is NOT at stake.** The run is recorded as cancelled either way. ``agent/runner.py``
``_classify`` asks whether cancellation was requested BEFORE it looks at the exit code::

    if context.cancel_requested.is_set() or exit_code == EXIT_CANCELLED:
        return 'cancelled', 'cancelled on request'

so a step that ignores SIGTERM and is SIGKILLed still ends as ``cancelled``, not as a
retry. Nothing here may demand exit 20 as a rule. What that line does say, read the other
way round, is that ``exit_code == EXIT_CANCELLED`` is the whole of the distinction when
nobody asked: a step that stops itself and says 20 is recorded as stopped, and one that
says anything else is recorded as broken.

**What is genuinely at stake** is four things, none of them invisible:

* **The nominal grace, per cancelled job**, paid in full by a step that cannot be
  stopped. Cancel a batch of two hundred and that is over an hour and a half of capacity
  spent on work somebody already said they did not want.
* **Whether the step notices at all while it is inside a transfer.** A flag cannot be
  read by a process parked in a socket call, so a handler that only sets one leaves the
  step's stop latency equal to its network timeouts. Measured on this file's own previous
  version: **12.2 seconds**, all of it spent waiting for a response nobody needed.
* **The receipt.** Salvage publishes exactly what the marker names and nothing else, and
  the marker's ``error`` and ``exit_code`` are what a human reads about the run. A stop
  that leaves no receipt leaves no account and no inventory.
* **Work that was explicitly cancelled being done anyway** — every remaining input
  fetched, copied and paid for after the request to stop.

**A step must install a SIGTERM handler; it cannot rely on the default.** The step is
PID 1 in its own container, and the kernel does not apply a signal's DEFAULT action to
PID 1 — it delivers the signal only if a handler exists. So a program that handles
nothing does not "die on SIGTERM": the signal is dropped on the floor and the program
carries on.

**What these tests measure, and what they cannot.** A SIGTERM delivered here models the
node's own behaviour and nothing else. It is not evidence that a stopped run delivers a
partial result: on the deadline path the terminal report is refused and the marker is
never collected, and an operator's Cancel usually arrives as a SIGKILL
(``docs/PROTOCOL.md`` section 7). The receipt is written for the runs where it IS read,
and for the day those gaps close.

These tests are among the slowest in the suite by design. One deliberately holds a
response open past the whole nominal grace, because a store that never answers is exactly
the case in which a step's own timeouts decide whether anything gets written.
"""

from __future__ import annotations

import json
import time

import pytest

from conformance import contract, docker
from conformance.fakes3 import delay_when, drip_when, matching
from conformance.job import InputSpec
from conformance.markers import conforms_today, reference_quality
from conformance.stalling import TUNNEL_GRANTED, StallingEndpoint, a_body_that_stops

#: Long enough that the signal lands squarely inside the transfer, short enough that a
#: correct implementation finishes well inside the grace period.
HELD_OPEN_SECONDS = 12.0

#: Longer than the grace, so a step that ignores the signal is SIGKILLed rather than
#: getting away with finishing the work it was told to abandon.
HELD_PAST_THE_GRACE = docker.STOP_GRACE_SECONDS + 15.0

#: What "promptly" means here, and the number is ours to pick rather than derived from
#: anything. ``docs/CONFORMANCE.md`` tells a node author to assert that the process exited
#: "within a few seconds" and to keep the threshold small precisely BECAUSE the thirty
#: seconds is not a promise — a node sized against the nominal grace is a node that gets
#: nothing done on a stop that gives it none. Five is a few, and it is roughly twenty
#: times what this step actually takes, so the test fails on a change of mechanism rather
#: than on a slow machine.
PROMPTLY_SECONDS = 5.0

#: How much of a body the stalling listener delivers, and how much it promises. The sent
#: half has to exceed any socket buffer, so its own ``sendall`` cannot finish until the
#: step has consumed nearly all of it — that is the evidence the step is inside the body
#: and not still parsing headers. The promised half has to exceed the sent half, so there
#: is always something left to wait for.
STALLED_BODY_SENT = 16 * 1024 * 1024
STALLED_BODY_PROMISED = 64 * 1024 * 1024

#: The bar for the one wait nothing can interrupt — the name lookup, the TCP connect and
#: the TLS handshake, which happen inside a single call that hands out no socket to shut
#: down. A stop landing there cannot be acted on until the call returns, so what is
#: measured is that the step BOUNDS that stretch, not that it cuts it. Deliberately a
#: different number from the one above, and deliberately larger: two different mechanisms
#: must not hide behind one threshold.
#:
#: **The value is a judgement and says so.** It sits between the deadline a node should
#: give itself for reaching a store and the twenty-five seconds it allows a transfer that
#: has begun — so this fails if the establish phase stops being separately bounded and
#: falls back on the transfer timeout, which is the defect it was written for. It is
#: deliberately NOT derived from the node's own constant: a test whose bar is the thing it
#: is testing passes by construction. A node that bounded the phase at twenty seconds would
#: pass this and still be badly tuned; how generous the bound should be is argued in prose
#: in node.py, where the trade-off (a stop nobody promised you time for, against killing a
#: healthy job that no automatic retry will ever rescue) can actually be weighed.
ESTABLISH_BOUND_SECONDS = 14.0

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

_THE_RECEIPT = (
    'Nothing obliges a stopped step to write anything. pipelines/external_finalize.py salvages only '
    '"Publish what a FAILED or cancelled step managed to write. Best-effort about OBJECTS.", and it '
    'answers an absent marker with a reported finding rather than a refusal: "No completion marker was '
    'written, so the step gave no account of itself and nothing it produced could be salvaged." A marker '
    'is REQUIRED only after a reported success, so on this path the contract permits silence and this '
    'expectation may never be written down as a rule. What it is worth is measurable and large: the '
    'marker is the only inventory salvage can publish from, and its error and exit_code are the sentence '
    'a human reads about the run. external/contract.py describes the shape without demanding it — '
    '"A cancelled run still writes a marker: partial logs and partial outputs are exactly what someone '
    'will want to look at afterwards."'
)

_A_WAIT_NOTHING_CAN_INTERRUPT = (
    'Nothing in the platform says a word about how a workload connects, so every part of this is ours: '
    'that DNS, the TCP connect and the TLS handshake are bounded separately from a transfer, and that '
    'the bound is small. It is reference quality with a measured reason rather than a preference — a '
    'stop arriving during those three phases cannot be acted on at all (Python\'s ssl detaches the '
    'plain socket while wrapping it, so shutting it down raises "Bad file descriptor" and the handshake '
    'runs to its timeout), which makes this the only stretch of the stop path where the length of a '
    'timeout IS the stop latency. It is also the only test here that exercises TLS at all, and '
    'production is TLS-only.'
)

_NOTICING_IN_TIME = (
    'Nothing measures how long a step takes to go, and nothing ever will: the agent asks docker to stop '
    'the container and its wait loop simply watches. So a step that spends the whole nominal grace parked '
    'in a socket call is perfectly conformant — and useless, because no interval on any stop path is '
    'guaranteed and a stop can arrive with no usable warning at all. This repository tells node authors '
    'to assert the process exited "within a few seconds" for exactly that reason (docs/CONFORMANCE.md, '
    '"Testing the stop path"), so holding its own reference file to the same bar is reference quality '
    'with a measured price: this file took 12.2s to notice a stop while its handler closed the wrong '
    'object, and 0.2s once it shut the socket down instead.'
)


@conforms_today
@reference_quality(_COOPERATIVE_SHUTDOWN)
def test_a_step_stopped_during_a_download_does_not_claim_it_succeeded(make_job, sample_input):
    """Setup:    the store holds the first input's response open for twelve seconds.
    Action:   wait until the request has arrived, then stop the container with the
              agent's own thirty-second grace.
    Validate: the container stops well inside the grace, and whatever marker it leaves
              does not say ``succeeded``.

    The prohibited outcome, and the only one asserted here, is a receipt claiming the work
    was finished inside a launch the orchestrator has recorded as cancelled. Every other
    ending passes, including writing no marker at all — the test that requires one is
    ``test_a_stopped_step_leaves_a_receipt_and_says_it_was_stopped``, and keeping the two
    apart is what stops this one from prescribing a remedy for something it merely forbids.

    This is what an earlier version of ``node.py`` did instead, and it is worth knowing
    because it is what a first version of any node does: SIGTERM was dropped, the download
    completed, the file was copied, ``result.json`` was written, and a marker saying
    ``succeeded`` went into a run everybody else had given up on.
    """
    job = make_job(inputs=[sample_input])
    job.endpoint.hooks.on_request.append(delay_when(matching('input', index=1), HELD_OPEN_SECONDS))

    container = job.start()
    assert job.endpoint.wait_for('input', timeout=60), 'the step never started reading'
    grace_used = container.stop(grace=docker.STOP_GRACE_SECONDS)
    result = container.collect()

    _assert_stopped_cleanly(job, result, grace_used)


@conforms_today
@reference_quality(_COOPERATIVE_SHUTDOWN)
def test_a_step_stopped_during_an_upload_inventories_what_it_left_behind(make_job):
    """Setup:    TWO inputs, with the store holding the SECOND upload open for twelve
              seconds — so the first output has genuinely landed and been acknowledged
              before anything is signalled.
    Action:   wait until the second upload has arrived, stop the container, then **wait
              for the store to finish everything it is still holding**.
    Validate: it stops inside the grace, does not claim success, and no object the store
              ends up holding is missing from the marker the step left.

    **The store is read only after it settles, and that is the whole design of this
    test.** The reading that matters is the one the COLLECTOR will take, and that is not
    the state of the store at the instant the container died. A body that has fully
    arrived is committed and answered afterwards; killing the client in between does not
    un-write the object, it only means nobody hears the acknowledgement. Snapshotting
    immediately would let a step write a marker naming the first object, exit, and leave
    the second landing a moment later — unnamed, therefore never published, which is
    exactly the outcome this test exists to catch. ``Endpoint.settle`` closes that window.

    **Two remedies, neither of them prescribed.** A step may drain what it has already
    started and inventory it, or it may make sure nothing it did not account for is left
    behind. Naming an object that turns out not to be there is safe on the platform's
    side: ``_salvage_what_the_step_produced`` is "best-effort about OBJECTS" and drops one
    it cannot verify, while an object nobody named is never looked at at all.

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
    assert job.endpoint.settle(timeout=HELD_OPEN_SECONDS + docker.STOP_GRACE_SECONDS), (
        'the store was still handling a request when this test gave up waiting for it, so the objects it '
        'holds are still changing and nothing read from it now is the state collection would see'
    )
    landed = [key for key in job.endpoint.keys_in_order() if key.startswith('outputs/')]
    if not landed:
        pytest.skip(
            'the step left no output object behind at all, so there is nothing an inventory could be '
            'missing — a step that abandons an unfinished transfer rather than accounting for it has '
            'answered this question the other legal way'
        )
    uploaded = job.endpoint.keys_in_order()
    assert contract.MARKER_FILENAME in uploaded, (
        f'the step left no marker at all, so the {len(landed)} object(s) it had already written are '
        f'abandoned: salvage publishes exactly what a marker names. The store holds {uploaded}'
    )
    inventoried = {obj['relpath'] for obj in job.marker()['objects']}
    assert set(landed) <= inventoried, (
        f'{sorted(set(landed) - inventoried)} are in the store and not in the marker the step left, so '
        f'salvage will publish none of them'
    )


@conforms_today
@reference_quality(_COOPERATIVE_SHUTDOWN)
def test_a_cancelled_step_stops_taking_on_new_work(make_job):
    """Cancellation has to change what the step does next, not just how it ends.

    Setup:    three inputs. The store holds the FIRST upload open for twelve seconds, so
              the step is mid-write with two inputs still unread when the signal lands.
    Action:   stop the container.
    Validate: the second and third inputs are never fetched.

    This is the test that separates "shuts down cleanly" from "ignores the request and
    happens to finish". An earlier version of ``node.py`` read and uploaded all three
    inputs after being told to stop — the signal changed nothing at all, and the only
    reason the run ended was that it ran out of work to do. Inputs are counted as DISTINCT
    objects rather than as requests, so a step that retried one of them would not
    accidentally satisfy this.
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


@conforms_today
@reference_quality(_COOPERATIVE_SHUTDOWN)
def test_a_step_that_cannot_be_stopped_costs_the_whole_grace_period(make_job, sample_input):
    """The price of ignoring SIGTERM, measured.

    Setup:    the store holds the first upload open for longer than the grace period, so
              the step cannot finish its way out of the situation.
    Action:   stop the container and time it.
    Validate: it returns well inside the thirty seconds.

    An earlier version of ``node.py`` took the full thirty and was then SIGKILLed. The
    exit code that follows a kill is 137, and this test deliberately does NOT assert
    anything about it: the run is recorded as cancelled regardless, because the agent asks
    whether cancellation was requested before it looks at any exit code. What is real is
    the stopwatch — half a minute of a runner slot, per cancelled job, spent waiting for a
    process that was never going to answer.

    The bar here is the nominal grace, which is the *platform's* number and the most this
    can ever cost. ``test_a_stop_is_noticed_without_waiting_for_the_transfer_it_landed_in``
    asks the sharper question with a number of our own choosing, and on the read path,
    where nothing but the step's own timeout was ever going to end the wait.
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


@conforms_today
@reference_quality(_THE_RECEIPT)
def test_a_stopped_step_leaves_a_receipt_and_says_it_was_stopped(make_job):
    """The thing this suite never asked for until now: a receipt at all.

    Setup:    three inputs, with the store holding the FIRST input's response open for
              twelve seconds, so the signal lands inside a transfer.
    Action:   stop the container as soon as the request has arrived.
    Validate: a completion marker is in the store, it parses, and its ``status`` is
              ``cancelled``.

    **Why this is not covered by "it does not claim it succeeded".** That assertion lets
    an absent marker through — deliberately, because the contract permits one — and the
    consequence was measured rather than supposed: a copy of ``node.py`` that writes no
    marker on the stop path passes that test, passes
    ``test_a_cancelled_step_stops_taking_on_new_work``, and passes
    ``test_a_step_that_cannot_be_stopped_costs_the_whole_grace_period``. Only the upload
    test notices, and only because in ITS scenario an object had already landed to be
    missing from the inventory; stopped before it produces anything, the step could write
    nothing at all and this file had nothing to say. Prompt exit was measured; the account
    of the run was not. The second is the one salvage reads — it publishes exactly what a
    marker names — so a run that leaves none abandons everything it had already uploaded
    and explains nothing to the operator.

    ``cancelled`` and not merely "something other than succeeded": ``failed`` would be a
    receipt saying the step broke, inside a launch nobody has any reason to investigate,
    and the next person to read it starts looking for a defect that was never there.
    """
    job = make_job(inputs=THREE_INPUTS)
    job.endpoint.hooks.on_request.append(delay_when(matching('input', index=1), HELD_OPEN_SECONDS))

    container = job.start()
    assert job.endpoint.wait_for('input', timeout=60), 'the step never started reading'
    container.stop(grace=docker.STOP_GRACE_SECONDS)
    result = container.collect()

    _assert_it_really_stopped(result)
    assert job.endpoint.settle(timeout=HELD_OPEN_SECONDS + docker.STOP_GRACE_SECONDS), (
        'the store was still handling a request when this test gave up waiting for it'
    )
    uploaded = job.endpoint.keys_in_order()
    assert contract.MARKER_FILENAME in uploaded, (
        f'the step was stopped and wrote no completion marker: the store holds {uploaded}. Nothing it '
        f'had produced can be salvaged (salvage publishes exactly what a marker names) and the run has '
        f'no account of itself beyond the exit code {result.exit_code}'
    )
    status = job.marker()['status']
    assert status == 'cancelled', (
        f'the step was asked to stop and its receipt says {status!r}. Nothing on the platform side reads '
        f'this word — the outcome comes from the orchestrator\'s own journal, and the sentence an '
        f'operator is shown quotes the exit_code and the error, never the status — so this is a '
        f'preference, and the preference is that a document nobody has to interpret should not need '
        f'interpreting: a run that was stopped says so'
    )


@conforms_today
@reference_quality(_THE_RECEIPT)
def test_the_receipt_of_a_stopped_step_carries_the_code_the_process_returned(make_job):
    """One run, one story about how it ended.

    Setup:    as above — three inputs, the first response held open.
    Action:   stop the container.
    Validate: the marker's ``exit_code`` is the code the process really returned, and
              that code is 20.

    **The agreement is the point, and the number is what makes it useful.** A marker that
    says one thing while the process says another leaves two contradictory accounts of a
    single run, and the platform reads both — the exit code through the agent, the marker
    through ``_step_account``. As for the number: nothing requires 20 here, because the
    agent asks whether cancellation was requested before it looks at any exit code. Read
    that line the other way round, though, and 20 is the whole of the distinction in the
    case where nobody asked — ``if context.cancel_requested.is_set() or exit_code ==
    EXIT_CANCELLED`` is what separates a run recorded as stopped from one recorded as
    broken when the platform has no other way to tell.
    """
    job = make_job(inputs=THREE_INPUTS)
    job.endpoint.hooks.on_request.append(delay_when(matching('input', index=1), HELD_OPEN_SECONDS))

    container = job.start()
    assert job.endpoint.wait_for('input', timeout=60), 'the step never started reading'
    container.stop(grace=docker.STOP_GRACE_SECONDS)
    result = container.collect()

    _assert_it_really_stopped(result)
    assert job.endpoint.settle(timeout=HELD_OPEN_SECONDS + docker.STOP_GRACE_SECONDS)
    if contract.MARKER_FILENAME not in job.endpoint.keys_in_order():
        pytest.skip(
            'the step wrote no marker, so there is no second account of this run to disagree with the '
            'first — which is a legal ending, and the test above is the one that is about it'
        )
    marker = job.marker()
    assert marker.get('exit_code') == result.exit_code, (
        f'the receipt says the step exited {marker.get("exit_code")!r} and the process exited '
        f'{result.exit_code!r} — one run, two accounts, and the platform reads both'
    )
    assert result.exit_code == contract.EXIT_CANCELLED, (
        f'the step was stopped and exited {result.exit_code}, not {contract.EXIT_CANCELLED}. Nothing '
        f'refuses that, but it is the only signal that says "stopped" rather than "broken" when the '
        f'platform was not the party that asked'
    )


@conforms_today
@reference_quality(_THE_RECEIPT)
def test_a_stopped_step_claims_no_object_the_store_never_received(make_job):
    """The inventory has to be true in both directions.

    Setup:    two inputs, with the store holding the SECOND upload open for twelve
              seconds, so one object has certainly landed and one is in flight.
    Action:   stop the container, then wait for the store to settle.
    Validate: every relpath the marker inventories names an object the store really
              holds.

    The sibling test asserts the other direction — that nothing which landed is missing
    from the marker — and neither implies the other. This one is about a receipt that
    over-claims: an inventory naming an object nobody can find. **The platform survives
    it**: salvage is "best-effort about OBJECTS" and drops one it cannot verify, keeping
    the rest, so this is not a rule and no fix is prescribed. What it costs is the trust
    a reader puts in the document — an inventory that has to be re-verified before it can
    be believed is not an inventory, and this file is the one people copy.

    Two premises, and both are ordinary legal outcomes rather than assertions: a step may
    leave no marker, and a step may account for nothing at all (having abandoned rather
    than recorded what it had begun). Either way there is no claim to be false, and the
    test says which happened instead of manufacturing a pass.
    """
    job = make_job(inputs=THREE_INPUTS[:2])
    job.endpoint.hooks.on_request.append(delay_when(matching('upload', index=2), HELD_OPEN_SECONDS))

    container = job.start()
    assert _wait_for_upload_number(job, 2), 'the step never reached its second upload'
    container.stop(grace=docker.STOP_GRACE_SECONDS)
    result = container.collect()

    _assert_it_really_stopped(result)
    assert job.endpoint.settle(timeout=HELD_OPEN_SECONDS + docker.STOP_GRACE_SECONDS), (
        'the store was still handling a request when this test gave up waiting for it, so the objects it '
        'holds are still changing and nothing read from it now is the state collection would see'
    )
    if contract.MARKER_FILENAME not in job.endpoint.keys_in_order():
        pytest.skip('the step left no marker, so it claimed nothing and there is nothing to hold to')
    inventoried = [obj['relpath'] for obj in job.marker()['objects']]
    if not inventoried:
        pytest.skip(
            'the step inventoried nothing — the other legal answer to being stopped mid-write, and one '
            'that cannot over-claim'
        )
    held = set(job.endpoint.keys_in_order())
    missing = [relpath for relpath in inventoried if relpath not in held]
    assert not missing, (
        f'the receipt inventories {missing}, which the store never received. Salvage will try each of '
        f'them, fail to verify it and drop it, so the claim costs nothing but is still false: the store '
        f'holds {sorted(held)}'
    )


@conforms_today
@reference_quality(_NOTICING_IN_TIME)
def test_a_stop_is_noticed_without_waiting_for_the_transfer_it_landed_in(make_job, sample_input):
    """A flag cannot be read by a process parked in a socket call.

    Setup:    the store accepts the first input's request and never answers it — held
              open past the whole nominal grace period.
    Action:   stop the container once that request has arrived, and time it.
    Validate: the container is gone within a few seconds.

    **This is the assertion that separates handling a stop from having a handler.**
    Installing one and then blocking in a read until it times out is not stopping; it is
    finishing, slowly, for a run nobody will collect. The distinction is invisible to
    every other test here, because a store that eventually answers lets a step that
    waited look exactly like a step that stopped — which is why this one holds the
    response open longer than the stop is nominally given.

    Measured against this file's previous version, where the handler set the flag and
    closed the response object: **12.2 seconds** on a response held for twelve, and the
    real bound was the 25-second socket timeout, since closing a response neither
    interrupts a blocked read nor exists at all while the store is still deciding whether
    to answer.
    """
    job = make_job(inputs=[sample_input])
    job.endpoint.hooks.on_request.append(delay_when(matching('input', index=1), HELD_PAST_THE_GRACE))

    container = job.start()
    assert job.endpoint.wait_for('input', timeout=60), 'the step never started reading'
    grace_used = container.stop(grace=docker.STOP_GRACE_SECONDS)
    result = container.collect()

    _assert_it_really_stopped(result)
    assert grace_used < PROMPTLY_SECONDS, (
        f'the step took {grace_used:.1f}s to go after being asked to stop, with a store that was never '
        f'going to answer it. Nothing refuses that — but nothing guarantees it those seconds either, so '
        f'a step that needs them is a step that writes nothing on a stop that gives it none. It exited '
        f'{result.exit_code}'
    )


@conforms_today
@reference_quality(_NOTICING_IN_TIME)
def test_a_stop_while_a_body_is_still_arriving_does_not_wait_for_the_rest(make_job):
    """The wait after the answer has begun, which is a different object from the one before.

    Setup:    the input's URL points at a listener that promises a large response,
              delivers enough of it that the step is certainly reading the body, and then
              goes silent for ever.
    Action:   let the step get past the headers and into the body, then stop the container.
    Validate: it is gone within a few seconds.

    **Why this is not the same test as the one above it.** A client waiting for a store to
    START answering and a client reading a body that has STOPPED arriving are two waits
    reached through two different objects, and a node can be interruptible in the first and
    not the second — which is exactly what this file shipped. ``http.client`` closes the
    connection object as soon as it has parsed the headers of a ``Connection: close``
    response, and urllib sets that header on every request, so a ledger that forgets a
    connection when it is closed is empty for the whole of the body. The fake store cannot
    produce this case: its hooks run while a request is still being authorised, before a
    byte of the response exists.
    """
    # The pin has to allow the bytes this listener sends, or the step refuses them before
    # it ever blocks and the stall never happens — measured, when a fourteen-byte pin met a
    # sixteen-megabyte preamble and the run failed on "larger than the job pinned" instead.
    # A synthetic pin of exactly what the response promises leaves the size check with
    # nothing to complain about, and the step waits for a body that stops arriving.
    with StallingEndpoint(a_body_that_stops(sent=STALLED_BODY_SENT, promised=STALLED_BODY_PROMISED)) as quiet:
        job = make_job(inputs=[
            InputSpec(relpath='data.csv', synthetic_size=STALLED_BODY_PROMISED,
                      extra={'get_url': quiet.url(docker.HOST_ALIAS)}),
        ])
        container = job.start()
        # Waits for EVIDENCE, not for a moment: the listener announces only once its own
        # sendall of sixteen megabytes has returned, which cannot happen until the step has
        # consumed most of them. Anything weaker — announcing on accept, then sleeping —
        # would let the signal land in the wait BEFORE the answer, and this test would
        # quietly become a copy of its sibling while still passing.
        assert quiet.wait_for_stall(timeout=60), 'the step never got into the body of its input'
        grace_used = container.stop(grace=docker.STOP_GRACE_SECONDS)
        result = container.collect()

    _assert_it_really_stopped(result)
    assert grace_used < PROMPTLY_SECONDS, (
        f'the step took {grace_used:.1f}s to go while it was reading a body that had stopped arriving. '
        f'The rest of it was never coming, so that is the whole of a read timeout spent on a run already '
        f'called off. It exited {result.exit_code}'
    )


@conforms_today
@reference_quality(_A_WAIT_NOTHING_CAN_INTERRUPT)
def test_a_stop_during_a_tls_handshake_is_bounded_even_though_it_cannot_be_cut(make_job):
    """The one wait on this path that no signal can shorten — so it has to be short.

    Setup:    the input's URL is **https**, pointing at a listener that accepts the
              connection and never negotiates anything.
    Action:   stop the container once that connection has been accepted.
    Validate: it is gone in a few seconds — bounded by the step's own connect budget
              rather than by the twenty-five seconds a transfer is allowed.

    **This is the only test here that does not claim promptness, and the difference is
    real.** DNS, the TCP connect and the TLS handshake happen inside one call that hands
    out no socket anybody else can reach: Python's ``ssl`` detaches the plain socket while
    it wraps it, so shutting that down raises ``Bad file descriptor`` and the handshake
    runs to its timeout regardless (measured). What cannot be interrupted has to be
    bounded, and what is asserted here is that it IS bounded — separately, and tightly,
    rather than inheriting the timeout meant for moving bytes.

    **It is also the only test in this suite that exercises TLS**, and that matters more
    than the stall it measures: every presigned URL in production is https, this harness's
    store is plain http, and a stop mechanism that had been proved only over http would be
    a claim about the wrong protocol. No certificate is involved — a handshake stalls
    before any certificate is offered, so refusing to speak at all is enough.
    """
    with StallingEndpoint() as silent:
        job = make_job(inputs=[
            InputSpec(relpath='data.csv', data=b'never arrives\n',
                      extra={'get_url': silent.url(docker.HOST_ALIAS, scheme='https')}),
        ])
        container = job.start()
        # Announced only once the listener has READ something, which for a TLS client is
        # its hello: proof that negotiation has begun. Signalling on accept alone would
        # leave the raw socket still owned and interruptible on an unlucky schedule, and a
        # node with an unbounded handshake could then pass this for the wrong reason.
        assert silent.wait_for_stall(timeout=60), 'the step never began negotiating'
        grace_used = container.stop(grace=docker.STOP_GRACE_SECONDS)
        result = container.collect()

    _assert_it_really_stopped(result)
    assert grace_used < ESTABLISH_BOUND_SECONDS, (
        f'the step took {grace_used:.1f}s to go while a TLS handshake was hanging. Nothing can cut that '
        f'handshake, so the only thing standing between a stop and the end of the run is how long the '
        f'step is prepared to wait for a connection — and it should be prepared to wait for a connection '
        f'far less long than it waits for bytes. It exited {result.exit_code}'
    )


@conforms_today
@reference_quality(_THE_RECEIPT)
def test_a_stop_while_the_receipt_is_in_flight_leaves_exactly_one_receipt(make_job, sample_input):
    """One run, one document — the rule that replaced two failed attempts at correcting one.

    Setup:    one input, so the third upload is the completion marker itself, and the store
              holds that upload open for twelve seconds.
    Action:   stop the container while its own receipt is in flight.
    Validate: the store received the marker's name exactly ONCE, and the code the process
              returned agrees with what that one document says.

    **Why not "the receipt must say cancelled".** Two earlier versions of this file tried
    to make it say that, and both created a second write to the same key: the first
    abandoned the in-flight upload and wrote a cancellation, the second let it finish and
    wrote a correction afterwards. Both lose the same way — a write that fails ambiguously
    may still be accepted, so the correction can commit first and the thing it corrected
    can land on top of it. Sequential calls are not sequential commits, and this harness
    reproduces the inversion in both directions.

    **And the state they were trying to prevent turns out not to be one the platform can
    act on.** A `succeeded` receipt beside a launch the orchestrator recorded as cancelled
    changes nothing there: the outcome is decided from the orchestrator's own journal
    (`_TERMINAL_LAUNCH_OUTCOMES`, keyed on the launch state), a marker can only ever VETO a
    success and never claim one, a stopped attempt's objects are salvaged as diagnostics
    with no output port rather than published, the cascade sits behind a compare-and-set
    that a cancellation loses, and the sentence an operator reads quotes the marker's
    `exit_code` and `error` — never its `status`. A step that finished its work and was
    interrupted while REPORTING it has genuinely succeeded; the stop arrived late.

    So what a stopped step owes here is not a particular word. It is that whatever it says,
    it says once, and its exit code says the same thing.
    """
    job = make_job(inputs=[sample_input])
    job.endpoint.hooks.on_request.append(delay_when(matching('upload', index=3), HELD_OPEN_SECONDS))

    container = job.start()
    assert _wait_for_upload_number(job, 3), 'the step never reached its third upload — the marker'
    container.stop(grace=docker.STOP_GRACE_SECONDS)
    result = container.collect()

    _assert_it_really_stopped(result)
    assert job.endpoint.settle(timeout=HELD_OPEN_SECONDS + docker.STOP_GRACE_SECONDS), (
        'the store was still handling a request when this test gave up waiting for it'
    )
    written = [key for key in job.endpoint.keys_in_order() if key == contract.MARKER_FILENAME]
    assert len(written) <= 1, (
        f'the step wrote {len(written)} completion markers for one run. Whichever it meant to stand, the '
        f'store decides between overlapping writes and neither the step nor S3 defines which wins — so '
        f'the run has two accounts of itself and no way to say which is current'
    )
    if not written:
        pytest.skip('the step wrote no receipt at all, which is legal here and is another test\'s subject')
    marker = job.marker()
    assert marker.get('exit_code') == result.exit_code, (
        f'the one receipt says the step exited {marker.get("exit_code")!r} and it returned '
        f'{result.exit_code!r}. One document, and it disagrees with the process that wrote it'
    )
    assert (marker['status'] == 'cancelled') == (result.exit_code == contract.EXIT_CANCELLED), (
        f'the receipt says {marker["status"]!r} beside exit {result.exit_code} — the status and the code '
        f'are the same claim told twice, so they cannot differ'
    )


@conforms_today
@reference_quality(_THE_RECEIPT)
def test_the_receipt_is_given_a_deadline_of_its_own(make_job, sample_input):
    """The one transfer this step will not abandon is the one that needs a clock.

    Setup:    the store answers the receipt's upload by DRIBBLING it out — one byte at a
              time over forty seconds, so it is never idle for long and a socket timeout
              never fires.
    Action:   run the job to its end and time the container.
    Validate: the step gives up inside the grace a stop is nominally given, rather than
              waiting out the whole answer.

    A socket timeout measures SILENCE, not duration: a peer that sends a byte every second
    is never quiet, so an inactivity timeout of ten seconds does not bound a forty-second
    answer at all. That is tolerable for a transfer a stop can abandon, and not tolerable
    for the receipt, which this step has promised not to abandon — the promise is exactly
    what makes an upper bound necessary, because the kill behind a stop arrives on its own
    schedule and a step still politely waiting on a store when it lands has written nothing
    and said nothing.

    **The bar here is a judgement and not a promise**, because there is no promise to be
    had: every stop path today passes the executor's own thirty-second default, but nothing
    tells the workload that, and nothing tells it how much of it is left. Thirty is
    therefore the most a polite stop has ever been observed to give, not a guarantee — a
    fence gives none at all, and a grace can be spent before the container is even
    signalled. So this threshold says only that a self-imposed save deadline must be short
    enough to be worth having; it cannot say the deadline will be honoured.
    """
    job = make_job(inputs=[sample_input])
    job.endpoint.hooks.on_request.append(drip_when(matching('upload', index=3), 40.0))

    began = time.monotonic()
    result = job.run(timeout=120)
    elapsed = time.monotonic() - began

    assert result.exit_code is not None, 'the container had not finished'
    assert elapsed < docker.STOP_GRACE_SECONDS - 2, (
        f'the step spent {elapsed:.1f}s on a receipt whose answer was dribbled out over forty. Nothing '
        f'was ever idle, so no socket timeout could end it — only an elapsed deadline can, and without '
        f'one this transfer outlives the whole grace a stop is given. It exited {result.exit_code}'
    )


@conforms_today
@reference_quality(_A_WAIT_NOTHING_CAN_INTERRUPT)
def test_one_connect_deadline_covers_every_address_a_name_has(make_job, image):
    """A budget spent once per address is not a budget.

    Setup:    the input's host is put in the container's ``/etc/hosts`` three times, on
              three addresses whose packets are dropped in silence.
    Action:   run the job to its end and time the container.
    Validate: the whole thing is over inside one establish bound, not three.

    ``socket.create_connection`` takes a timeout and applies it **separately to each
    address it was given**, so a node that passes it a number is not saying what it thinks
    it is saying: a host with four addresses waits four times as long. This is the shape
    that hides, because the ordinary case — one address — behaves exactly as intended.

    No signal is sent here. The subject is the bound itself, which matters as much to a
    step that is stopped as to one that is merely stuck: the receipt a stopped run writes
    is a connection like any other, made at the moment there is least time left to make one.
    """
    dead = tuple(('unanswered.invalid', address) for address in docker.SILENTLY_DROPPED)
    # The premise is not "one address hangs" — it is "this name costs a MULTIPLE of the
    # timeout under the implementation being guarded against", and the check has to prove
    # the property it needs rather than something adjacent. Probing one address for 2s and
    # accepting 1.5s did not: an address that fails at the kernel's own ~3s ARP give-up
    # passes that, and three of those cost ~9s under the broken implementation — inside
    # this test's own threshold, so the guard would have gone green against the very thing
    # it exists to catch.
    probe_timeout = 4.0
    spent = docker.seconds_spent_connecting(image, 'unanswered.invalid', timeout=probe_timeout,
                                            extra_hosts=dead)
    if spent < probe_timeout * len(dead) * 0.9:
        pytest.skip(
            f'reaching a name on {len(dead)} dropped addresses costs {spent:.1f}s on this machine, not '
            f'the {probe_timeout * len(dead):.0f}s a timeout spent once per address would cost, so the '
            f'network here answers rather than drops and the multiplication this test measures cannot '
            f'be produced at all'
        )

    job = make_job(inputs=[
        InputSpec(relpath='data.csv', data=b'never arrives\n',
                  extra={'get_url': 'http://unanswered.invalid:9/held-open'}),
    ])
    began = time.monotonic()
    result = job.run(timeout=120, extra_hosts=dead)
    elapsed = time.monotonic() - began

    assert result.exit_code is not None, 'the container had not finished'
    assert elapsed < ESTABLISH_BOUND_SECONDS + 6.0, (
        f'the step spent {elapsed:.1f}s failing to reach a host with {len(dead)} addresses. One deadline '
        f'should cover the lot; spent per address it is multiplied by however many a name happens to '
        f'have, which is not a number this node chose or can see. It exited {result.exit_code}'
    )


@conforms_today
@reference_quality(_A_WAIT_NOTHING_CAN_INTERRUPT)
def test_a_name_lookup_that_hangs_is_bounded_too(make_job, image):
    """The phase with no socket at all — and the one a receipt cannot survive.

    Setup:    a resolver of our own that receives every query and answers none, and a
              container told to be patient with it. An ordinary lookup against it takes
              forty seconds. The store is reached through ``/etc/hosts`` and is
              unaffected; only the input's host needs resolving.
    Action:   run the job to its end and time the container.
    Validate: it is over inside one establish bound.

    ``socket.getaddrinfo`` takes no timeout at all — it is a blocking call into the system
    resolver, whose patience comes from ``/etc/resolv.conf`` and is measured in tens of
    seconds. It owns no socket, so a stop cannot be acted on inside it, and the completion
    receipt — which by then must not be abandoned — cannot be written either.

    **The resolver has to swallow the query rather than be unreachable**, and the first
    version of this test got that wrong: an address in TEST-NET-3 fails in 0.4 s because
    nothing is routed to it, so the test passed against a node with no bound at all.
    """
    job = make_job(inputs=[
        InputSpec(relpath='data.csv', data=b'never arrives\n',
                  extra={'get_url': 'http://nowhere.invalid:9/held-open'}),
    ])

    with docker.silent_resolver(image) as resolver:
        began = time.monotonic()
        result = job.run(timeout=180, dns=(resolver,), dns_options=('timeout:10', 'attempts:2'))
        elapsed = time.monotonic() - began

    assert result.exit_code is not None, 'the container had not finished'
    assert elapsed < ESTABLISH_BOUND_SECONDS + 6.0, (
        f'the step spent {elapsed:.1f}s on a name lookup that was never going to answer. That is the '
        f"resolver's patience, not this step's: nothing in the standard library bounds a lookup, so a "
        f'node that wants a number here has to impose one. It exited {result.exit_code}'
    )


@conforms_today
@reference_quality(_A_WAIT_NOTHING_CAN_INTERRUPT)
def test_a_proxy_does_not_get_the_connect_deadline_twice(make_job):
    """Two waits inside one connect, and only one deadline to spend on them.

    Setup:    the container is given an HTTPS proxy that grants the tunnel **slowly** —
              seven seconds — and then never speaks again, so the TLS handshake behind it
              stalls. The input is an https URL, which is what makes urllib tunnel.
    Action:   run the job to its end and time the container.
    Validate: the whole establish phase is over inside one bound, not two.

    A proxied connect is where "one deadline" is easiest to believe and least likely to be
    true: the socket's timeout is set once, on the way out of the TCP connect, and the
    handshake after the tunnel re-uses that same value — so a proxy that eats most of the
    budget leaves the handshake nearly all of it again. Nothing about the ordinary,
    proxy-less path shows this, which is exactly why it is worth a test: it is invisible
    until somebody's ``HTTPS_PROXY`` is set, and invisible again afterwards because it only
    costs time.
    """
    with StallingEndpoint(TUNNEL_GRANTED, answer_after=7.0) as slow_proxy:
        job = make_job(inputs=[
            InputSpec(relpath='data.csv', data=b'never arrives\n',
                      extra={'get_url': 'https://unreachable.example/held-open'}),
        ])

        began = time.monotonic()
        result = job.run(timeout=120, env={'HTTPS_PROXY': slow_proxy.url(docker.HOST_ALIAS)})
        elapsed = time.monotonic() - began

    assert result.exit_code is not None, 'the container had not finished'
    assert elapsed < ESTABLISH_BOUND_SECONDS, (
        f'the step spent {elapsed:.1f}s getting a connection through a proxy that took seven of them to '
        f'answer. One deadline covers reaching the peer AND negotiating with it; read once and applied '
        f'twice it is doubled by anybody who happens to sit in between. It exited {result.exit_code}'
    )


def _wait_for_upload_number(job, wanted: int, timeout: float = 60.0) -> bool:
    """Block until the store has RECEIVED its ``wanted``-th upload request.

    ``Endpoint.wait_for`` answers "has any upload arrived yet?" and stays answered, which
    is not the question when a test needs to signal the container during a specific one.
    The counter is stamped once the body has been read and before any hook runs — so this
    returns while the upload is still in flight, with the bytes already at the store and
    the answer not yet sent, which is the moment worth interrupting.
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
