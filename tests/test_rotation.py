"""Credentials EXPIRE under a running step, and the step has to notice.

An envelope is short-lived by design: ``LSPO_RUNNER_CREDS_TTL_S``, fifteen minutes by
default, clamped to whatever remains of the job's runtime budget
(``runners/credentials.py``). The agent asks for a new one before the old expires and
replaces ``creds.json`` in place — a new file beside it, then ``os.replace``, which is
why the DIRECTORY is what gets mounted. ``agent/creds.py`` states the intent plainly:

    **Refresh before expiry, not after.** A long job outlives its credentials. The agent
    asks for a new envelope once the remaining validity drops under a margin, so the
    workload never has to handle an expired file.

The file is kept fresh. A step that read it once into a variable at startup is not
holding the file — it is holding a fifteen-minute-old copy of it, and no amount of
refreshing on the agent's side reaches it.

**What these tests do NOT require, and used to.** Issuing a fresh envelope does not
revoke the one already in the container's hands: a presigned URL is a signature over a
deadline and nothing takes it back. So "re-read the credentials file before every
transfer" is not the rule, an expiry-aware step that re-reads when it needs to is
correct, and a 403 that the step recovers from is not a failure. The earlier version of
this file asserted all three of those things and was wrong about all three; see
``conformance/fakes3.py`` for how the credential model was corrected.
"""

from __future__ import annotations

from conformance import contract
from conformance.fakes3 import delay_when, expire_and_refresh_when, matching, refresh_when
from conformance.job import InputSpec
from conformance.markers import expected_red_until_fixed, traces_to

#: A credential lifetime the harness can wait out. Its clock starts when the CONTAINER
#: launches (``Job.start`` re-mints for a job with a TTL), not when the job was built, so
#: the whole budget is available to the step rather than being spent on ``docker run``.
#: Production numbers are fifteen minutes and hours; the mechanism is identical and only
#: the arithmetic is not.
SHORT_TTL_SECONDS = 6.0

#: How long the store holds one response open — comfortably past the expiry above, so
#: that everything the step does afterwards is done with a dead copy of the envelope.
HELD_OPEN_SECONDS = 12.0


@expected_red_until_fixed
@traces_to(
    'runners/credentials.py: "The envelope expires in LSPO_RUNNER_CREDS_TTL_S (default 15 minutes), '
    'clamped to whatever remains of the job\'s own runtime budget… A long job calls POST /jobs/<id>/sign '
    'for a fresh set". agent/creds.py: "Refresh before expiry, not after… so the workload never has to '
    'handle an expired file", with the fresh envelope published by atomic replacement into the mounted '
    'directory.'
)
def test_a_step_that_outlives_its_envelope_reads_the_fresh_one(make_job, sample_input):
    """The ordinary life of a long job, compressed into a dozen seconds.

    Setup:    an envelope good for six seconds, counted from the container's launch. The
              store holds the input's response open for twelve, and publishes a fresh
              ``creds.json`` — with a long life — the moment that response completes. The
              step's own copy is by then expired; the file on disk is not.
    Action:   run.
    Validate: the step uploads everything and exits 0, and the objects that land are
              signed with the FRESH generation — which is only possible if it re-read the
              file.

    Today the step loads the envelope into a local variable in ``main()`` and every later
    transfer uses that copy, so this is a hard ceiling on its own runtime: past one
    credential lifetime it cannot upload its outputs, and it cannot upload the failure
    marker that would explain why either. Fifteen minutes is not an edge case for a batch
    step; it is Tuesday.
    """
    job = make_job(inputs=[sample_input], creds_ttl_s=SHORT_TTL_SECONDS)
    job.endpoint.hooks.on_request.append(delay_when(matching('input', index=1), HELD_OPEN_SECONDS))
    job.endpoint.hooks.on_responded.append(refresh_when(matching('input', index=1)))

    result = job.run(timeout=120)

    _assert_the_step_had_its_whole_credential_lifetime(job)
    assert result.exit_code == 0, f'the step did not survive its own credential expiry:\n{result.output}'
    uploaded = job.endpoint.keys_in_order()
    assert contract.MARKER_FILENAME in uploaded, f'nothing was completed; only {uploaded} landed'
    assert 'outputs/data.csv' in uploaded, f'the output never landed; got {uploaded}'

    fresh = job.endpoint.token
    for relpath in ('outputs/data.csv', contract.MARKER_FILENAME):
        upload = job.endpoint.uploaded(relpath)
        assert upload.token == fresh, (
            f'{relpath!r} was uploaded with credential {upload.token!r}, not the live {fresh!r} — '
            f'the step is still using the envelope it read at startup'
        )


@expected_red_until_fixed
@traces_to(
    'runners/credentials.py: the envelope carries "the moment all of that stops working" (expires_at) '
    'and one presigned POST policy; agent/creds.py replaces creds.json atomically in the mounted '
    'directory before expiry. A refusal is therefore recoverable in place — the live envelope is already '
    'on disk — and a step that treats it as terminal throws away work that would have completed.'
)
def test_a_transfer_refused_on_an_expired_credential_is_repeated_after_re_reading(make_job, sample_input):
    """The store took the body and then said no.

    Setup:    one input. On the first upload the store reads the whole body, kills the
              credential that was used, publishes a fresh ``creds.json``, and only then
              answers 403.
    Action:   run.
    Validate: the step ends up with every object in place and exits 0. A 403 along the
              way is fine — recovering from one is the whole point.

    This is the case that a "check the expiry before you start" fix does not cover: the
    envelope was valid when the request left, the bytes were transferred, and the refusal
    arrives afterwards. The only correct response is to re-read the credentials file —
    which by then holds a live envelope — and repeat the upload, which is safe because
    the object is addressed by its own relpath.

    Today the step reports the 403 as a PERMANENT failure, and then cannot write its
    failure marker either, because that upload is refused for the same reason. The run
    leaves nothing behind at all.
    """
    job = make_job(inputs=[sample_input])
    job.endpoint.hooks.on_accepted.append(expire_and_refresh_when(matching('upload', index=1)))

    result = job.run()

    assert result.exit_code == 0, f'the step gave up on a mid-transfer expiry:\n{result.output}'
    uploaded = job.endpoint.keys_in_order()
    assert contract.MARKER_FILENAME in uploaded, f'nothing was completed; only {uploaded} landed'
    assert 'outputs/data.csv' in uploaded, f'the refused object was never re-sent; got {uploaded}'
    assert job.endpoint.uploaded('outputs/data.csv').token == job.endpoint.token, (
        'the object landed under the dead credential, which this store would not have accepted'
    )


@expected_red_until_fixed
@traces_to(
    'runners/credentials.py: every input arrives as its own presigned GET inside ONE envelope, and the '
    'envelope expires as a whole ("the moment all of that stops working"). agent/creds.py refreshes the '
    'file before that moment, so a step that re-reads it keeps working through an expiry; one that does '
    'not stops at whatever it reaches next.'
)
def test_the_work_remaining_after_an_expiry_is_still_done(make_job):
    """An expiry part-way through a batch must cost the batch nothing.

    Setup:    three inputs and a six-second envelope whose clock starts when the
              container launches. The store holds the FIRST read open past the expiry
              and publishes a fresh ``creds.json`` when it completes.
    Action:   run.
    Validate: all three inputs are fetched and all three copies land — counted as
              DISTINCT objects, not as requests, so three retries of one input would not
              satisfy it.

    The single-input test above proves the step can recover; this one proves the recovery
    is not a one-off. It is also the honest measure of what the defect costs: not "a
    transfer failed" but "everything after the fifteen-minute mark did not happen".
    """
    job = make_job(
        inputs=[
            InputSpec(relpath='one.csv', data=b'1\n'),
            InputSpec(relpath='two.csv', data=b'2\n'),
            InputSpec(relpath='three.csv', data=b'3\n'),
        ],
        creds_ttl_s=SHORT_TTL_SECONDS,
    )
    job.endpoint.hooks.on_request.append(delay_when(matching('input', index=1), HELD_OPEN_SECONDS))
    job.endpoint.hooks.on_responded.append(refresh_when(matching('input', index=1)))

    result = job.run(timeout=120)

    _assert_the_step_had_its_whole_credential_lifetime(job)
    fetched = job.endpoint.names_of('input')
    assert fetched == ['input/one.csv', 'input/two.csv', 'input/three.csv'], (
        f'the step stopped after its envelope expired; it fetched {fetched}'
    )
    landed = [key for key in job.endpoint.keys_in_order() if key.startswith('outputs/')]
    assert sorted(landed) == ['outputs/one.csv', 'outputs/three.csv', 'outputs/two.csv'], (
        f'only {sorted(landed)} were copied'
    )
    assert result.exit_code == 0, f'the step did not survive its own credential expiry:\n{result.output}'
    assert contract.MARKER_FILENAME in job.endpoint.keys_in_order()


def _assert_the_step_had_its_whole_credential_lifetime(job) -> None:
    """Fail with the right story if the CONTAINER, not the node, ran out of credential.

    The lifetime here is six seconds and it starts when ``docker run`` returns, so the
    step has to make its first request inside that. It does, comfortably — measured at
    roughly a quarter of a second — but a badly overloaded machine could break that, and
    the resulting 403 on ``invocation.json`` would look exactly like a node defect. This
    says which it was instead of letting the next reader guess.
    """
    early = [rejection for rejection in job.endpoint.rejections if rejection.kind == 'manifest']
    assert not early, (
        f'the container took longer than the {SHORT_TTL_SECONDS}s credential lifetime to make its first '
        f'request, so its manifest fetch was refused before the step could do anything: {early}. '
        f'This run says nothing about the node — re-run it, and raise SHORT_TTL_SECONDS if it recurs'
    )
