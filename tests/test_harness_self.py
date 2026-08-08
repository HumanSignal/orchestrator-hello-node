"""Proving the instrument before trusting its readings.

Every "the node fails this" verdict in this suite is only as trustworthy as the fake
store that produced it. If the endpoint accepted everything, if rotation did not rotate,
or if upload order were an artefact of dictionary iteration, the whole baseline would be
fiction. These tests hold the harness to the three claims the rest of the suite leans on:

* the prefix fence and the credential check really refuse;
* a rotation really replaces the file, atomically, and a reader really sees the change;
* the order objects arrive in is really recorded, and really is arrival order.

They talk to the endpoint directly, with an HTTP client, so a bug in ``Job`` cannot make
them pass.
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest
import requests

from conformance import contract, docker
from conformance.fakes3 import Blob, Endpoint
from conformance.job import InputSpec, Job
from conformance.markers import harness_self_test

PREFIX = 'conformance/pipelines/1/executions/2/attempts/1/gen-1/'
LOCALHOST = '127.0.0.1'


@pytest.fixture
def endpoint():
    store = Endpoint().start()
    try:
        yield store
    finally:
        store.stop()


def post(store: Endpoint, token: str, key: str, data: bytes) -> requests.Response:
    policy = store.post_policy(LOCALHOST, token, PREFIX)
    fields = dict(policy['fields'])
    fields['key'] = key
    return requests.post(policy['url'], data=fields, files={'file': ('part', data)}, timeout=30)


@harness_self_test
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
def test_the_endpoint_refuses_a_key_outside_the_prefix(endpoint):
    """The fence exists; prove it stops something.

    Setup:    a live credential and a key in a DIFFERENT execution's prefix.
    Action:   post.
    Validate: 403, nothing recorded, and the refusal is in the ledger.

    Without this test, "no object was ever written outside the prefix" would be equally
    consistent with an endpoint that never checks.
    """
    token = endpoint.mint()
    response = post(endpoint, token, 'conformance/pipelines/1/executions/999/attempts/1/gen-1/stolen.csv', b'x')

    assert response.status_code == 403, response.text
    assert endpoint.uploads == []
    assert [(r.kind, r.status, r.code) for r in endpoint.rejections] == [('upload', 403, 'AccessDenied')]


@harness_self_test
def test_a_superseded_credential_is_refused(endpoint):
    """Setup: mint a credential, then mint another. Action: use the first.
    Validate: 403 — which is what makes every rotation test falsifiable."""
    old = endpoint.mint()
    endpoint.blobs['thing'] = Blob.of(b'bytes')
    assert requests.get(endpoint.get_url(LOCALHOST, 'thing', old), timeout=30).status_code == 200

    endpoint.mint()
    refused = requests.get(endpoint.get_url(LOCALHOST, 'thing', old), timeout=30)
    assert refused.status_code == 403
    assert post(endpoint, old, PREFIX + 'outputs/a.csv', b'x').status_code == 403


@harness_self_test
def test_upload_order_is_arrival_order(endpoint):
    """Setup: three uploads, posted in a known order, with names that sort differently.
    Action: read the ledger. Validate: it is arrival order, not alphabetical."""
    token = endpoint.mint()
    for name in ('zebra.csv', 'apple.csv', 'mango.csv'):
        assert post(endpoint, token, PREFIX + name, b'x').status_code == 204

    assert endpoint.keys_in_order() == ['zebra.csv', 'apple.csv', 'mango.csv']
    assert [upload.order for upload in endpoint.uploads] == [1, 2, 3]


@harness_self_test
def test_a_rotation_replaces_the_file_atomically(image, workdir):
    """The swap a reader must never catch half-done.

    Setup:    a job whose credentials are on disk, and an open file handle on them.
    Action:   rotate.
    Validate: the already-open handle still reads the OLD document in full; a fresh
              open reads the NEW one in full; and no partial document exists at any
              point, because the new bytes were written to a different file first.

    This is ``os.replace`` semantics, and it is the reason the agent mounts the
    directory rather than the file. It is also the reason a reader can never see a
    truncated envelope: there is no moment at which the name points at an incomplete
    file.
    """
    job = Job(image=image, workdir=workdir / 'rot', inputs=[InputSpec(relpath='a.csv', data=b'a\n')])
    try:
        job.setup()
        creds_path = job.creds_dir / 'creds.json'
        first = json.loads(creds_path.read_text())

        with open(creds_path, 'rb') as pinned:
            job.endpoint.rotate()
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
def test_docker_really_drops_an_environment_variable_the_image_baked_in(image):
    """Two credential tests rest entirely on this mechanism; prove it works.

    Setup:    this image bakes ``ENV LSPO_CREDENTIALS=/lspo/creds/creds.json``.
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
def test_the_harness_writes_a_manifest_the_contract_accepts(image, workdir):
    """A harness that hands the node an invalid job description proves nothing about it.

    Setup:    a job with two ports, one nested relpath and awkward characters.
    Action:   build its manifest and validate it against the contract.
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
def test_the_harness_injects_exactly_the_nine_variables(image, workdir):
    """Setup: a job. Action: read the environment it would inject. Validate: it is the
    nine the agent injects, no more and no fewer — and it does not include the legacy
    name, so a node cannot pass by reading something the agent never sets."""
    job = Job(image=image, workdir=workdir / 'env', inputs=[])
    injected = job.injected_env()

    assert sorted(injected) == sorted(contract.INJECTED_ENV)
    assert contract.LEGACY_CREDENTIALS_ENV not in injected


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
