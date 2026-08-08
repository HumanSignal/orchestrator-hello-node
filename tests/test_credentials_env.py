"""Which variable names the credentials file, and what happens when they disagree.

The agent injects **nine** variables, and the one that says where the credentials are is
``LSPO_CREDENTIALS_FILE``. This node reads ``LSPO_CREDENTIALS``, which nothing on the
orchestrator's side sets.

That the node works at all today is a coincidence of two facts that could each change
without warning:

* its own ``Dockerfile`` bakes ``ENV LSPO_CREDENTIALS=/lspo/creds/creds.json``, so the
  variable it reads is always set — by itself;
* the orchestrator currently always issues the DEFAULT credentials path, so the value
  the image baked happens to be right.

Both sides of the contract already support a non-default path (the envelope and the
manifest each carry ``credentials_file``, and the agent mounts the directory that path
lives in). The first customer node that is handed one — or the first rebuild of this
image without that ``ENV`` line — reads a file that is not there.
"""

from __future__ import annotations

from conformance import contract
from conformance.markers import conforms_today, expected_red_until_fixed

ALTERNATIVE_CREDS = '/lspo/creds/envelope.json'


@expected_red_until_fixed
def test_the_credentials_file_variable_is_honoured(make_job, sample_input):
    """The node must read the file LSPO_CREDENTIALS_FILE names.

    Setup:    a job whose credentials are mounted as ``/lspo/creds/envelope.json`` —
              a path both the envelope and the manifest declare, and which the agent
              genuinely supports. ``/lspo/creds/creds.json`` does not exist.
    Action:   run the container with the nine injected variables and nothing else.
    Validate: the step ran and finished — it found its credentials.

    Today it reads ``LSPO_CREDENTIALS``, gets the path its own image baked in, and dies
    on a file that is not there. Nothing in the log names the variable it should have
    read, so the failure looks like a broken mount rather than a misread contract.
    """
    job = make_job(inputs=[sample_input], credentials_file=ALTERNATIVE_CREDS)
    result = job.run()

    assert result.exit_code == 0, f'the step failed instead of reading {ALTERNATIVE_CREDS}:\n{result.output}'
    assert contract.MARKER_FILENAME in job.endpoint.keys_in_order()


@expected_red_until_fixed
def test_with_no_variable_at_all_the_default_path_is_used(make_job, sample_input):
    """With neither variable set, the contract's default path is the answer.

    Setup:    credentials at the contract default ``/lspo/creds/creds.json``.
    Action:   run with ``LSPO_CREDENTIALS_FILE`` omitted AND the image's own
              ``LSPO_CREDENTIALS`` stripped, so the node genuinely sees neither.
    Validate: the step still finds its credentials and finishes.

    ``DEFAULT_CREDENTIALS_FILE`` is part of the contract, not a fallback the agent
    happens to choose. A step that only works when somebody tells it where to look has
    a hard dependency on a variable the contract says is optional.
    """
    job = make_job(inputs=[sample_input])
    container = job.start(omit_env=('LSPO_CREDENTIALS_FILE', 'LSPO_CREDENTIALS'))
    container.wait()
    result = container.collect()

    assert result.exit_code == 0, f'the step could not find the default credentials path:\n{result.output}'
    assert contract.MARKER_FILENAME in job.endpoint.keys_in_order()


@conforms_today
def test_the_legacy_variable_on_its_own_is_still_honoured(make_job, sample_input):
    """A container that only has the old variable must keep working.

    Setup:    credentials at a non-default path, named ONLY by the legacy variable.
    Action:   run with ``LSPO_CREDENTIALS_FILE`` omitted.
    Validate: the step reads the file and finishes.

    This is the compatibility half of the fix, and it is asserted BEFORE the fix so that
    whoever makes the node read the new variable cannot quietly drop the old one. Images
    in the field bake it, and a fix that broke them would be a worse regression than the
    defect it repaired.
    """
    job = make_job(inputs=[sample_input], credentials_file=ALTERNATIVE_CREDS)
    container = job.start(
        env={contract.LEGACY_CREDENTIALS_ENV: ALTERNATIVE_CREDS}, omit_env=('LSPO_CREDENTIALS_FILE',)
    )
    container.wait()
    result = container.collect()

    assert result.exit_code == 0, result.output
    assert contract.MARKER_FILENAME in job.endpoint.keys_in_order()


@expected_red_until_fixed
def test_when_both_variables_are_set_the_current_one_wins(make_job, sample_input):
    """Two names, two values, one of them stale — the contract's name decides.

    Setup:    ``creds.json`` holds the LIVE envelope; ``stale.json`` holds an envelope
              from a superseded generation whose URLs are all refused. The legacy
              variable points at the stale one, the current variable at the live one.
    Action:   run.
    Validate: the step used the live envelope and finished.

    This is the shape of a real upgrade: an operator sets the new variable, the old one
    is still baked into an image somewhere, and the two disagree. Preferring the older
    name means the newer, correct value is the one that gets ignored.
    """
    job = make_job(inputs=[sample_input])
    stale_token = job.endpoint.token
    job.write_envelope_file('stale.json', stale_token)
    job.endpoint.rotate()  # creds.json becomes generation 2; stale.json is now dead

    result = job.run(env={contract.LEGACY_CREDENTIALS_ENV: '/lspo/creds/stale.json'})

    assert result.exit_code == 0, f'the step used the stale envelope:\n{result.output}'
    assert contract.MARKER_FILENAME in job.endpoint.keys_in_order()


@expected_red_until_fixed
def test_a_disagreement_between_the_two_variables_is_reported(make_job, sample_input):
    """When both are set and disagree, say so — by NAME, never by value.

    Setup:    as above: the two variables point at two different files.
    Action:   run.
    Validate: the log names both variables, so an operator can see which one was used
              and why; and it prints neither path's CONTENTS.

    A silent preference is the failure mode that costs a day: the step works, the wrong
    credentials are in use, and the only symptom is a 403 much later with nothing
    connecting it to the two variables that disagreed at startup.
    """
    job = make_job(inputs=[sample_input])
    job.write_envelope_file('stale.json', job.endpoint.token)
    job.endpoint.rotate()

    result = job.run(env={contract.LEGACY_CREDENTIALS_ENV: '/lspo/creds/stale.json'})

    output = result.output
    assert 'LSPO_CREDENTIALS_FILE' in output, f'the current variable is never named:\n{output}'
    assert contract.LEGACY_CREDENTIALS_ENV in output, f'the legacy variable is never named:\n{output}'
    assert_no_credential_material(job, output)


@conforms_today
def test_an_ordinary_run_prints_no_credential_material(make_job, sample_input):
    """Whatever the step prints is stored with the execution and searchable.

    Setup:    an ordinary successful job.
    Action:   run.
    Validate: no presigned URL, no upload-policy field and no signature appears in the
              container's output.

    The credentials are short-lived, but a leaked one is still a leak, and the run log
    is the most-read artifact of a pipeline.
    """
    job = make_job(inputs=[sample_input])
    result = job.run()

    assert result.exit_code == 0, result.output
    assert_no_credential_material(job, result.output)


@expected_red_until_fixed
def test_a_refused_request_does_not_print_the_presigned_url(make_job, sample_input):
    """A 403 must be reported without quoting the credential that earned it.

    Setup:    a job whose credentials are superseded before the step can use them, so
              every request it makes is refused.
    Action:   run and read the log.
    Validate: the failure is reported, and the presigned URL is not in it.

    ``requests`` puts the full URL into the message of the error it raises for a bad
    status, and that URL IS the credential. So the natural way to report a failed
    fetch — let the exception's text through — publishes the thing that failed.
    """
    job = make_job(inputs=[sample_input])
    dead_token = job.endpoint.token
    job.write_envelope_file('stale.json', dead_token)
    job.endpoint.rotate()

    result = job.run(env={contract.LEGACY_CREDENTIALS_ENV: '/lspo/creds/stale.json'})

    assert result.exit_code != 0, 'the run was supposed to fail on a superseded credential'
    assert_no_credential_material(job, result.output)


#: Substrings that only ever appear inside something that grants access. The query
#: parameter is this endpoint's stand-in for a real presigned signature, so a URL that
#: carries it IS the credential; the other two are the upload policy and its signature.
#: Deliberately generation-agnostic — a leak of a superseded credential is still a leak.
CREDENTIAL_MATERIAL = ('?tok=', 'signature-for-', 'ZmFrZS1wb2xpY3k=')


def assert_no_credential_material(job, output: str) -> None:
    """Nothing that grants access may appear in the container's output."""
    leaked = sorted(marker for marker in CREDENTIAL_MATERIAL if marker in output)
    assert not leaked, f'credential material reached the log: {leaked}\n---\n{output}'
