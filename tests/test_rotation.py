"""Credentials EXPIRE under a running step, and this node cannot outlive one envelope.

An envelope is short-lived by design: ``LSPO_RUNNER_CREDS_TTL_S``, fifteen minutes by
default, clamped to whatever remains of the job's runtime budget
(``runners/credentials.py``). The agent asks for a new one before the old expires and
replaces ``creds.json`` in place — a new file beside it, then ``os.replace``, which is
why the DIRECTORY is what gets mounted rather than the file.

**Why these are reference-quality and not conformance, which is a change this round.**
Nothing in the platform's sources tells a workload to re-read its credentials file. What
they state is what the PLATFORM does: the envelope expires, and the agent refreshes the
file "so the workload never has to handle an expired file" (``agent/creds.py``). Read
literally that is a guarantee offered to the step, not an obligation placed on it — the
obligation ("so read the file when you need it, rather than keeping a copy from
startup") is an inference, and this harness no longer labels inferences as contract.

That is not the same as saying it does not matter, and the wording of these tests should
not be read as softening it. The consequence is a hard ceiling on how long this node can
run: past one credential lifetime it cannot upload its outputs, and it cannot upload the
failure marker that would explain why either. Fifteen minutes is not an edge case for a
batch step. The half of this failure that IS a contract violation — reporting a
recoverable refusal with the exit code that means "never retry me" — is measured in
``tests/test_failures.py``.

**What is NOT asserted here, and used to be.** Issuing a fresh envelope does not revoke
the one already in the container's hands: a presigned URL is a signature over a deadline
and nothing takes it back. So "re-read before every transfer" is not the expectation, an
expiry-aware step that re-reads when it needs to is right, and a 403 it recovers from is
not a failure. A third test in this file demanded that a step retry an upload the store
had already accepted and then refused; that is revocation, production cannot produce it,
and it has been removed rather than reworded.
"""

from __future__ import annotations

from conformance import contract
from conformance.fakes3 import delay_when, matching, refresh_when
from conformance.job import InputSpec
from conformance.markers import conforms_today, reference_quality

#: A credential lifetime the harness can wait out. Its clock starts when the CONTAINER
#: launches (``Job.start`` re-mints for a job with a TTL), not when the job was built, so
#: the whole budget is available to the step rather than being spent on ``docker run``.
#: Production numbers are fifteen minutes and hours; the mechanism is identical and only
#: the arithmetic is not.
SHORT_TTL_SECONDS = 6.0

#: How long the store holds one response open — comfortably past the expiry above, so
#: that everything the step does afterwards is done with a dead copy of the envelope.
HELD_OPEN_SECONDS = 12.0

_OUTLIVING_ONE_ENVELOPE = (
    'No sentence in the platform\'s sources obliges a workload to re-read its credentials file. '
    'agent/creds.py describes what the AGENT does — "Refresh before expiry, not after… so the workload '
    'never has to handle an expired file" — which is a guarantee to the step, not a duty on it, and '
    'runners/credentials.py likewise only states that the envelope expires and that a long job is '
    're-signed. So a node that reads the file once is conformant. It is also unable to run longer than '
    'one credential lifetime: after that it can upload neither its outputs nor the failure marker that '
    'would explain why. Making that visible is what a reference implementation is for.'
)


def _refresh_while_the_first_read_is_in_flight(job) -> None:
    """Publish a fresh ``creds.json``, then hold that same response open past the expiry.

    The ORDER of these two hooks is the whole point, and getting it wrong is a bug this
    harness had: publishing the replacement after the response had been written is a race
    the step can lose through no fault of its own. It receives the body, re-opens
    ``creds.json`` to get a live credential, and reads the document that is about to be
    replaced — so a CORRECT reload-on-expiry implementation would fail here at random.
    Refreshing while the request is still in flight makes the fresh file strictly older
    than anything the step can do with the response.
    """
    job.endpoint.hooks.on_request.append(refresh_when(matching('input', index=1)))
    job.endpoint.hooks.on_request.append(delay_when(matching('input', index=1), HELD_OPEN_SECONDS))


@conforms_today
@reference_quality(_OUTLIVING_ONE_ENVELOPE)
def test_a_step_that_outlives_its_envelope_reads_the_fresh_one(make_job, sample_input):
    """The ordinary life of a long job, compressed into a dozen seconds.

    Setup:    an envelope good for six seconds, counted from the container's launch. The
              store publishes a fresh ``creds.json`` — with a long life — while the
              input's response is in flight, and then holds that response open for
              twelve. The step's own copy is expired by the time it can act; the file on
              disk is not.
    Action:   run.
    Validate: the step uploads everything and exits 0, and the objects that land are
              signed with the FRESH generation — which is only possible if it re-read the
              file.

    Today the step loads the envelope into a local variable in ``main()`` and every later
    transfer uses that copy.
    """
    job = make_job(inputs=[sample_input], creds_ttl_s=SHORT_TTL_SECONDS)
    _refresh_while_the_first_read_is_in_flight(job)

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


@conforms_today
@reference_quality(_OUTLIVING_ONE_ENVELOPE)
def test_the_work_remaining_after_an_expiry_is_still_done(make_job):
    """An expiry part-way through a batch must cost the batch nothing.

    Setup:    three inputs and a six-second envelope whose clock starts when the
              container launches. The store publishes a fresh ``creds.json`` while the
              FIRST read is in flight and then holds that read open past the expiry.
    Action:   run.
    Validate: all three inputs are fetched and all three copies land — counted as
              DISTINCT objects, not as requests, so three retries of one input would not
              satisfy it, and compared as SETS, because nothing obliges a step to work
              through its inputs in the order the envelope happens to list them.

    The single-input test above shows the step can recover; this one shows the recovery
    is not a one-off. It is also the honest measure of what the gap costs: not "a transfer
    failed" but "everything after the fifteen-minute mark did not happen".
    """
    job = make_job(
        inputs=[
            InputSpec(relpath='one.csv', data=b'1\n'),
            InputSpec(relpath='two.csv', data=b'2\n'),
            InputSpec(relpath='three.csv', data=b'3\n'),
        ],
        creds_ttl_s=SHORT_TTL_SECONDS,
    )
    _refresh_while_the_first_read_is_in_flight(job)

    result = job.run(timeout=120)

    _assert_the_step_had_its_whole_credential_lifetime(job)
    fetched = job.endpoint.names_of('input')
    assert sorted(fetched) == ['input/one.csv', 'input/three.csv', 'input/two.csv'], (
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
