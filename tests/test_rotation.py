"""Credentials rotate under a running step, and the step has to notice.

An envelope is short-lived by design — fifteen minutes by default, clamped to whatever
remains of the job's runtime budget. A job that runs longer than that gets a NEW envelope
written over the old one while it is running, and the presigned URLs it was holding stop
working. That is not an edge case: it is the normal life of any step that takes longer
than a quarter of an hour.

So the rule is: **re-read the credentials file immediately before every transfer, and on
a refusal re-read and try once more.** A step that loads the envelope once at startup and
keeps it in a variable is a step with a fifteen-minute ceiling on its own runtime.

These tests model rotation with **generations, not a clock**. The store issues generation
``A``; at a point in the traffic the test chooses, ``creds.json`` is atomically replaced
with generation ``B`` and ``A`` stops being accepted. Nothing waits, nothing is skewed,
and the answer to "did the step reload?" is a fact rather than a race.
"""

from __future__ import annotations

from conformance import contract
from conformance.fakes3 import matching, rotate_when
from conformance.job import InputSpec
from conformance.markers import expected_red_until_fixed

THREE_INPUTS = [
    InputSpec(relpath='one.csv', data=b'1\n'),
    InputSpec(relpath='two.csv', data=b'2\n'),
    InputSpec(relpath='three.csv', data=b'3\n'),
]


@expected_red_until_fixed
def test_credentials_that_rotate_before_the_first_upload_are_picked_up(make_job):
    """Rotate the moment the last input is read; the uploads must still land.

    Setup:    three pinned inputs. The store is told to replace ``creds.json`` with a
              new generation the instant the LAST input finishes downloading, and to
              refuse the previous generation from then on.
    Action:   run the step. Its reads all succeed; every write happens after the swap.
    Validate: all five objects are uploaded — three copies, ``result.json`` and the
              completion marker — and the step exits 0.

    This is the ordinary life of a long job compressed into a second. Today the step
    reads the envelope once at startup, keeps the upload policy in memory, and the first
    write after the rotation is refused with 403 — which it reports as a PERMANENT
    failure, so the orchestrator never even retries it.
    """
    job = make_job(inputs=THREE_INPUTS)
    job.endpoint.hooks.on_responded.append(rotate_when(matching('input', index=len(THREE_INPUTS))))

    result = job.run()

    assert job.endpoint.count_of('input') >= len(THREE_INPUTS), 'the step never read its inputs'
    assert result.exit_code == 0, f'the step did not survive a credential rotation:\n{result.output}'
    assert contract.MARKER_FILENAME in job.endpoint.keys_in_order()
    assert not [r for r in job.endpoint.rejections if r.status == 403], (
        f'a request was refused with 403 after the rotation: {job.endpoint.rejections}'
    )


@expected_red_until_fixed
def test_credentials_are_reloaded_before_every_transfer_not_merely_once(make_job):
    """Rotate twice — once before the writes, once between them.

    Setup:    three inputs. ``creds.json`` is replaced when the last input is read, AND
              again the moment the first upload is accepted.
    Action:   run.
    Validate: every object still lands, in the usual order, and the step exits 0.

    The second rotation is what makes this test different from the one above. A step
    that reloads its credentials ONCE, after processing, passes that test and fails this
    one — and "reloads once" is exactly the shape of fix somebody writes when the only
    evidence is a single 403 on the first upload. Reloading before EVERY transfer is the
    rule, because the file can be replaced at any moment, including between two writes.
    """
    job = make_job(inputs=THREE_INPUTS)
    job.endpoint.hooks.on_responded.append(rotate_when(matching('input', index=len(THREE_INPUTS))))
    job.endpoint.hooks.on_responded.append(rotate_when(matching('upload', index=1)))

    result = job.run()

    assert result.exit_code == 0, f'the step did not survive the second rotation:\n{result.output}'
    uploaded = job.endpoint.keys_in_order()
    assert len(uploaded) == len(THREE_INPUTS) + 2, f'only {uploaded} landed'
    assert uploaded[-1] == contract.MARKER_FILENAME


@expected_red_until_fixed
def test_credentials_that_rotate_between_two_input_reads_are_picked_up(make_job):
    """The same rule applies to reads, not only to writes.

    Setup:    three inputs; ``creds.json`` is replaced after the FIRST one is served.
    Action:   run.
    Validate: the step reads all three and finishes.

    Reads are where a long job spends most of its time, so a rotation is more likely to
    land between two of them than anywhere else. A step holding a presigned GET it
    fetched fifteen minutes ago gets a 403 that has nothing to do with the object.
    """
    job = make_job(inputs=THREE_INPUTS)
    job.endpoint.hooks.on_responded.append(rotate_when(matching('input', index=1)))

    result = job.run()

    assert result.exit_code == 0, f'the step stopped at the first rotated input:\n{result.output}'
    assert job.endpoint.count_of('input') >= len(THREE_INPUTS), (
        f'only {job.endpoint.count_of("input")} of {len(THREE_INPUTS)} inputs were fetched'
    )
    assert contract.MARKER_FILENAME in job.endpoint.keys_in_order()


@expected_red_until_fixed
def test_a_credential_that_expires_mid_operation_is_retried(make_job, sample_input):
    """The store accepted the request and THEN the credential died.

    Setup:    one input. On the first upload the store reads the whole body, replaces
              ``creds.json`` with a new generation, and only then answers 403.
    Action:   run.
    Validate: the step re-reads its credentials, repeats the upload, and finishes with
              every object in place.

    This is the case a "check the expiry before you start" fix does not cover. The
    envelope was valid when the request left, the bytes were transferred, and the
    refusal arrives afterwards — which is precisely what an expiring upload policy looks
    like from inside the container. The only correct response is to reload and repeat,
    and re-uploading an object that is addressed by its own relpath is safe to repeat.
    """
    job = make_job(inputs=[sample_input])
    job.endpoint.hooks.on_accepted.append(rotate_when(matching('upload', index=1)))

    result = job.run()

    assert result.exit_code == 0, f'the step gave up on a mid-operation expiry:\n{result.output}'
    uploaded = job.endpoint.keys_in_order()
    assert contract.MARKER_FILENAME in uploaded, f'nothing was completed; only {uploaded} landed'
    assert 'outputs/data.csv' in uploaded, f'the refused object was never retried; got {uploaded}'
