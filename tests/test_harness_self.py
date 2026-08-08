"""Proving the instrument before trusting its readings.

Every "the node fails this" verdict in this suite is only as trustworthy as the fake
store and the container plumbing that produced it. If the endpoint accepted everything,
if expiry did not expire, if upload order were an artefact of dictionary iteration, or if
a hung container were reported as a clean exit, the whole baseline would be fiction.

These tests hold the harness to the claims the rest of the suite leans on:

* the prefix fence, the POST policy and the credential check really refuse;
* a credential expires when it says it will — and, crucially, does NOT die merely
  because a newer one was issued, nor because its body took longer to arrive than it
  had left to live;
* a fresh envelope really replaces the file, atomically, and a reader really sees it;
* the store knows what it has not finished, and says so until it has;
* the order objects arrive in is really arrival order, and a re-upload really wins;
* a container that never exits is reported as a container that never exited, and a
  container that has vanished is never reported as a result at all;
* this harness's copy of the contract accepts the orchestrator's own frozen documents,
  and every rule it claims to quote is really in them.

They talk to the endpoint directly, with an HTTP client, so a bug in ``Job`` cannot make
them pass.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import socket
import threading
import time

import pytest
import requests

from conformance import citations, contract, docker
from conformance.fakes3 import Blob, Endpoint, delay_when, matching
from conformance.job import InputSpec, Job
from conformance.markers import harness_self_test, our_policy, traces_to

PREFIX = 'conformance/pipelines/1/executions/2/attempts/1/gen-1/'
LOCALHOST = '127.0.0.1'
TESTS_DIR = pathlib.Path(__file__).resolve().parent
FIXTURES = TESTS_DIR.parent / 'conformance' / 'fixtures'

_INSTRUMENT = (
    'The subject is this harness. A conformance verdict is a measurement, and a measurement from an '
    'uncalibrated instrument is a guess with a number attached.'
)


@pytest.fixture
def endpoint():
    store = Endpoint().start()
    try:
        yield store
    finally:
        store.stop()


def post(store: Endpoint, token: str, key: str, data: bytes, *, drop: tuple[str, ...] = ()) -> requests.Response:
    policy = store.post_policy(LOCALHOST, token, PREFIX)
    fields = {name: value for name, value in policy['fields'].items() if name not in drop}
    fields['key'] = key
    return requests.post(policy['url'], data=fields, files={'file': ('part', data)}, timeout=30)


def post_in_two_halves(store: Endpoint, token: str, key: str, data: bytes, *, pause_s: float) -> int:
    """POST over a raw socket, pausing in the MIDDLE of the body. Returns the status.

    Neither ``requests`` nor the endpoint's hooks can express this. ``requests`` sends a
    body as fast as the socket takes it, and a hook runs only once the whole body has
    been read — so the one thing that matters here, time passing WHILE the bytes are
    arriving, is unreachable through either. Hence the hand-built multipart body and the
    bare socket.
    """
    policy = store.post_policy(LOCALHOST, token, PREFIX)
    fields = dict(policy['fields'])
    fields['key'] = key
    body, content_type = _multipart_body(fields, data)
    head = (
        f'POST /upload HTTP/1.1\r\nHost: {LOCALHOST}:{store.port}\r\n'
        f'Content-Type: {content_type}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'
    ).encode('utf-8')
    middle = len(body) // 2
    with socket.create_connection((LOCALHOST, store.port), timeout=60) as sock:
        sock.sendall(head + body[:middle])
        time.sleep(pause_s)
        sock.sendall(body[middle:])
        answer = b''
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            answer += chunk
    assert answer.startswith(b'HTTP/'), f'the store never answered the slow upload; it said {answer!r}'
    return int(answer.split()[1])


def _multipart_body(fields: dict[str, str], data: bytes) -> tuple[bytes, str]:
    """A ``multipart/form-data`` body built by hand, so a test can send it in pieces."""
    boundary = 'conformance-boundary'
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode('utf-8')
        for name, value in fields.items()
    ]
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="part"\r\n'
        f'Content-Type: application/octet-stream\r\n\r\n'.encode('utf-8')
        + data
        + b'\r\n'
    )
    parts.append(f'--{boundary}--\r\n'.encode('utf-8'))
    return b''.join(parts), f'multipart/form-data; boundary={boundary}'


@harness_self_test
@our_policy(_INSTRUMENT)
def test_the_endpoint_accepts_an_upload_inside_the_prefix(endpoint):
    """Setup: a live credential and a key under the launch's prefix. Action: post.
    Validate: accepted, and recorded with the right hash and size."""
    token = endpoint.mint()
    response = post(endpoint, token, PREFIX + 'outputs/a.csv', b'hello\n')

    assert response.status_code == 204, response.text
    upload = endpoint.uploaded('outputs/a.csv')
    assert upload is not None
    assert upload.sha256 == hashlib.sha256(b'hello\n').hexdigest()
    assert upload.size == 6


@harness_self_test
@our_policy(_INSTRUMENT)
def test_the_endpoint_refuses_a_key_outside_the_prefix(endpoint):
    """The fence exists; prove it stops something.

    Setup:    a live credential and a key in a DIFFERENT execution's prefix.
    Action:   post.
    Validate: 403, nothing recorded, and the refusal is in the ledger.
    """
    token = endpoint.mint()
    response = post(endpoint, token, 'conformance/pipelines/1/executions/999/attempts/1/gen-1/stolen.csv', b'x')

    assert response.status_code == 403, response.text
    assert endpoint.uploads == []
    assert [(r.kind, r.status, r.code) for r in endpoint.rejections] == [('upload', 403, 'AccessDenied')]


@harness_self_test
@our_policy(
    'A presigned POST is accepted by S3 only if the whole signed form arrives — policy, algorithm, '
    'credential, date and signature. This endpoint used to check the key prefix and one custom token and '
    'nothing else, so a step that forwarded the URL and dropped the rest would have passed here and been '
    'refused in production. ' + _INSTRUMENT
)
@pytest.mark.parametrize('dropped', ['policy', 'x-amz-signature', 'x-amz-algorithm', 'x-amz-credential'])
def test_an_upload_missing_a_signed_policy_field_is_refused(endpoint, dropped):
    """Setup: a valid upload with one required POST field removed. Action: post.
    Validate: 403, naming the field."""
    token = endpoint.mint()
    response = post(endpoint, token, PREFIX + 'outputs/a.csv', b'x', drop=(dropped,))

    assert response.status_code == 403, response.text
    assert dropped in response.text, response.text
    assert endpoint.uploads == []


@harness_self_test
@our_policy(_INSTRUMENT)
def test_an_upload_with_an_altered_signature_is_refused(endpoint):
    """Setup: a valid upload whose signature field has been edited. Action: post.
    Validate: 403."""
    token = endpoint.mint()
    policy = endpoint.post_policy(LOCALHOST, token, PREFIX)
    fields = dict(policy['fields'])
    fields['key'] = PREFIX + 'outputs/a.csv'
    fields['x-amz-signature'] = 'signature-for-somebody-else'

    response = requests.post(policy['url'], data=fields, files={'file': ('part', b'x')}, timeout=30)

    assert response.status_code == 403, response.text
    assert endpoint.uploads == []


@harness_self_test
@traces_to(
    'runners/credentials.py: an envelope carries "the moment all of that stops working" and expires in '
    'LSPO_RUNNER_CREDS_TTL_S. Nothing revokes a presigned URL before then — the issuer signs a deadline '
    'and cannot take it back — which is why this endpoint expires credentials and never revokes them.'
)
def test_a_credential_expires_but_is_not_revoked_by_a_newer_one(endpoint):
    """The correction at the heart of this round, proven on the instrument itself.

    Setup:    a credential with no expiry, used successfully; then a SECOND credential is
              issued; then a third that is already expired.
    Action:   use the first again after the second exists, and use the expired one.
    Validate: the first still works — issuing a new credential revoked nothing — and the
              expired one is refused.

    The previous version of this endpoint failed the first of those two assertions by
    design, and every rotation test in the suite was built on it. A step that cached a
    valid envelope was reported as broken; the fix slice would have been sent to write
    "re-read the credentials file before every single transfer", which nothing asks for.
    """
    first = endpoint.mint()
    endpoint.blobs['thing'] = Blob.of(b'bytes')
    assert requests.get(endpoint.get_url(LOCALHOST, 'thing', first), timeout=30).status_code == 200

    endpoint.mint()  # a fresh envelope is issued; the one in flight is untouched
    assert requests.get(endpoint.get_url(LOCALHOST, 'thing', first), timeout=30).status_code == 200
    assert post(endpoint, first, PREFIX + 'outputs/a.csv', b'x').status_code == 204

    dead = endpoint.mint(ttl_s=-1)
    assert requests.get(endpoint.get_url(LOCALHOST, 'thing', dead), timeout=30).status_code == 403
    assert post(endpoint, dead, PREFIX + 'outputs/b.csv', b'x').status_code == 403


@harness_self_test
@our_policy(
    'WHEN a store authorizes a request is a property of S3, and no orchestrator source describes it — so '
    'this is a modelling choice of this harness, and labelling it as a contract rule (which the previous '
    'version of this test did, under a citation about credentials EXPIRING) claimed an authority that '
    'does not exist. The choice: a presigned request is authorized ONCE, when it arrives, before its body '
    'has been read — which is what S3 does with the signature and policy in the request it receives. '
    'Judging expiry after the last byte instead un-authorizes a transfer that was legitimate when it '
    'began, and every rotation test built on that would be demanding a step retry a transfer nothing on '
    'the platform asks it to retry. ' + _INSTRUMENT
)
def test_a_credential_that_expires_while_the_bytes_arrive_still_uploads(endpoint):
    """Expiry DURING an upload, proven by sending the body across the expiry instant.

    Setup:    a credential good for one second, and a POST whose body is sent in two
              halves with a two-and-a-half second pause in the middle — so the credential
              is alive when the request line arrives and long dead before the last byte.
    Action:   post, slowly, over a raw socket.
    Validate: accepted and recorded — and, so this cannot pass for the wrong reason, that
              the credential really was live on arrival and really is dead now.

    **This test found a bug in this store, which is the reason it is written this way.**
    The version before it held the request open with a HOOK, and hooks run only after the
    whole multipart body has been read and parsed: it proved that a delay AFTER arrival
    changes nothing, which was never in question, while the store was in fact stamping a
    POST's arrival after its body — so a credential expiring mid-upload refused a transfer
    that had begun inside its lifetime. Sending the body itself across the expiry is the
    only way to ask the question the name claims to ask.
    """
    token = endpoint.mint(ttl_s=1.0)

    status = post_in_two_halves(endpoint, token, PREFIX + 'outputs/a.csv', b'x' * 64, pause_s=2.5)

    arrived_at = endpoint.requests[-1].arrived_at
    assert endpoint.is_live(token, at=arrived_at), 'the credential had already expired when the request arrived'
    assert not endpoint.is_live(token), 'the credential was supposed to expire while the body was arriving'
    assert status == 204, f'the store refused an upload that began inside its credential\'s life: HTTP {status}'
    assert endpoint.uploaded('outputs/a.csv') is not None


@harness_self_test
@our_policy(
    'A cancellation test reads this store only once settle() calls it quiet, and if settle() answered '
    'early that test would be back to the racy snapshot it was rebuilt to stop being — passing a step '
    'that wrote its inventory and left an object landing behind it. This is exactly the kind of helper '
    'that looks obviously right and can be silently wrong, so it is measured. ' + _INSTRUMENT
)
def test_the_store_calls_itself_busy_until_what_it_holds_is_answered(endpoint):
    """Setup:    an upload the store holds open for two seconds, posted from a thread.
    Action:   ask the store to settle, first with too short a deadline and then with a
              long enough one.
    Validate: the short call reports it is still busy; the long one waits out the upload
              and reports quiet, with the object recorded by then.
    """
    token = endpoint.mint()
    endpoint.hooks.on_request.append(delay_when(matching('upload', index=1), 2.0))
    uploader = threading.Thread(
        target=post, args=(endpoint, token, PREFIX + 'outputs/a.csv', b'held open'), daemon=True
    )
    uploader.start()
    try:
        assert endpoint.wait_for('upload', timeout=30), 'the upload never reached the store'

        assert endpoint.settle(timeout=0.2) is False, 'settle answered while a request was still in flight'
        assert endpoint.settle(timeout=30) is True, 'settle never saw the store go quiet'
        assert endpoint.uploaded('outputs/a.csv') is not None, 'the store went quiet without recording the upload'
    finally:
        uploader.join(timeout=30)


@harness_self_test
@our_policy(_INSTRUMENT)
def test_upload_order_is_arrival_order_and_a_re_upload_wins(endpoint):
    """Setup: three uploads in a known order, then a second write of one of them.
    Action: read the ledger. Validate: arrival order, not alphabetical — and the object
    that was written twice reads back as the SECOND write.

    Last-write-wins is what a real object store does, and this endpoint used to answer
    with the first write. A hash test could have blessed bytes that were replaced, or
    rejected a step that legitimately re-sent an object after a refused transfer.
    """
    token = endpoint.mint()
    for name in ('zebra.csv', 'apple.csv', 'mango.csv'):
        assert post(endpoint, token, PREFIX + name, b'x').status_code == 204

    assert endpoint.keys_in_order() == ['zebra.csv', 'apple.csv', 'mango.csv']
    assert [upload.order for upload in endpoint.uploads] == [1, 2, 3]

    assert post(endpoint, token, PREFIX + 'apple.csv', b'second write').status_code == 204
    assert endpoint.body_of('apple.csv') == b'second write'
    assert endpoint.uploaded('apple.csv').sha256 == hashlib.sha256(b'second write').hexdigest()
    assert endpoint.last_index('apple.csv') == 3


@harness_self_test
@our_policy(_INSTRUMENT)
def test_distinct_names_are_counted_separately_from_requests(endpoint):
    """Setup: one object fetched three times and another fetched once.
    Action: count. Validate: four requests, two distinct names.

    Two rotation tests and one cancellation test assert coverage of the inputs. Counting
    requests would let three retries of one input look like three inputs done.
    """
    token = endpoint.mint()
    endpoint.blobs['a'] = Blob.of(b'a')
    endpoint.blobs['b'] = Blob.of(b'b')
    for name in ('a', 'a', 'a', 'b'):
        requests.get(endpoint.get_url(LOCALHOST, name, token), timeout=30)

    assert endpoint.count_of('input') == 4
    assert endpoint.names_of('input') == ['a', 'b']


@harness_self_test
@traces_to(
    'agent/creds.py: "a new file is written beside it and os.replaced over it: a reader sees either the '
    'old document or the new one." This proves the harness reproduces that swap rather than truncating '
    'and rewriting, which would hand a reader half a document at exactly the wrong moment.'
)
def test_a_fresh_envelope_replaces_the_file_atomically(image, workdir):
    """The swap a reader must never catch half-done.

    Setup:    a job whose credentials are on disk, and an open file handle on them.
    Action:   publish a fresh envelope.
    Validate: the already-open handle still reads the OLD document in full; a fresh open
              reads the NEW one in full; and no temporary file is left behind.
    """
    job = Job(image=image, workdir=workdir / 'rot', inputs=[InputSpec(relpath='a.csv', data=b'a\n')])
    try:
        job.setup()
        creds_path = job.creds_dir / 'creds.json'
        first = json.loads(creds_path.read_text())

        with open(creds_path, 'rb') as pinned:
            job.endpoint.refresh()
            pinned.seek(0)
            still_old = json.loads(pinned.read().decode('utf-8'))

        second = json.loads(creds_path.read_text())

        assert still_old == first, 'the open handle saw the replacement'
        assert second != first, 'the file on disk did not change'
        assert second['staging']['post']['fields']['tok'] != first['staging']['post']['fields']['tok']
        assert list(job.creds_dir.iterdir()) == [creds_path], 'a temporary file was left behind'
    finally:
        job.teardown()


@harness_self_test
@our_policy(_INSTRUMENT)
def test_a_synthetic_blob_hashes_to_what_it_pins(endpoint):
    """The oversized-input test pins a blob nobody buffers; prove the pin is right.

    Setup:    a synthetic blob of 5 MiB.
    Action:   fetch it and hash what arrived.
    Validate: it matches the declared digest and length.
    """
    token = endpoint.mint()
    blob = Blob.synthetic(5 * 1024 * 1024)
    endpoint.blobs['big'] = blob

    body = requests.get(endpoint.get_url(LOCALHOST, 'big', token), timeout=60).content
    assert len(body) == blob.size
    assert hashlib.sha256(body).hexdigest() == blob.sha256


@harness_self_test
@our_policy(
    'The most dangerous bug this harness can have, so it gets its own test. Docker reports '
    'State.ExitCode as 0 for a container that is still running; the harness used to ignore a wait '
    'timeout and then report that 0, so a step that hung forever would have been recorded as a step '
    'that succeeded — and every verdict resting on "exit code and logs" would have been vacuous. '
    + _INSTRUMENT
)
def test_a_container_that_never_exits_is_never_reported_as_a_success(image):
    """Setup: a container that sleeps far longer than the wait. Action: wait for it,
    briefly, and collect it. Validate: waiting RAISES rather than returning, and the
    collected result says the exit code is unknown — not zero."""
    container = docker.start(
        image,
        name=f'lspo-conformance-hang-{os.getpid()}',
        env={},
        creds_dir=None,
        entrypoint='python',
        command=('-c', 'import time; print("working", flush=True); time.sleep(120)'),
    )
    try:
        with pytest.raises(docker.ContainerDidNotExit):
            container.wait(timeout=3)

        result = container.collect()
        assert result.still_running is True
        assert result.exit_code is None, f'a running container reported exit code {result.exit_code!r}'
        assert 'working' in result.output
    finally:
        container.remove()


@harness_self_test
@our_policy(
    'The cancellation tests read a container\'s fate from three docker calls, and a harness that let any '
    'of the three fail quietly would report an infrastructure fault as a step\'s behaviour. `docker stop` '
    'failing outright is the dangerous one: it returns in a fraction of a second having done nothing, '
    'which is indistinguishable from a step that shut down instantly — short elapsed time, no marker, no '
    'further work — and those tests bypass wait(), so the hung-container self-test does not cover them. '
    + _INSTRUMENT
)
def test_a_container_that_disappeared_is_never_read_as_a_result(image):
    """Every way the harness asks about a container must raise when the answer is gone.

    Setup:    a long-running container, removed out from under the harness.
    Action:   inspect it, wait for it, collect it, and stop it.
    Validate: all four raise ``ContainerVanished`` rather than answering.

    ``inspect`` used to map EVERY non-zero ``docker inspect`` onto ``exit_code=None`` and
    ``stop`` used to discard its return code entirely, so a daemon fault could arrive at
    a test as an ordinary result — and in the cancellation tests, as a PASS.
    """
    container = docker.start(
        image,
        name=f'lspo-conformance-vanish-{os.getpid()}',
        env={},
        creds_dir=None,
        entrypoint='python',
        command=('-c', 'import time; time.sleep(120)'),
    )
    container.remove()

    answered = []
    for description, call in (
        ('inspect', container.inspect),
        ('wait', lambda: container.wait(timeout=3)),
        ('collect', container.collect),
        ('stop', lambda: container.stop(grace=1)),
    ):
        try:
            call()
        except docker.ContainerVanished:
            continue
        answered.append(description)
    assert not answered, f'{answered} answered about a container that is gone instead of raising'


@harness_self_test
@our_policy(
    'Two credential tests rest entirely on this docker mechanism, so it is proven rather than assumed. '
    + _INSTRUMENT
)
def test_docker_really_drops_an_environment_variable_the_image_baked_in(image):
    """Setup:    this image bakes ``ENV LSPO_CREDENTIALS=/lspo/creds/creds.json``.
    Action:   run it with ``--env LSPO_CREDENTIALS`` and no value, and print the
              environment.
    Validate: the variable is absent — not empty, absent.

    ``-e NAME=`` would set it to the empty string, which a step could read as "set but
    blank" and behave differently about. Only the valueless form removes it, and "the
    variable is genuinely not there" is the case the contract's default path exists for.
    """
    baked = _env_in_container(image, name='lspo-conformance-env-baked', unset=())
    assert baked.get(contract.LEGACY_CREDENTIALS_ENV) == contract.DEFAULT_CREDENTIALS_FILE

    stripped = _env_in_container(image, name='lspo-conformance-env-stripped', unset=(contract.LEGACY_CREDENTIALS_ENV,))
    assert contract.LEGACY_CREDENTIALS_ENV not in stripped, f'it survived as {stripped.get(contract.LEGACY_CREDENTIALS_ENV)!r}'


@harness_self_test
@traces_to(
    'external/versioning.py: "The golden wire-format tests (external/tests/test_golden_v1.py, backed by '
    'the checked-in fixtures/golden_v1_*.json) are the tripwire: any change to the V1 shape turns them '
    'red, so this cannot happen by accident." Those same three documents are checked in here.'
)
def test_this_harness_agrees_with_the_orchestrators_own_frozen_documents():
    """The one measurement of "my re-implementation matches the real parser".

    Setup:    the orchestrator's three golden version-1 documents, copied byte for byte
              into ``conformance/fixtures/``.
    Action:   validate each with this harness's own re-implementation of the contract.
    Validate: all three are accepted.

    ``conformance/contract.py`` deliberately does not import the orchestrator's parser —
    a harness that shares code with the system it judges inherits its bugs. The price of
    that is drift, and this is what turns "they agree" from a claim into a check: the
    golden manifest carries a ``layout='prefix'`` port with a real ``prefix_digest``, an
    ``organization_id``, ``upstream_execution_id`` on its objects and a ``null``
    ``prefix_digest`` on a file port — every one of which this harness's validator ignored
    entirely until this round.
    """
    manifest = json.loads((FIXTURES / 'golden_v1_manifest.json').read_bytes())
    marker = json.loads((FIXTURES / 'golden_v1_marker.json').read_bytes())
    result = json.loads((FIXTURES / 'golden_v1_result.json').read_bytes())

    contract.validate_manifest(manifest, raw_bytes=(FIXTURES / 'golden_v1_manifest.json').read_bytes())
    contract.validate_marker(marker, raw_bytes=(FIXTURES / 'golden_v1_marker.json').read_bytes())
    contract.validate_result(result, raw_bytes=(FIXTURES / 'golden_v1_result.json').read_bytes())

    # The parts this harness's own traffic never produces, so the fixture is doing real work.
    prefix_port = next(port for port in manifest['inputs'] if port['layout'] == 'prefix')
    assert prefix_port['prefix_digest'] and all(obj.get('relpath') for obj in prefix_port['objects'])
    assert manifest['organization_id'] and len(marker['produced_ports']) == 2


@harness_self_test
@our_policy(
    'A harness that hands the node an invalid job description proves nothing about the node. This checks '
    'the harness\'s manifest against the harness\'s own copy of the contract — which is only worth '
    'anything because the test above measures that copy against the orchestrator\'s frozen documents. '
    + _INSTRUMENT
)
def test_the_harness_writes_a_manifest_this_copy_of_the_contract_accepts(image, workdir):
    """Setup:    a job with two ports, one nested relpath and awkward characters.
    Action:   build its manifest and validate it.
    Validate: accepted — including the rule that the staging prefix must encode the same
              attempt and generation the manifest claims.
    """
    job = Job(
        image=image,
        workdir=workdir / 'manifest',
        inputs=[
            InputSpec(relpath='a/b.csv', data=b'x', port='left'),
            InputSpec(relpath='100%-done.csv', data=b'y', port='right'),
        ],
        attempt=4,
        generation=7,
    )
    raw = json.dumps(job.manifest(), sort_keys=True).encode('utf-8')
    contract.validate_manifest(json.loads(raw), raw_bytes=raw)
    assert job.staging_prefix.endswith('attempts/4/gen-7/')


@harness_self_test
@our_policy(
    'The harness must inject what the agent injects and nothing it does not — above all not the legacy '
    'variable, or a node could pass the credentials tests by reading a name the agent never sets. '
    + _INSTRUMENT
)
def test_the_harness_injects_exactly_the_variables_the_agent_always_sets(image, workdir):
    """Setup: a job. Action: read the environment it would inject. Validate: it is the
    fixed set the agent always sets, and it does not include the legacy name.

    "Exactly these" is right for the HARNESS even though it is wrong for the agent: a job
    here declares no ``env_names``, so the fixed set is the whole environment. The agent's
    real rule — these plus what the manifest asked for and the operator allowed — is
    tested in ``test_platform_rules.py``.
    """
    job = Job(image=image, workdir=workdir / 'env', inputs=[])
    injected = job.injected_env()

    assert sorted(injected) == sorted(contract.INJECTED_ENV)
    assert contract.LEGACY_CREDENTIALS_ENV not in injected


@harness_self_test
@our_policy(
    'The citation on a basis_contract test is the whole of its authority, and until this round the only '
    'thing checked was that it mentioned an authoritative FILE — which a paraphrase, a stale quotation or '
    'no quotation at all passes just as easily. This checks every quoted fragment against the platform\'s '
    'real sources. What it CANNOT check is whether the sentence supports the claim; every over-claim '
    'corrected in this harness so far cited a real file and quoted a real sentence, so that judgement '
    'stays with a reviewer. ' + _INSTRUMENT
)
def test_every_contract_citation_quotes_the_platform_verbatim():
    """The one check that measures a citation against the thing it cites.

    Setup:    ``LSPO_ORCHESTRATOR_SRC`` pointing at an orchestrator checkout — optionally
              with ``LSPO_ORCHESTRATOR_REF`` to read a git ref rather than the working
              tree. Without it this test skips: this repository is standalone and does
              not vendor the platform, and a check that passed because it had nothing to
              read would be worse than none.
    Action:   read every citation written in ``tests/`` straight out of the source, and
              look each quoted fragment up in the files that citation names.
    Validate: every fragment occurs, ignoring case, wrapping and Sphinx markup.

    **Read from the files, not from the collected tests**, and the difference is not
    academic: the first version of this walked the session's items, so running it on its
    own — the obvious way to run it — checked the single test that had been selected and
    passed. A check that is weaker the more precisely you aim it is not a check.

    A fragment that has gone missing means one of two things, and both need a human: the
    platform reworded a rule (so the restatement may now be wrong), or the citation was
    never a quotation in the first place.
    """
    read_source = citations.source_reader()
    if read_source is None:
        pytest.skip(
            f'set {citations.SRC_ENV} to an orchestrator checkout (and optionally {citations.REF_ENV} to '
            f'a git ref) to check every quoted rule against the real sources. Unset, this harness can '
            f'only check that a citation NAMES an authoritative file and quotes something rule-length, '
            f'which collection already enforces'
        )
    written = citations.citations_in((TESTS_DIR).glob('test_*.py'))
    assert len(written) > 20, f'only {len(written)} citations were found; this check is not reading the suite'

    problems: list[str] = []
    for where, citation in written:
        if not citation:
            problems.append(f'{where}: the citation is not a plain string, so it cannot be checked')
            continue
        problems += [f'{where}: {problem}' for problem in citations.verbatim_problems(citation, read_source)]
    assert not problems, 'citations that do not quote the platform verbatim:\n  ' + '\n  '.join(problems)


def _env_in_container(image: str, *, name: str, unset: tuple[str, ...]) -> dict[str, str]:
    container = docker.start(
        image,
        name=f'{name}-{os.getpid()}',
        env={},
        unset_env=unset,
        creds_dir=None,
        entrypoint='python',
        command=('-c', 'import os,json; print(json.dumps(dict(os.environ)))'),
    )
    try:
        container.wait(timeout=60)
        result = container.collect()
        return json.loads(result.stdout.strip().splitlines()[-1])
    finally:
        container.remove()
