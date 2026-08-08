"""The wire contract, re-implemented here from the specification rather than imported.

The orchestrator has its own copy of these rules (``external/contract.py``). This file
deliberately does NOT vendor it. A conformance harness that shares code with the thing
it is judging inherits that thing's bugs and stops being able to detect them: if the
orchestrator's parser ever grows lax about, say, an empty path component, a vendored
copy would go lax at the same moment and the node would pass a test it should fail.

So these are the rules as WRITTEN DOWN, and any disagreement between this file and the
orchestrator's parser is itself a finding worth reporting.

The rules, in one place:

* Filenames inside a run's staging prefix are fixed by the contract, not configured:
  ``invocation.json``, ``result.json``, ``logs.ndjsonl``, ``__lspo_complete.json``.
* Credentials default to ``/lspo/creds/creds.json``; the envelope and the manifest may
  both name a different path, and the agent mounts the DIRECTORY that path lives in.
* Ceilings: manifest 8 MiB, marker 8 MiB, result 1 MiB. These bound reads AND writes —
  a document over the ceiling is one the other side cannot read.
* Exit codes: 0 ok, 1 transient, 10 permanent, 20 cancelled, 30 contention. Anything
  else is transient, because a crash or an OOM kill is far more often an accident than
  a considered "never retry me".
* A sha256 is exactly 64 lowercase hex characters, with no ``sha256:`` prefix and no
  trailing newline.
* A relpath is relative and canonical: no leading ``/``, no backslash, no line break,
  no ``.`` or ``..`` component, and no empty component (so ``a//b`` and a trailing
  ``/`` are both refused).
* In a completion marker every relpath in ``produced_ports`` must also appear in
  ``objects``; a relpath may not repeat within one port; the same relpath MAY appear
  in two different ports; and ``objects`` may not inventory one relpath twice. An
  object claimed by no port at all is normal and allowed.
"""

from __future__ import annotations

import re

MANIFEST_FILENAME = 'invocation.json'
RESULT_FILENAME = 'result.json'
LOGS_FILENAME = 'logs.ndjsonl'
MARKER_FILENAME = '__lspo_complete.json'

DEFAULT_CREDENTIALS_FILE = '/lspo/creds/creds.json'

MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_MARKER_BYTES = 8 * 1024 * 1024
MAX_RESULT_BYTES = 1024 * 1024

EXIT_OK = 0
EXIT_TRANSIENT = 1
EXIT_PERMANENT = 10
EXIT_CANCELLED = 20
EXIT_CONTENTION = 30

CLASS_OK = 'ok'
CLASS_TRANSIENT = 'transient'
CLASS_PERMANENT = 'permanent'
CLASS_CANCELLED = 'cancelled'
CLASS_CONTENTION = 'contention'

_EXIT_CLASSES = {
    EXIT_OK: CLASS_OK,
    EXIT_TRANSIENT: CLASS_TRANSIENT,
    EXIT_PERMANENT: CLASS_PERMANENT,
    EXIT_CANCELLED: CLASS_CANCELLED,
    EXIT_CONTENTION: CLASS_CONTENTION,
}

#: The nine variables the agent injects into every workload container. The node is
#: entitled to all nine and to nothing else — anything further is the operator's
#: allowlist, not the orchestrator's decision.
INJECTED_ENV = (
    'LSPO_JOB_ID',
    'LSPO_EXECUTION_ID',
    'LSPO_ATTEMPT',
    'LSPO_GENERATION',
    'LSPO_INVOCATION_URI',
    'LSPO_STAGING_PREFIX',
    'LSPO_CREDENTIALS_FILE',
    'LSPO_IDEMPOTENCY_KEY',
    'LSPO_CONTRACT_VERSION',
)

#: The name this node reads instead. Nothing on the orchestrator's side sets it; the
#: node's own Dockerfile does, which is the only reason the node runs at all today.
LEGACY_CREDENTIALS_ENV = 'LSPO_CREDENTIALS'

#: The opt-in progress line prefix. The trailing space is part of it.
PROGRESS_PREFIX = '@lspo:progress '

SHA256_PATTERN = re.compile(r'[0-9a-f]{64}')


class ContractViolation(AssertionError):
    """A document that the orchestrator's parser would refuse."""


def classify_exit(code: int | None) -> str:
    """Map a process exit code onto its contract class; unknown codes are transient."""
    if code is None:
        return CLASS_TRANSIENT
    return _EXIT_CLASSES.get(code, CLASS_TRANSIENT)


def check_sha256(value: object, where: str) -> str:
    """Reject anything that is not a lowercase 64-character hex digest."""
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise ContractViolation(f'{where}: {value!r} is not a lowercase 64-character hex sha256 digest')
    return value


def check_relpath(value: object, where: str) -> str:
    """Reject anything that is not a canonical relative path."""
    if not isinstance(value, str) or not value:
        raise ContractViolation(f'{where}: relpath must be a non-empty string, got {value!r}')
    if value.startswith('/'):
        raise ContractViolation(f'{where}: relpath {value!r} must be relative, not absolute')
    if '\\' in value:
        raise ContractViolation(f'{where}: relpath {value!r} must use "/" separators, not "\\"')
    if '\n' in value or '\r' in value:
        raise ContractViolation(f'{where}: relpath {value!r} must not contain a line break')
    parts = value.split('/')
    if any(part in ('.', '..') for part in parts):
        raise ContractViolation(f'{where}: relpath {value!r} must not contain "." or ".." components')
    if any(part == '' for part in parts):
        raise ContractViolation(f'{where}: relpath {value!r} must not contain an empty path component')
    return value


def check_size(value: object, where: str) -> int:
    """A byte count is a real non-negative ``int`` — not ``True``, not ``'12'``."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ContractViolation(f'{where}: size must be a non-negative int, got {value!r}')
    return value


def check_positive_int(value: object, where: str) -> int:
    """An id or a 1-based counter. Zero is what an uninitialised variable looks like."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ContractViolation(f'{where}: expected an int >= 1, got {value!r}')
    return value


def _duplicates(values) -> list[str]:
    seen: set[str] = set()
    duplicated: set[str] = set()
    for value in values:
        if value in seen:
            duplicated.add(value)
        seen.add(value)
    return sorted(duplicated)


def validate_marker(document: object, *, raw_bytes: bytes | None = None) -> dict:
    """Hold a completion marker to every rule the orchestrator's collector applies.

    Returns the document. Raises :class:`ContractViolation` naming the first rule it
    breaks — the harness reports that sentence verbatim, because "which rule" is the
    whole content of the finding.
    """
    if raw_bytes is not None and len(raw_bytes) > MAX_MARKER_BYTES:
        raise ContractViolation(f'the marker is {len(raw_bytes)} bytes, over the {MAX_MARKER_BYTES}-byte ceiling')
    if not isinstance(document, dict):
        raise ContractViolation(f'the marker must be a JSON object, got {type(document).__name__}')

    if document.get('schema_version') != 1:
        raise ContractViolation(f'marker schema_version must be 1, got {document.get("schema_version")!r}')
    check_positive_int(document.get('execution_id'), 'marker.execution_id')
    check_positive_int(document.get('attempt'), 'marker.attempt')
    check_positive_int(document.get('generation'), 'marker.generation')

    key = document.get('idempotency_key')
    if key is not None and (not isinstance(key, str) or not key.strip()):
        raise ContractViolation(f'marker.idempotency_key must be a non-blank string or absent, got {key!r}')

    status = document.get('status')
    if status not in ('succeeded', 'failed', 'cancelled'):
        raise ContractViolation(f'marker.status must be succeeded/failed/cancelled, got {status!r}')

    exit_code = document.get('exit_code')
    if exit_code is not None and (not isinstance(exit_code, int) or isinstance(exit_code, bool)):
        raise ContractViolation(f'marker.exit_code must be an int or null, got {exit_code!r}')

    objects = document.get('objects', [])
    if not isinstance(objects, list):
        raise ContractViolation(f'marker.objects must be a list, got {type(objects).__name__}')
    for index, obj in enumerate(objects):
        if not isinstance(obj, dict):
            raise ContractViolation(f'marker.objects[{index}] must be an object, got {obj!r}')
        check_relpath(obj.get('relpath'), f'marker.objects[{index}]')
        check_sha256(obj.get('sha256'), f'marker.objects[{index}].sha256')
        check_size(obj.get('size'), f'marker.objects[{index}].size')

    duplicated = _duplicates(obj['relpath'] for obj in objects)
    if duplicated:
        raise ContractViolation(
            f'duplicate relpath(s) {duplicated} in the objects inventory — one file, one entry, '
            f'because two entries could carry two different hashes for the same path'
        )

    ports = document.get('produced_ports', {})
    if not isinstance(ports, dict):
        raise ContractViolation(f'marker.produced_ports must be an object, got {type(ports).__name__}')
    known = {obj['relpath'] for obj in objects}
    for port, relpaths in ports.items():
        if not isinstance(relpaths, list):
            raise ContractViolation(f'produced_ports[{port!r}] must be a list, got {type(relpaths).__name__}')
        seen: set[str] = set()
        for relpath in relpaths:
            check_relpath(relpath, f'produced_ports[{port!r}]')
            if relpath not in known:
                raise ContractViolation(
                    f'produced_ports[{port!r}] references {relpath!r}, which is absent from the objects '
                    f'inventory — every delivered relpath must carry a hash and a size'
                )
            if relpath in seen:
                raise ContractViolation(f'produced_ports[{port!r}] lists {relpath!r} more than once')
            seen.add(relpath)

    error = document.get('error')
    if error is not None and not isinstance(error, str):
        raise ContractViolation(f'marker.error must be a string or null, got {error!r}')
    return document


def validate_result(document: object, *, raw_bytes: bytes | None = None) -> dict:
    """Hold ``result.json`` to its shape and to the 1 MiB ceiling."""
    if raw_bytes is not None and len(raw_bytes) > MAX_RESULT_BYTES:
        raise ContractViolation(
            f'result.json is {len(raw_bytes)} bytes, over the {MAX_RESULT_BYTES}-byte ceiling — '
            f'result.json carries metrics and a summary, and anything approaching a megabyte '
            f'there is payload in the wrong place'
        )
    if not isinstance(document, dict):
        raise ContractViolation(f'result.json must be a JSON object, got {type(document).__name__}')
    if document.get('schema_version') != 1:
        raise ContractViolation(f'result.json schema_version must be 1, got {document.get("schema_version")!r}')
    for field in ('metrics', 'summary'):
        value = document.get(field, {})
        if not isinstance(value, dict):
            raise ContractViolation(f'result.{field} must be an object, got {type(value).__name__}')
    return document


def validate_manifest(document: object, *, raw_bytes: bytes | None = None) -> dict:
    """Hold ``invocation.json`` to its shape — used to prove the HARNESS emits a valid one."""
    if raw_bytes is not None and len(raw_bytes) > MAX_MANIFEST_BYTES:
        raise ContractViolation(f'the manifest is {len(raw_bytes)} bytes, over the {MAX_MANIFEST_BYTES}-byte ceiling')
    if not isinstance(document, dict):
        raise ContractViolation(f'the manifest must be a JSON object, got {type(document).__name__}')
    if document.get('schema_version') != 1:
        raise ContractViolation(f'manifest schema_version must be 1, got {document.get("schema_version")!r}')
    for field in ('execution_id', 'pipeline_id', 'deployment_id', 'revision_id', 'timeout_seconds'):
        check_positive_int(document.get(field), f'manifest.{field}')
    attempt = check_positive_int(document.get('attempt'), 'manifest.attempt')
    generation = check_positive_int(document.get('generation'), 'manifest.generation')

    image = document.get('image_digest')
    if not isinstance(image, str) or not re.fullmatch(r'(?:.+@)?sha256:[0-9a-f]{64}', image):
        raise ContractViolation(f'manifest.image_digest {image!r} is not pinned to an immutable digest')

    key = document.get('idempotency_key')
    if not isinstance(key, str) or not key.strip():
        raise ContractViolation(f'manifest.idempotency_key must be a non-blank string, got {key!r}')

    prefix = document.get('staging_prefix')
    expected_tail = f'attempts/{attempt}/gen-{generation}'
    if not isinstance(prefix, str) or not prefix.rstrip('/').endswith(expected_tail):
        raise ContractViolation(
            f'manifest.staging_prefix {prefix!r} must end with {expected_tail!r} — the prefix has to encode '
            f'the same attempt and generation the manifest claims, or the per-generation fence is not there'
        )

    ports = document.get('inputs', [])
    if not isinstance(ports, list):
        raise ContractViolation(f'manifest.inputs must be a list, got {type(ports).__name__}')
    duplicated = _duplicates(str(port.get('name')) for port in ports)
    if duplicated:
        raise ContractViolation(f'duplicate input port name(s) {duplicated} — port names must be unique')
    for port in ports:
        name = port.get('name')
        layout = port.get('layout')
        if layout not in ('file', 'prefix'):
            raise ContractViolation(f'input port {name!r}: layout must be file/prefix, got {layout!r}')
        cardinality = port.get('cardinality')
        if cardinality not in ('one', 'at_least_one', 'many'):
            raise ContractViolation(f'input port {name!r}: bad cardinality {cardinality!r}')
        objects = port.get('objects', [])
        if cardinality == 'one' and len(objects) != 1:
            raise ContractViolation(f'input port {name!r}: cardinality="one" requires exactly 1 object')
        if cardinality == 'at_least_one' and not objects:
            raise ContractViolation(f'input port {name!r}: cardinality="at_least_one" requires 1 or more objects')
        for index, obj in enumerate(objects):
            check_sha256(obj.get('sha256'), f'input port {name!r} objects[{index}].sha256')
            check_size(obj.get('size'), f'input port {name!r} objects[{index}].size')
    return document
