"""Rules whose subject is the PLATFORM, not this node.

Nothing here can be made to pass or fail by editing ``node.py``.

**Read this before trusting the label.** Most of these tests are RESTATEMENTS: they hold
this harness's own copy of a platform rule to what the platform's source says. A change
on the platform's side cannot turn them red by itself — nobody's CI runs this harness
against the orchestrator — so they are not a tripwire on the platform. What they are is
threefold, and each is worth the file:

* the rest of the suite leans on these rules, so they have to be written down somewhere
  rather than assumed inside an assertion;
* the citation on each one names the source line it restates, so a reader can check the
  restatement against the original in one step, which is exactly how the "exactly nine
  environment variables" mistake in this file was found and fixed;
* two of them are not restatements at all. ``test_a_bind_mounted_file_never_sees_a_rotation``
  and ``test_a_0700_credentials_directory_is_unreadable_to_any_other_user`` exercise real
  kernel and docker behaviour with real containers, and would genuinely change if the
  platform's mount or permission choices did.
"""

from __future__ import annotations

import json

import pytest

from conformance import contract, docker, platform_rules
from conformance.markers import reference_quality, subject_is_platform, traces_to


# ------------------------------------------------------------------- exit codes


@subject_is_platform
@traces_to(
    'external/contract.py classify_exit: "Unknown codes classify as transient — a crash, an OOM kill, or '
    'a signal death is far more often a retryable accident than a deliberate permanent failure, and a '
    'step that means \'do not retry me\' must say so with EXIT_PERMANENT."'
)
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

    A restatement, and the default matters more than the table. The cost of that default
    is visible in this suite: an OOM-killed step exits 137 and is retried forever.
    """
    assert contract.classify_exit(code) == expected


# ------------------------------------------------- the environment the agent injects


@subject_is_platform
@traces_to(
    'agent/runner.py _workload_env: "env = {name: os.environ[name] for name in manifest.env_names if '
    'name in os.environ}" followed by "env.update({\'LSPO_JOB_ID\': …, \'LSPO_EXECUTION_ID\': …, …})" — '
    'the job\'s own ids are applied LAST, over whatever the manifest asked for.'
)
def test_the_agent_injects_its_own_variables_plus_what_the_manifest_asked_for():
    """The environment is nine variables PLUS the manifest's, not nine and nothing else.

    Setup:    a manifest requesting two permitted variables, one of which the agent's
              machine does not have, and one which collides with an injected name.
    Action:   build the environment the way the agent does.
    Validate: the requested-and-present one arrives; the requested-and-absent one is
              simply missing; the colliding one does NOT shadow the agent's value.

    This file used to assert "the agent injects exactly nine variables", which is wrong
    and was wrong when it was written: ``env_names`` exists precisely so a step can ask
    for more. What is fixed is which nine the agent always sets and that they win any
    collision.
    """
    env = platform_rules.workload_env(
        {'LSPO_EXECUTION_ID': '4242', 'LSPO_JOB_ID': '77'},
        requested=['ACME_TOKEN', 'NOT_ON_THIS_MACHINE', 'LSPO_EXECUTION_ID'],
        allowlist=['ACME_*', 'NOT_ON_THIS_MACHINE', 'LSPO_EXECUTION_ID'],
        machine={'ACME_TOKEN': 'secret', 'LSPO_EXECUTION_ID': 'a lie'},
    )

    assert env['ACME_TOKEN'] == 'secret'
    assert 'NOT_ON_THIS_MACHINE' not in env
    assert env['LSPO_EXECUTION_ID'] == '4242', 'a manifest must not be able to shadow the job\'s own ids'


@subject_is_platform
@traces_to(
    'agent/runner.py _workload_env: "a name outside LSPO_AGENT_ALLOWED_ENV fails the job — visibly, '
    'naming the variable. A silent skip would be worse than either alternative: the step would run '
    'without a credential it was written to need and fail somewhere deep inside, and nobody would learn '
    'that the agent had refused it." The refusal it raises reads "the manifest asks this agent to inject '
    'environment variable(s) " followed by the names and "ALLOWED_ENV does not permit".'
)
def test_a_variable_outside_the_allowlist_fails_the_job_rather_than_being_skipped():
    """The manifest NAMES variables; the agent decides, and says no out loud.

    Setup:    a manifest requesting three variables against an allowlist that permits one
              exactly and one by pattern.
    Action:   build the environment.
    Validate: it is refused, by name.
    """
    with pytest.raises(platform_rules.EnvRefused, match='AWS_SECRET_ACCESS_KEY'):
        platform_rules.workload_env(
            {},
            requested=['ACME_TOKEN', 'CUSTOMER_API_KEY', 'AWS_SECRET_ACCESS_KEY'],
            allowlist=['ACME_TOKEN', 'CUSTOMER_*'],
            machine={},
        )


# ------------------------------------------------------------------ progress lines


@subject_is_platform
@traces_to(
    'agent/logbuf.py _absorb_progress: prefix "@lspo:progress " (with the trailing space), '
    '"fraction = float(payload[\'fraction\'])", "if not 0.0 <= fraction <= 1.0: return False", and '
    'phase "str(payload.get(\'phase\') or \'\')[:64]".'
)
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
@traces_to(
    'agent/logbuf.py _absorb_progress: "A malformed progress line is NOT consumed — it goes to the log '
    'as an ordinary line, where its author can see what they wrote. Silently swallowing it would make a '
    'typo look like a step that reports no progress at all."'
)
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
    Validate: it is NOT consumed."""
    assert platform_rules.absorb_progress(line) is None


@subject_is_platform
@traces_to(
    'agent/logbuf.py LogBuffer.add: "Record one line — or absorb it as a progress sample if that is what '
    'it is", with take_progress carrying "the most recent progress sample" to the heartbeat.'
)
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
@traces_to(
    'agent/creds.py: "The file is rewritten in place while the container is reading it… a new file is '
    'written beside it and os.replaced over it… The container sees the swap because the DIRECTORY is '
    'mounted, not the file — a bind-mounted file keeps pointing at the replaced inode and would never '
    'update."'
)
def test_a_bind_mounted_file_never_sees_a_rotation(image, workdir):
    """Why the agent mounts the DIRECTORY and never the credentials file itself.

    Setup:    a file on the host, bind-mounted into a container BY PATH, alongside the
              directory it lives in.
    Action:   while the container is running, replace the file the way the agent does —
              write a new one beside it, then ``os.replace``. Then have the container
              read both copies.
    Validate: the file-mount still shows the ORIGINAL content; the directory-mount shows
              the new content.

    Not a restatement: this runs a real container and observes real kernel behaviour. If
    it ever stopped holding, every rotation test in this suite would become
    unfalsifiable, and every long job in production would fail on an expiry it could not
    have avoided.
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
@traces_to(
    'agent/creds.py JobCredentials.write: "The directory is created 0700 and the file 0600 — on a shared '
    'machine the credential must not be readable by other users", with the agent running as its own uid '
    'and agent/runner.py setting run_as only in local demo mode.'
)
def test_a_0700_credentials_directory_is_unreadable_to_any_other_user(image, workdir):
    """The undocumented constraint a customer's Dockerfile has to satisfy.

    Setup:    a credentials directory with the modes the agent really uses — 0700 on the
              directory, 0600 on the file — owned by the user running these tests.
    Action:   read it from inside the node's image twice: once as the image's own user,
              once as the directory's owner.
    Validate: the first is refused with a permission error; the second succeeds.

    Not a restatement either: this measures real containers against a real 0700
    directory. In production the two happen to line up — the agent runs as uid 10001 and
    this node's image also runs as uid 10001 — so the workload can read a directory only
    its owner can open. That is a coincidence, not a design. A customer image that picks
    any other non-root user gets ``PermissionError`` on its own credentials file, and
    nothing in the contract documentation warns them. Running as root avoids it, which is
    precisely the wrong thing to encourage.
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
@traces_to(
    'external/contract.py: MAX_MANIFEST_BYTES = 8 * 1024 * 1024, MAX_MARKER_BYTES = 8 * 1024 * 1024, '
    'MAX_RESULT_BYTES = 1024 * 1024 — "The manifest and the marker share the larger ceiling because both '
    'legitimately carry inventories … and truncating that would be a correctness bug."'
)
def test_the_document_ceilings_are_what_the_contract_says():
    """Setup: the three ceilings. Validate: 8 MiB, 8 MiB, 1 MiB."""
    assert contract.MAX_MANIFEST_BYTES == 8 * 1024 * 1024
    assert contract.MAX_MARKER_BYTES == 8 * 1024 * 1024
    assert contract.MAX_RESULT_BYTES == 1024 * 1024


@subject_is_platform
@traces_to(
    'external/io.py _read_json: "Reads limit + 1 bytes: one byte past the ceiling is enough to know the '
    'limit was exceeded, without ever holding the whole oversized document in memory", raising '
    'ContractDocumentTooLarge.'
)
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
@traces_to(
    'external/contract.py check_sha256: "Uppercase, truncated, prefixed (sha256:…), whitespace-padded '
    'and placeholder digests are all refused: these values are compared for equality across two '
    'independently written implementations, and two spellings of one hash would read as \'the content '
    'changed\'."'
)
@pytest.mark.parametrize(
    'digest',
    ['A' * 64, 'sha256:' + 'a' * 64, 'a' * 63, 'a' * 64 + '\n', '', 'not a hash'],
)
def test_a_digest_that_is_not_lowercase_64_hex_is_refused(digest):
    """Setup: uppercase, prefixed, truncated, newline-padded and empty digests.
    Action: validate. Validate: each is refused."""
    with pytest.raises(contract.ContractViolation):
        contract.check_sha256(digest, 'test')


@subject_is_platform
@traces_to(
    'external/contract.py: "ByteSize = Annotated[int, Field(strict=True, ge=0)] … A byte count: a real '
    'int (not True, not \'12\'), never negative." Strict mode is what refuses the coercions pydantic '
    'would otherwise perform happily.'
)
@pytest.mark.parametrize('size', [True, '12', 1.0, -1, None])
def test_a_size_that_is_not_a_real_non_negative_int_is_refused(size):
    """Setup: each near-miss. Action: validate. Validate: refused."""
    with pytest.raises(contract.ContractViolation):
        contract.check_size(size, 'test')


@subject_is_platform
@traces_to(
    'external/versioning.py _parsers_for: "version = data.get(\'schema_version\', 1)" — absent means 1 — '
    'and "type(...) is not int rather than isinstance: bool subclasses int, and True == 1 would '
    'otherwise pick the version-1 parser for \'schema_version\': true."'
)
@pytest.mark.parametrize(
    ('document', 'accepted'),
    [
        ({}, True),  # unstamped is version 1
        ({'schema_version': 1}, True),
        ({'schema_version': True}, False),  # True == 1 in Python; the real gate refuses it
        ({'schema_version': 1.0}, False),
        ({'schema_version': '1'}, False),
        ({'schema_version': 2}, False),  # a version this build has no parser for
    ],
)
def test_the_version_gate_defaults_to_one_and_refuses_anything_that_is_not_an_integer(document, accepted):
    """Setup: each spelling of a schema version. Action: read it. Validate: accepted or
    refused exactly as the orchestrator's own version gate would.

    Both halves of this were wrong in this harness's first version — it rejected an
    unstamped document and accepted ``true`` — which is precisely the kind of drift a
    re-implemented contract is prone to.
    """
    if accepted:
        assert contract.check_schema_version(document, 'test') == 1
    else:
        with pytest.raises(contract.ContractViolation):
            contract.check_schema_version(document, 'test')


@subject_is_platform
@traces_to(
    'external/contract.py InputPort._check_prefix_layout: layout=\'prefix\' "requires a non-empty '
    'prefix_digest", "requires a relpath on every object … the prefix_digest is computed over (relpath, '
    'size, sha256)", and refuses a digest that "does not match the digest recomputed from its own '
    'objects (…) — a stated digest that disagrees with the listing beside it is worse than no digest, '
    'because both sides would trust it."'
)
def test_a_prefix_port_must_carry_a_digest_that_matches_its_own_listing():
    """Setup: a prefix port, then the same port with its digest left off, a relpath
    removed, and the digest altered. Action: validate each. Validate: only the first is
    accepted.

    ``external/hashing.py`` fixes the canonical form the digest is computed over, and
    this harness re-implements it: each entry renders as ``relpath\\nsize\\nsha256\\n``,
    the blocks are sorted as strings, and the concatenation is hashed.
    """
    objects = [
        {'uri': 's3://b/one', 'relpath': 'a/one.jpg', 'sha256': 'a' * 64, 'size': 10},
        {'uri': 's3://b/two', 'relpath': 'a/two.jpg', 'sha256': 'b' * 64, 'size': 20},
    ]
    digest = contract.prefix_digest((obj['relpath'], obj['size'], obj['sha256']) for obj in objects)
    good = {'name': 'frames', 'payload_kind': 'tree', 'layout': 'prefix', 'cardinality': 'many',
            'objects': objects, 'prefix_digest': digest}
    contract._check_input_port(good)

    for broken in (
        {**good, 'prefix_digest': None},
        {**good, 'prefix_digest': 'c' * 64},
        {**good, 'objects': [{k: v for k, v in objects[0].items() if k != 'relpath'}, objects[1]]},
    ):
        with pytest.raises(contract.ContractViolation):
            contract._check_input_port(broken)


@subject_is_platform
@reference_quality(
    'Not a platform rule at all, and it is here rather than in the node\'s own tests because no black-box '
    'test of a container can observe it: agent/runner.py refuses a manifest naming a variable outside '
    'LSPO_AGENT_ALLOWED_ENV BEFORE the container starts. Written down so the constraint on a node author '
    'exists somewhere executable.'
)
def test_an_allowlist_pattern_matches_the_way_the_agent_matches_it():
    """Setup: exact names and shell-style patterns. Action: ask which are refused.
    Validate: pattern matching is case-sensitive and anchored to the whole name."""
    assert platform_rules.refused_env(['ACME_TOKEN'], ['ACME_*']) == []
    assert platform_rules.refused_env(['acme_token'], ['ACME_*']) == ['acme_token']
    assert platform_rules.refused_env(['XACME_TOKEN'], ['ACME_*']) == ['XACME_TOKEN']
