#!/usr/bin/env python3
"""hello-node — the smallest program that is a real LSPO external step.

It reads its inputs, VERIFIES each one against the hash the orchestrator pinned for it,
counts their lines and bytes, copies each one into its output area, writes a small
report, and finishes by writing the completion marker.

**This file deliberately imports nothing from the orchestrator.** Copy it into your
own repository and start editing: the contract is four JSON documents and a directory,
not a library. The only third-party dependency is ``requests``, and only for the S3
mode.

What the orchestrator gives you
-------------------------------

``LSPO_CREDENTIALS`` points at a JSON file the agent mounts read-only. It carries a
short-lived way to read your inputs and write your outputs, and nothing else::

    {
      "schema_version": 1,
      "scheme": "s3",                      # or "local" in single-host demo mode
      "expires_at": "2026-08-07T12:00:00+00:00",
      "manifest_get": "https://…",         # ("manifest_path": "/…" when local)
      "inputs": [{"name": …, "relpath": …, "sha256": …, "size": …, "get_url": …}],
      "staging": {"mode": "presigned_post",
                  "post": {"url": …, "fields": {…}, "key_prefix": "…/"}}
    }

The manifest (``invocation.json``) describes the job: your ``params``, the pinned
input objects, the attempt and generation, the runtime budget.

What you must give back
-----------------------

Anything you like, under your staging prefix, and then ``__lspo_complete.json``:

    {"schema_version": 1, "execution_id": …, "attempt": …, "generation": …,
     "status": "succeeded", "exit_code": 0,
     "objects":        [{"relpath": "outputs/x.csv", "sha256": "…", "size": 123}],
     "produced_ports": {"output": ["outputs/x.csv"]}}

**Write the marker LAST.** Its existence is the orchestrator's proof that everything
it lists is already readable — write it early and a half-finished run is
indistinguishable from a complete one. Every object you name in ``produced_ports``
must also appear in ``objects``, with its real hash and size: collection re-reads
every one of them and refuses to publish anything if a single hash disagrees.

Exit codes: 0 succeeded, 1 retry me, 10 do not retry, 20 cancelled.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from urllib.parse import urlparse

MARKER_FILENAME = '__lspo_complete.json'
RESULT_FILENAME = 'result.json'
OUTPUT_PORT = 'output'
OUTPUT_DIR = 'outputs'

EXIT_OK = 0
EXIT_TRANSIENT = 1
EXIT_PERMANENT = 10


class StepError(Exception):
    """Something the step cannot recover from; the marker will say ``failed``."""


# --------------------------------------------------------------------------- io


def read_bytes(source: dict) -> bytes:
    """Fetch one input and CHECK IT against the hash the orchestrator pinned.

    Every input arrives with a ``sha256`` and a ``size``: the orchestrator hashed the
    object when it built this job, and those two numbers are its statement about what
    your step is supposed to be reading. Verifying them is not ceremony — the URL is
    presigned and points at a bucket, and "the bytes at that key today" is not
    automatically "the bytes that were there when the job was created". A step that
    skips this can silently process the wrong version of its input and produce output
    that passes every check the orchestrator makes on the way back, because those
    checks are about what you WROTE, not about what you READ.

    A missing pin is refused for the same reason: an input nobody can verify is an
    input nobody should act on.
    """
    if source.get('get_url'):
        data = _http_get(source['get_url'])
    else:
        path = source.get('local_path')
        if not path:
            raise StepError(f'input {source.get("name")!r} has neither a get_url nor a local_path')
        with open(path, 'rb') as handle:
            data = handle.read()
    _check_pin(source, data)
    return data


def _check_pin(source: dict, data: bytes) -> None:
    """Hold one fetched input to the size and digest the job pinned for it."""
    name = source.get('relpath') or source.get('name') or 'input'
    expected_sha = source.get('sha256')
    if not expected_sha:
        raise StepError(f'input {name!r} arrived with no sha256 pin; refusing to work on unverified bytes')
    expected_size = source.get('size')
    if isinstance(expected_size, int) and len(data) != expected_size:
        raise StepError(f'input {name!r} is {len(data)} bytes, but the job pinned {expected_size}')
    actual = _sha256(data)
    if actual != expected_sha:
        raise StepError(
            f'input {name!r} hashes to {actual}, but the job pinned {expected_sha} — '
            f'this is not the object this run was built from'
        )


def write_output(staging: dict, relpath: str, data: bytes) -> None:
    """Write one object into the job's staging area.

    Every write in this program goes through this ONE function — which is what makes
    "the marker is written last" a property of the code rather than a hope, and what
    lets a test record the order things were written in.
    """
    if staging['mode'] == 'local_path':
        destination = os.path.join(staging['path'], relpath)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, 'wb') as handle:
            handle.write(data)
        return
    _post_object(staging['post'], relpath, data)


def _http_get(url: str) -> bytes:
    import requests

    response = requests.get(url, timeout=120)
    response.raise_for_status()
    return response.content


def _post_object(post: dict, relpath: str, data: bytes) -> None:
    """Upload via the presigned POST policy.

    ``key`` is the full object key: the policy's ``starts-with`` condition means S3
    itself refuses anything outside this job's prefix, so the prefix is prepended
    here rather than trusted to be implied.
    """
    import requests

    fields = dict(post['fields'])
    fields['key'] = post['key_prefix'] + relpath
    response = requests.post(post['url'], data=fields, files={'file': (relpath, data)}, timeout=300)
    if response.status_code not in (200, 201, 204):
        raise StepError(f'upload of {relpath!r} was refused with HTTP {response.status_code}: {response.text[:300]}')


def read_manifest(envelope: dict) -> dict:
    """The job description, from wherever this envelope says it lives."""
    if envelope.get('manifest_get'):
        return json.loads(_http_get(envelope['manifest_get']).decode('utf-8'))
    path = envelope.get('manifest_path')
    if not path:
        raise StepError('the credential envelope names neither manifest_get nor manifest_path')
    with open(path, 'rb') as handle:
        return json.loads(handle.read().decode('utf-8'))


def load_envelope() -> dict:
    """Read the credentials file the agent mounted for this job."""
    path = os.environ.get('LSPO_CREDENTIALS')
    if not path:
        raise StepError('LSPO_CREDENTIALS is not set; the agent did not mount a credentials file')
    with open(path, 'rb') as handle:
        return json.loads(handle.read().decode('utf-8'))


# ------------------------------------------------------------------------- work


def process(envelope: dict, manifest: dict) -> tuple[list[dict], dict]:
    """Copy every input into ``outputs/`` and count what went past.

    Returns ``(objects, metrics)`` where ``objects`` is the inventory the marker
    needs: for each thing written, its relative path, its sha256 and its size —
    computed from the bytes we actually wrote, never assumed.
    """
    staging = envelope['staging']
    objects: list[dict] = []
    total_bytes = 0
    total_lines = 0

    for source in envelope.get('inputs') or []:
        data = read_bytes(source)
        name = source.get('relpath') or source.get('name') or _basename(source.get('uri', 'input'))
        relpath = f'{OUTPUT_DIR}/{name}'
        write_output(staging, relpath, data)
        objects.append({'relpath': relpath, 'sha256': _sha256(data), 'size': len(data)})
        total_bytes += len(data)
        total_lines += data.count(b'\n')

    result = {
        'schema_version': 1,
        'metrics': {
            'files': len(objects),
            'total_bytes': total_bytes,
            'lines': total_lines,
        },
        'summary': {
            'params': manifest.get('params') or {},
            'attempt': manifest.get('attempt'),
        },
    }
    payload = _dump(result)
    write_output(staging, RESULT_FILENAME, payload)
    objects.append({'relpath': RESULT_FILENAME, 'sha256': _sha256(payload), 'size': len(payload)})
    return objects, result['metrics']


def write_marker(envelope: dict, manifest: dict, objects: list[dict], *, status: str, error: str | None) -> None:
    """The terminal receipt. ALWAYS the last thing this program writes.

    It states its own identity — execution, attempt, generation — so the orchestrator
    can tell a receipt for THIS run from one a superseded copy of the job left behind.
    """
    marker = {
        'schema_version': 1,
        'execution_id': manifest.get('execution_id'),
        'attempt': manifest.get('attempt'),
        'generation': manifest.get('generation'),
        'idempotency_key': manifest.get('idempotency_key'),
        'status': status,
        'exit_code': EXIT_OK if status == 'succeeded' else EXIT_PERMANENT,
        'objects': objects,
        'produced_ports': {OUTPUT_PORT: [obj['relpath'] for obj in objects]} if status == 'succeeded' else {},
        'error': error,
    }
    write_output(envelope['staging'], MARKER_FILENAME, _dump(marker))


def main() -> int:
    """Run the step. Never raises: every failure becomes a marker plus an exit code."""
    envelope = load_envelope()
    manifest = read_manifest(envelope)
    print(f'hello-node: execution {manifest.get("execution_id")} attempt {manifest.get("attempt")}', flush=True)
    try:
        objects, metrics = process(envelope, manifest)
    except Exception as failure:
        # A failed run still writes a marker: the orchestrator's alternative is to
        # infer what happened from an exit code, and "the step said why" is a much
        # better failure report than "the process died".
        print(f'hello-node: FAILED: {failure}', file=sys.stderr, flush=True)
        try:
            write_marker(envelope, manifest, [], status='failed', error=str(failure)[:1000])
        except Exception as marker_failure:  # nothing left to do but say so
            print(f'hello-node: could not write the failure marker: {marker_failure}', file=sys.stderr, flush=True)
        return EXIT_PERMANENT if isinstance(failure, StepError) else EXIT_TRANSIENT

    write_marker(envelope, manifest, objects, status='succeeded', error=None)
    print(f'hello-node: done — {metrics["files"]} file(s), {metrics["total_bytes"]} bytes', flush=True)
    return EXIT_OK


# ---------------------------------------------------------------------- helpers


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dump(document: dict) -> bytes:
    return json.dumps(document, sort_keys=True, indent=2).encode('utf-8')


def _basename(uri: str) -> str:
    return os.path.basename(urlparse(uri).path) or 'input'


if __name__ == '__main__':
    sys.exit(main())
