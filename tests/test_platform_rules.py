"""Rules whose subject is the PLATFORM, not this node.

Nothing here can be made to pass or fail by editing ``node.py``. They are in the suite
because a node is judged against these rules, and a rule nobody has written down drifts
until two sides of a contract quietly disagree. Each one is a re-statement of something
the agent or the collector really does — re-stated rather than imported, so that a
harness which shares no code with the system it checks can still notice that system
changing.
"""

from __future__ import annotations

import json

import pytest

from conformance import contract, docker, platform_rules
from conformance.markers import subject_is_platform


# ------------------------------------------------------------------- exit codes


@subject_is_platform
@pytest.mark.parametrize(
    ('code', 'expected'),
    [
        (0, contract.CLASS_OK),
        (1, contract.CLASS_TRANSIENT),
        (10, contract.CLASS_PERMANENT),
        (20, contract.CLASS_CANCELLED),
        (30, contract.CLASS_CONTENTION),
        (137, contract.CLASS_TRANSIENT),  # SIGKILL, which is what an OOM kill looks like
        (143, contract.CLASS_TRANSIENT),  # SIGTERM with no handler
        (2, contract.CLASS_TRANSIENT),
        (None, contract.CLASS_TRANSIENT),
    ],
)
def test_unknown_exit_codes_are_treated_as_transient(code, expected):
    """Setup: each exit code a step can return. Action: classify it. Validate: the
    documented class, with everything unrecognised falling to transient.

    The default matters more than the table. A crash, an OOM kill or a signal death is
    far more often a retryable accident than a considered "never retry me", so a step
    that means permanent has to say so with 10. The cost of the default is visible in
    this suite: an OOM-killed step exits 137 and is retried forever.
    """
    assert contract.classify_exit(code) == expected


# ------------------------------------------------- the environment the agent injects


@subject_is_platform
def test_the_agent_injects_exactly_nine_variables():
    """Setup: the injected set. Validate: it is the nine the contract names, and the
    one this node reads is NOT among them."""
    assert len(contract.INJECTED_ENV) == 9
    assert 'LSPO_CREDENTIALS_FILE' in contract.INJECTED_ENV
    assert contract.LEGACY_CREDENTIALS_ENV not in contract.INJECTED_ENV


@subject_is_platform
def test_a_manifest_may_request_env_but_the_agent_decides():
    """The manifest NAMES variables; the values come from the agent's machine.

    Setup:    a manifest requesting three variables against an operator allowlist that
              permits one exactly and one by pattern.
    Action:   ask which are refused.
    Validate: only the unlisted one, and it is refused by name rather than skipped.

    A silent skip is the worst of the three options: the step runs without a credential
    it was written to need, fails somewhere deep inside, and nobody learns that the
    agent refused it. This is also a rule no node can satisfy on its own — which is why
    it is here rather than among the node's own tests.
    """
    refused = platform_rules.refused_env(
        ['ACME_TOKEN', 'CUSTOMER_API_KEY', 'AWS_SECRET_ACCESS_KEY'],
        allowlist=['ACME_TOKEN', 'CUSTOMER_*'],
    )
    assert refused == ['AWS_SECRET_ACCESS_KEY']


# ------------------------------------------------------------------ progress lines


@subject_is_platform
@pytest.mark.parametrize(
    ('line', 'expected'),
    [
        ('@lspo:progress {"fraction": 0.4, "phase": "encoding"}', {'fraction': 0.4, 'phase': 'encoding'}),
        ('@lspo:progress {"fraction": 1}', {'fraction': 1.0, 'phase': ''}),
        ('@lspo:progress {"fraction": 0}', {'fraction': 0.0, 'phase': ''}),
    ],
)
def test_a_well_formed_progress_line_is_consumed(line, expected):
    """Setup: a valid progress line. Action: read it. Validate: it becomes a sample.

    It is consumed rather than logged because it is instrumentation, not output: it goes
    to the Runs UI on the next heartbeat instead of into the log.
    """
    assert platform_rules.absorb_progress(line) == expected


@subject_is_platform
@pytest.mark.parametrize(
    'line',
    [
        '@lspo:progress{"fraction": 0.4}',  # the trailing space is part of the prefix
        '@lspo:progress not json at all',
        '@lspo:progress {"phase": "encoding"}',  # no fraction
        '@lspo:progress {"fraction": "half"}',
        '@lspo:progress {"fraction": 1.5}',  # outside 0..1
        '@lspo:progress {"fraction": -0.1}',
        ' @lspo:progress {"fraction": 0.4}',  # not at the start of the line
        'INFO copying file 3 of 9',
    ],
)
def test_a_malformed_progress_line_stays_an_ordinary_log_line(line):
    """Setup: each way to get a progress line slightly wrong. Action: read it.
    Validate: it is NOT consumed.

    Silently swallowing a typo would make it look identical to a step that reports no
    progress at all — the author would see neither their line nor a progress bar, and
    have nothing to debug from.
    """
    assert platform_rules.absorb_progress(line) is None


@subject_is_platform
def test_progress_lines_are_removed_from_the_log_and_the_rest_is_kept():
    """Setup: a log with two valid samples and one near-miss. Action: split it.
    Validate: the samples are taken out, the near-miss stays in with everything else."""
    ordinary, samples = platform_rules.split_log(
        [
            'INFO starting',
            '@lspo:progress {"fraction": 0.1, "phase": "reading"}',
            '@lspo:progress{"fraction": 0.5}',
            '@lspo:progress {"fraction": 0.9}',
            'INFO done',
        ]
    )
    assert [sample['fraction'] for sample in samples] == [0.1, 0.9]
    assert ordinary == ['INFO starting', '@lspo:progress{"fraction": 0.5}', 'INFO done']


# ------------------------------------------------------- credentials as a directory


@subject_is_platform
def test_a_bind_mounted_file_never_sees_a_rotation(image, workdir):
    """Why the agent mounts the DIRECTORY and never the credentials file itself.

    Setup:    a file on the host, bind-mounted into a container BY PATH, alongside the
              directory it lives in.
    Action:   while the container is running, replace the file the way the agent does —
              write a new one beside it, then ``os.replace``. Then have the container
              read both copies.
    Validate: the file-mount still shows the ORIGINAL content; the directory-mount shows
              the new content.

    ``os.replace`` swaps a directory entry, not an inode. A bind-mounted file pins the
    inode it was mounted from, so the container holds the old document for the rest of
    its life and never learns its credentials changed — which would make every
    rotation test in this suite unfalsifiable, and every long job in production fail on
    an expiry it could not have avoided.
    """
    import os
    import tempfile
    import time

    host_dir = workdir / 'mountcheck'
    host_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(host_dir, 0o755)
    target = host_dir / 'creds.json'
    target.write_text('{"generation": "A"}')
    os.chmod(target, 0o644)

    script = 'import time,sys; time.sleep(3); ' 'print(open("/pinned/creds.json").read(), open("/live/creds.json").read())'
    container = docker.start(
        image,
        name=f'lspo-conformance-mount-{os.getpid()}',
        env={},
        creds_dir=None,
        mounts=((target, '/pinned/creds.json', 'ro'), (host_dir, '/live', 'ro')),
        entrypoint='python',
        command=('-c', script),
    )
    try:
        time.sleep(0.5)
        handle, tmp = tempfile.mkstemp(dir=str(host_dir), prefix='.creds-', suffix='.json')
        with os.fdopen(handle, 'w') as stream:
            stream.write('{"generation": "B"}')
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)

        container.wait(timeout=60)
        result = container.collect()
        assert '"generation": "A"' in result.output, f'the pinned file-mount changed under us: {result.output}'
        assert '"generation": "B"' in result.output, f'the directory-mount did not see the swap: {result.output}'
    finally:
        container.remove()


# ------------------------------------------------- who may read the credentials file


@subject_is_platform
def test_a_0700_credentials_directory_is_unreadable_to_any_other_user(image, workdir):
    """The undocumented constraint a customer's Dockerfile has to satisfy.

    Setup:    a credentials directory with the modes the agent really uses — 0700 on the
              directory, 0600 on the file — owned by the user running these tests.
    Action:   read it from inside the node's image twice: once as the image's own user,
              once as the directory's owner.
    Validate: the first is refused with a permission error; the second succeeds.

    In production the two happen to line up: the agent runs as uid 10001
    (``Dockerfile.agent``) and this node's image also runs as uid 10001 (its own
    ``Dockerfile``), so the workload can read a directory only its owner can open. That
    is a coincidence, not a design. A customer image that picks any other non-root user —
    the ordinary thing to do — gets ``PermissionError`` on its own credentials file, and
    nothing in the contract documentation warns them. Running as root avoids it, which is
    precisely the wrong thing to encourage.

    This test does not fail today. It is here so the constraint is written down and
    executable: if the agent ever changes those modes, or this image changes its uid, one
    of these two assertions changes with it.
    """
    import os

    assert contract.CREDENTIALS_DIR_MODE == 0o700 and contract.CREDENTIALS_FILE_MODE == 0o600

    creds_dir = workdir / 'perms'
    creds_dir.mkdir(parents=True, exist_ok=True)
    (creds_dir / 'creds.json').write_text('{"schema_version": 1}')
    os.chmod(creds_dir, contract.CREDENTIALS_DIR_MODE)
    os.chmod(creds_dir / 'creds.json', contract.CREDENTIALS_FILE_MODE)

    as_image_user = _read_creds_as(image, creds_dir, user=None)
    assert 'PermissionError' in as_image_user, (
        f'a 0700 directory owned by uid {os.getuid()} was readable by the image\'s own user: {as_image_user}'
    )

    as_owner = _read_creds_as(image, creds_dir, user=str(os.getuid()))
    assert '"schema_version": 1' in as_owner, as_owner


def _read_creds_as(image: str, creds_dir, user: str | None) -> str:
    import os

    container = docker.start(
        image,
        name=f'lspo-conformance-perms-{os.getpid()}-{user or "image"}',
        env={},
        creds_dir=creds_dir,
        user=user,
        entrypoint='python',
        command=('-c', 'print(open("/lspo/creds/creds.json").read())'),
    )
    try:
        container.wait(timeout=60)
        return container.collect().output
    finally:
        container.remove()


# -------------------------------------------------------------- document ceilings


@subject_is_platform
def test_the_document_ceilings_are_what_the_contract_says():
    """Setup: the three ceilings. Validate: 8 MiB, 8 MiB, 1 MiB.

    The manifest and the marker share the larger one because both carry inventories and
    truncating an inventory would be a correctness bug rather than a safety measure.
    """
    assert contract.MAX_MANIFEST_BYTES == 8 * 1024 * 1024
    assert contract.MAX_MARKER_BYTES == 8 * 1024 * 1024
    assert contract.MAX_RESULT_BYTES == 1024 * 1024


@subject_is_platform
def test_an_oversized_marker_is_refused_even_when_it_is_valid_json():
    """Setup: a well-formed marker whose serialisation exceeds 8 MiB.
    Action: validate it. Validate: refused on size, before anything else."""
    marker = {
        'schema_version': 1,
        'execution_id': 1,
        'attempt': 1,
        'generation': 1,
        'status': 'succeeded',
        'objects': [],
        'produced_ports': {},
    }
    oversized = json.dumps(marker).encode('utf-8') + b' ' * contract.MAX_MARKER_BYTES
    with pytest.raises(contract.ContractViolation, match='over the'):
        contract.validate_marker(marker, raw_bytes=oversized)


@subject_is_platform
@pytest.mark.parametrize(
    'digest',
    ['A' * 64, 'sha256:' + 'a' * 64, 'a' * 63, 'a' * 64 + '\n', '', 'not a hash'],
)
def test_a_digest_that_is_not_lowercase_64_hex_is_refused(digest):
    """Two spellings of one hash read as "the content changed" to the side comparing them.

    Setup: uppercase, prefixed, truncated, newline-padded and empty digests.
    Action: validate. Validate: each is refused.
    """
    with pytest.raises(contract.ContractViolation):
        contract.check_sha256(digest, 'test')


@subject_is_platform
@pytest.mark.parametrize('size', [True, '12', 1.0, -1, None])
def test_a_size_that_is_not_a_real_non_negative_int_is_refused(size):
    """``True`` is an ``int`` in Python, and ``"12"`` looks like one to a lax parser.

    Setup: each near-miss. Action: validate. Validate: refused.
    """
    with pytest.raises(contract.ContractViolation):
        contract.check_size(size, 'test')
