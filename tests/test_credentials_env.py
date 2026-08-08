"""Which variable names the credentials file, and what happens when they disagree.

The agent sets ``LSPO_CREDENTIALS_FILE`` for every job, unconditionally
(``agent/runner.py`` ``_workload_env``). This node reads ``LSPO_CREDENTIALS``, which
nothing on the orchestrator's side sets.

That the node works at all today is a coincidence of two facts that could each change
without warning:

* its own ``Dockerfile`` bakes ``ENV LSPO_CREDENTIALS=/lspo/creds/creds.json``, so the
  variable it reads is always set — by itself;
* the orchestrator's issuer currently always names the DEFAULT credentials path, so the
  value the image baked happens to be right.

The mechanism for a different path is already there and already honoured: the envelope
carries ``credentials_file``, ``agent/creds.py`` takes the mount path from it ("The FIRST
envelope decides where the file lives inside the container"), and the agent injects that
path as ``LSPO_CREDENTIALS_FILE``.

**Where the line falls in this file.** Reading the variable the agent actually sets is
conformance: the contract states that credentials are "delivered out of band as a file at
``credentials_file``", the agent puts that exact path in ``LSPO_CREDENTIALS_FILE``, and a
step that looks somewhere else cannot find its credentials in a configuration the platform
is entitled to produce.

Everything else here is weaker, and this round moved two tests down to say so:

* falling back to the contract's default path, honouring the legacy name at all, which of
  the two wins, and saying so when they disagree are OUR policy. No contract mentions
  ``LSPO_CREDENTIALS``; we keep it working because images in the field bake it.
* **not printing your own credential is reference quality, not conformance.** It was
  labelled as a contract rule and that was wrong: ``agent/redact.py`` says how the AGENT
  cleans its own messages, and ``agent/runner.py`` proves a workload's output is stored
  exactly as written — a description of the consequence, not a prohibition. It stays as
  the strongest expectation in the file, under an honest label.
"""

from __future__ import annotations

from conformance import contract
from conformance.markers import (
    conforms_today,
    expected_red_until_fixed,
    our_policy,
    reference_quality,
    traces_to,
)

ALTERNATIVE_CREDS = '/lspo/creds/envelope.json'
STALE_CREDS = '/lspo/creds/stale.json'


@expected_red_until_fixed
@traces_to(
    'external/contract.py InvocationManifest: "credentials are delivered out of band as a file at '
    'credentials_file", whose own definition is "In-container path where credentials are mounted" — so '
    'the path is the platform\'s to choose, not the image\'s. agent/runner.py _workload_env then puts '
    'exactly that path in the environment: "LSPO_CREDENTIALS_FILE": context.creds.mount_file, for every '
    'job. agent/creds.py takes the path from the envelope — "The FIRST envelope decides where the file '
    'lives inside the container" — so a non-default path is a shape the platform really produces. '
    'Nothing anywhere sets LSPO_CREDENTIALS.'
)
def test_the_credentials_file_variable_is_honoured(make_job, sample_input):
    """The node must read the file ``LSPO_CREDENTIALS_FILE`` names.

    Setup:    a job whose credentials are mounted as ``/lspo/creds/envelope.json`` —
              a path both the envelope and the manifest declare, and which the agent
              genuinely supports. ``/lspo/creds/creds.json`` does not exist.
    Action:   run the container with the variables the agent injects and nothing else.
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
@our_policy(
    'The agent ALWAYS injects LSPO_CREDENTIALS_FILE, so in production a step that requires it is never '
    'actually broken — this is defence, not conformance. What it buys: external/contract.py names '
    'DEFAULT_CREDENTIALS_FILE = "/lspo/creds/creds.json" as the path the orchestrator mounts to when the '
    'envelope says nothing, so a step that knows the default keeps working under a runner that forgot the '
    'variable, and under anything that starts the image by hand.'
)
def test_with_no_variable_at_all_the_default_path_is_used(make_job, sample_input):
    """With neither variable set, the contract's default path is a sensible answer.

    Setup:    credentials at the contract default ``/lspo/creds/creds.json``.
    Action:   run with ``LSPO_CREDENTIALS_FILE`` omitted AND the image's own
              ``LSPO_CREDENTIALS`` stripped, so the node genuinely sees neither.
    Validate: the step still finds its credentials and finishes.
    """
    job = make_job(inputs=[sample_input])
    container = job.start(omit_env=('LSPO_CREDENTIALS_FILE', 'LSPO_CREDENTIALS'))
    container.wait()
    result = container.collect()

    assert result.exit_code == 0, f'the step could not find the default credentials path:\n{result.output}'
    assert contract.MARKER_FILENAME in job.endpoint.keys_in_order()


@conforms_today
@our_policy(
    'Nothing in the platform mentions LSPO_CREDENTIALS; this repository\'s own Dockerfile is what sets '
    'it, and images built from that Dockerfile are in the field. Keeping it working is a compatibility '
    'promise we are making to ourselves, asserted BEFORE the fix so that whoever teaches the node the '
    'real variable name cannot quietly drop the old one — that would be a worse regression than the '
    'defect it repaired.'
)
def test_the_legacy_variable_on_its_own_is_still_honoured(make_job, sample_input):
    """A container that only has the old variable must keep working.

    Setup:    credentials at a non-default path, named ONLY by the legacy variable.
    Action:   run with ``LSPO_CREDENTIALS_FILE`` omitted.
    Validate: the step reads the file and finishes.
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
@our_policy(
    'A precedence rule between one variable the platform sets and one only we set cannot come from the '
    'platform. Ours is: the name the agent actually sets wins, because it is the only one whose value the '
    'orchestrator chose. The alternative — preferring the older name — means the newer, correct value is '
    'the one that gets ignored, which is the shape of every mid-migration outage.'
)
def test_when_both_variables_are_set_the_current_one_wins(make_job, sample_input):
    """Two names, two values, one of them stale — the platform's name decides.

    Setup:    ``creds.json`` holds the LIVE envelope; ``stale.json`` holds one whose
              credential has already expired, so every URL in it is refused. The legacy
              variable points at the stale one, the current variable at the live one.
    Action:   run.
    Validate: the step used the live envelope and finished.
    """
    job = make_job(inputs=[sample_input])
    job.write_envelope_file('stale.json', job.endpoint.mint(ttl_s=-1))

    result = job.run(env={contract.LEGACY_CREDENTIALS_ENV: STALE_CREDS})

    assert result.exit_code == 0, f'the step used the stale envelope:\n{result.output}'
    assert contract.MARKER_FILENAME in job.endpoint.keys_in_order()


@expected_red_until_fixed
@our_policy(
    'Also ours: nothing requires a step to explain its own configuration. It is here because the failure '
    'it prevents costs a day — the step works, the wrong credentials are in use, and the only symptom is '
    'a 403 much later with nothing connecting it to two variables that disagreed at startup. The test is '
    'built so that printing both names unconditionally does NOT satisfy it: a run where the two AGREE '
    'must stay quiet.'
)
def test_a_disagreement_between_the_two_variables_is_reported_and_agreement_is_not(make_job, sample_input):
    """Say something when they disagree — by NAME, never by value — and nothing when they do not.

    Setup:    two runs. In the first the two variables name the same file; in the second
              they name different files.
    Action:   run both.
    Validate: the second names both variables in its output; the first does not. Neither
              prints any credential material.

    Asserting only the disagreeing case would let "print both variable names at startup,
    always" pass, which is noise rather than a warning — and noise at startup is what
    teaches people to skip the first ten lines of a log.
    """
    agreeing = make_job(inputs=[sample_input])
    quiet = agreeing.run(env={contract.LEGACY_CREDENTIALS_ENV: agreeing.credentials_file})
    assert quiet.exit_code == 0, quiet.output
    assert not (
        'LSPO_CREDENTIALS_FILE' in quiet.output and contract.LEGACY_CREDENTIALS_ENV in quiet.output
    ), f'the step announces both variables even when they agree, which makes the real warning invisible:\n{quiet.output}'

    disagreeing = make_job(inputs=[sample_input])
    disagreeing.write_envelope_file('stale.json', disagreeing.endpoint.mint(ttl_s=-1))
    noisy = disagreeing.run(env={contract.LEGACY_CREDENTIALS_ENV: STALE_CREDS})

    assert 'LSPO_CREDENTIALS_FILE' in noisy.output, f'the current variable is never named:\n{noisy.output}'
    assert contract.LEGACY_CREDENTIALS_ENV in noisy.output, f'the legacy variable is never named:\n{noisy.output}'
    assert_no_credential_material(noisy.output)


_NOT_LEAKING_IS_NOT_A_RULE = (
    'Nothing makes it a CONTRACT violation for a workload to print its own credential, and this harness '
    'labelled two tests as though it did. agent/redact.py governs how the AGENT cleans URLs out of the '
    'messages IT composes ("A presigned URL is not an address with a password attached: the query string '
    'IS the credential"); agent/runner.py hands a container\'s output to the log buffer verbatim '
    '(stream_logs(context.handle, context.buffer.add)), which is a description of what happens to what '
    'the step prints, not a rule about what it may print. So a leaky node is conformant. It is also a '
    'node nobody should copy: whatever it prints is stored with the execution, shown to everyone who can '
    'see the run, and searchable — and the credentials being short-lived makes a leak smaller, not '
    'harmless. This is the strongest reference-quality expectation in the suite, and it is still not a '
    'conformance requirement.'
)


@conforms_today
@reference_quality(_NOT_LEAKING_IS_NOT_A_RULE)
def test_an_ordinary_run_prints_no_credential_material(make_job, sample_input):
    """Whatever the step prints is stored with the execution and searchable.

    Setup:    an ordinary successful job.
    Action:   run.
    Validate: no presigned URL, no upload-policy field and no signature appears in the
              container's output.
    """
    job = make_job(inputs=[sample_input])
    result = job.run()

    assert result.exit_code == 0, result.output
    assert_no_credential_material(result.output)


@expected_red_until_fixed
@reference_quality(
    _NOT_LEAKING_IS_NOT_A_RULE
    + ' The specific trap this one measures is named in agent/redact.py: "requests in particular puts '
    'the full URL into the text of an HTTP error (403 Client Error: Forbidden for url: '
    'https://…?X-Amz-Signature=…), so redacting only the URI the agent interpolates itself would miss '
    'the most likely leak by far." The natural way to report a failed fetch is exactly what publishes '
    'the credential.'
)
def test_a_refused_request_does_not_print_the_presigned_url(make_job, sample_input):
    """A 403 must be reported without quoting the credential that earned it.

    Setup:    a job pointed at an envelope whose credential has already expired, so every
              request it makes is refused.
    Action:   run and read the log.
    Validate: the failure is reported, and no presigned URL is in it.

    The natural way to report a failed fetch — let the exception's text through — is
    exactly what publishes the credential, because ``requests`` builds that text out of
    the URL. The platform cannot save the step here: it redacts its own words, not the
    container's.
    """
    job = make_job(inputs=[sample_input])
    job.write_envelope_file('stale.json', job.endpoint.mint(ttl_s=-1))

    result = job.run(env={contract.LEGACY_CREDENTIALS_ENV: STALE_CREDS})

    assert result.exit_code != 0, 'the run was supposed to fail on an expired credential'
    assert_no_credential_material(result.output)


#: Substrings that only ever appear inside something that grants access. The query
#: parameter is this endpoint's stand-in for a real presigned signature, so a URL that
#: carries it IS the credential; the others are the upload policy and its signature.
#: Deliberately generation-agnostic — a leak of an expired credential is still a leak.
CREDENTIAL_MATERIAL = ('?tok=', 'signature-for-', 'ZmFrZS1wb2xpY3k=', 'AKIACONFORMANCE')


def assert_no_credential_material(output: str) -> None:
    """Nothing that grants access may appear in the container's output."""
    leaked = sorted(marker for marker in CREDENTIAL_MATERIAL if marker in output)
    assert not leaked, f'credential material reached the log: {leaked}\n---\n{output}'
