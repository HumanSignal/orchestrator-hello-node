"""The wire contract, re-implemented here from the specification rather than imported.

The orchestrator has its own copy of these rules (``external/contract.py``). This file
deliberately does NOT vendor it. A conformance harness that shares code with the thing
it is judging inherits that thing's bugs and stops being able to detect them: if the
orchestrator's parser ever grows lax about, say, an empty path component, a vendored
copy would go lax at the same moment and the node would pass a test it should fail.

So these are the rules as WRITTEN DOWN, and any disagreement between this file and the
orchestrator's parser is itself a finding worth reporting.

**Re-implemented is not the same as equivalent, and the difference was measured.** A
review of the first version of this file found four places where it disagreed with the
real parser: it rejected a document that omitted ``schema_version`` (the real one
defaults it to 1), it accepted ``"schema_version": true`` (the real one refuses booleans
explicitly, because ``True == 1`` in Python would otherwise select the version-1
parser), and it checked neither the prefix-layout rules nor the types of half the
manifest's fields. All four are fixed here, and
``tests/test_harness_self.py`` now validates the orchestrator's own frozen golden
documents (``conformance/fixtures/golden_v1_*.json``, copied byte-for-byte from
``external/fixtures/``) so that "this file agrees with the real one" is a measurement
rather than a claim.

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
* A relpath is relative and canonical: no leading ``/``, no backslash, **no control
  character at all**, no ``.`` or ``..`` component, and no empty component (so ``a//b``
  and a trailing ``/`` are both refused). A relpath and an output-port name are
  IDENTIFIERS, so they are refused rather than cleaned — a cleaned one would quietly
  name a different file, or match a different declared port.
* ``schema_version`` may be OMITTED — it defaults to 1. If present it must be a real
  integer: ``true`` is refused even though Python would call it equal to 1, and so are
  ``1.0`` and ``"1"``. A version this build has no parser for is refused by number.
* An input port with ``layout='prefix'`` must carry a ``prefix_digest``, a ``relpath``
  on every object, no duplicate relpaths, and a digest that matches the one recomputed
  from its own listing.
* ``result.json`` may carry an optional ``facts`` list — the searchable ``key=value``
  pairs the orchestrator records for the run. It is checked here and never enforced: a
  bad entry costs itself, so every finding about it is a RECOMMENDATION returned to the
  caller rather than a refusal (see :class:`Recommendation`).
* In a completion marker every relpath in ``produced_ports`` must also appear in
  ``objects``; a relpath may not repeat within one port; the same relpath MAY appear
  in two different ports; and ``objects`` may not inventory one relpath twice. An
  object claimed by no port at all is normal and allowed.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable

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

#: The nine variables the agent sets for EVERY workload container. They are not the whole
#: environment: a manifest may also name variables in ``env_names``, and the agent injects
#: the ones the operator's allowlist permits, with these nine applied last so a manifest
#: cannot shadow them (``agent/runner.py`` → ``_workload_env``). "Exactly nine" was this
#: harness's own mistake; the rule is "these nine, plus what was asked for and allowed".
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

#: The mode the agent gives a job's credentials DIRECTORY (``agent/creds.py``
#: ``CREDS_DIR_MODE``): **traversable by everybody, listable by nobody but the agent**.
#: The execute bit is what lets a KNOWN filename inside be opened; the read bit is what
#: lets the directory be enumerated, and the contract never needs that — your container
#: is told the exact path in ``LSPO_CREDENTIALS_FILE``. So this is the one real
#: constraint the mode bits still place on a node: open the path you were given, do not
#: list the directory it is in.
CREDENTIALS_DIR_MODE = 0o711

#: The mode the agent gives the credentials FILE itself (``agent/creds.py``
#: ``CREDS_FILE_MODE``): **readable by every uid, writable by none**, and re-applied on
#: every write, a credential refresh included. What keeps the credential off the rest of
#: the machine is the agent's own working directory ABOVE the mounted leaf — owner-only,
#: bind-mounted into nothing — rather than this file's mode.
#:
#: **There is therefore no uid your image has to run as**, and this pair of constants is
#: the whole of the reason. There used to be: the directory was 0700 and the file 0600,
#: owned by the uid the agent happened to run as, so an image declaring any other user
#: could not open its own credentials and the only repair a customer could find was to
#: run their container as root — the platform punishing the careful choice. Both halves
#: of that are gone (orchestrator PR #250 for the modes; PR #268 makes the agent run as
#: the operator's own account rather than as the uid its image declares), and this
#: repository no longer records an agent uid at all, because a number written down here
#: is a number somebody will build an image around. See
#: ``tests/test_platform_rules.py`` and CONFORMANCE-BASELINE.md.
CREDENTIALS_FILE_MODE = 0o444

SHA256_PATTERN = re.compile(r'[0-9a-f]{64}')

#: Every C0 control character, tab and newline included. A relpath and a port name are
#: identifiers, and there is no reading of a tab inside one that is layout somebody meant.
#: The platform refuses names on exactly this set (``external/text.py``
#: ``ANY_CONTROL_CHARACTER``); it generalised the older "no line break" rule when U+0000
#: turned out to be worse than a forged digest listing — no filesystem can hold it in a
#: name, so such a relpath cannot name a file that exists.
CONTROL_CHARACTER = re.compile(r'[\x00-\x1f]')

#: The orchestrator's rules for ONE search fact — the optional ``facts`` list in
#: ``result.json``. Re-stated from where they are written down (``pipelines/facts.py``:
#: ``KEY_RE``, ``MAX_VALUE_LEN``, ``_CONTROL_RE``; ``external/contract.py``:
#: ``MAX_RESULT_FACTS``) rather than imported, for the reason at the top of this file.
#: ``\Z`` and not ``$``, mirroring the orchestrator exactly: ``$`` also matches before a
#: final newline, so ``'task\n'`` would pass a check written with it — and be stored with
#: the newline attached, where no search can reach it. At full length it is worse than
#: unsearchable: 64 characters plus a newline is 65 going into a 64-character column, a row
#: no store will take. The rules here are picked so that cannot happen — everything they
#: accept is something the columns hold, which is what makes one bad entry cost only itself.
FACT_KEY = re.compile(r'^[a-z][a-z0-9_]{0,63}\Z')
MAX_FACT_VALUE_CHARS = 512
MAX_RESULT_FACTS = 10_000

#: The shape of one fact's ``meta``: FLAT, and small (``pipelines/facts.py``:
#: ``MAX_META_KEYS``, ``MAX_META_KEY_LEN``). See :func:`_why_this_meta_is_not_flat`.
MAX_FACT_META_KEYS = 32
MAX_FACT_META_KEY_CHARS = 64

#: The control characters a fact VALUE may not carry. Deliberately NOT the same set as
#: :data:`CONTROL_CHARACTER` above: this one includes DEL (0x7f). A value is refused
#: rather than cleaned for the reason the orchestrator gives beside the rule — a value
#: carrying a control character "did not come from the canonical formatter, and a
#: silently-mangled value would never match" anything anybody searches for.
CONTROL_CHARACTER_IN_FACT = re.compile(r'[\x00-\x1f\x7f]')

#: Half a UTF-16 surrogate pair, which is the one thing here that is not a matter of taste:
#: a lone surrogate has no UTF-8 encoding at all, so a value carrying one is not merely
#: unsearchable — the database refuses the row. It reaches a document the ordinary way, when
#: bytes that are not valid UTF-8 are decoded with surrogate escapes, which is what a
#: filename copied off a foreign filesystem does. Refused in a fact VALUE beside the control
#: characters (``pipelines/facts.py`` puts both in one class) and reported separately: a
#: finding is read one at a time, and "carries a control character" would suggest a tab.
LONE_SURROGATE = re.compile(r'[\ud800-\udfff]')

#: Every fact finding ends with this, because a finding is read one at a time and each
#: has to say what it costs. Nothing here is a refusal.
_A_BAD_FACT_COSTS_ITSELF = (
    'the orchestrator skips this entry, records the rest and fails nothing (search facts are '
    'not load-bearing; pipelines/facts.py)'
)

#: Contract versions this harness can read. The real gate lives in
#: ``external/versioning.py``; the shape of it — default to 1 when unstamped, refuse a
#: non-integer, refuse an integer with no parser — is what is re-stated here.
SUPPORTED_CONTRACT_VERSIONS = (1,)


class ContractViolation(AssertionError):
    """A document that the orchestrator's parser would refuse."""


class Recommendation(str):
    """A finding the platform does NOT refuse — the RECOMMENDATION label, made executable.

    Until search facts arrived, every rule this harness knew had the same consequence: the
    platform refuses the document, so the only report it needed was an exception. The
    optional ``facts`` list in ``result.json`` is the first thing here that is read ENTRY
    BY ENTRY and skipped entry by entry — a fact that breaks a rule costs that fact and
    nothing else, and the document, the objects it accompanies and the run are all
    unaffected. Raising for one would teach a RULE where ``docs/PROTOCOL.md`` says
    RECOMMENDATION, and somebody would then build a node around the stricter reading.

    So a finding of this kind is RETURNED, never raised: :func:`validate_result` appends
    one to the list a caller hands it and accepts the document regardless. It is a ``str``
    subclass so that the sentence is the whole of it — "which rule" is the content of a
    finding here exactly as it is for a violation.
    """


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
    if CONTROL_CHARACTER.search(value):
        raise ContractViolation(f'{where}: relpath {value!r} must not contain a control character')
    parts = value.split('/')
    if any(part in ('.', '..') for part in parts):
        raise ContractViolation(f'{where}: relpath {value!r} must not contain "." or ".." components')
    if any(part == '' for part in parts):
        raise ContractViolation(f'{where}: relpath {value!r} must not contain an empty path component')
    return value


def check_port_name(value: object, where: str) -> str:
    """A port name is an identifier twice over, so it is refused rather than cleaned.

    A downstream step selects its input by matching this string, and collection stores it
    on the delivered artifact as its ``payload_kind`` — so a cleaned name might match a
    DIFFERENT declared port, and handing the next step somebody else's file is worse than
    handing it nothing. A NUL is additionally unstorable: the artifact list lands in a
    Postgres ``jsonb`` column, which refuses the whole statement over that character.
    """
    if not isinstance(value, str) or not value.strip():
        raise ContractViolation(f'{where}: an output port name must not be empty or whitespace-only, got {value!r}')
    if CONTROL_CHARACTER.search(value):
        raise ContractViolation(f'{where}: output port name {value!r} must not contain control characters')
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


def check_schema_version(document: dict, where: str) -> int:
    """Read a document's contract version the way the real version gate does.

    Three rules, and the middle one is the subtle one:

    * **absent means 1.** An unstamped document is a version-1 document; refusing it
      would reject something the orchestrator's own parser accepts.
    * **``true`` is not 1.** ``bool`` subclasses ``int`` in Python and ``True == 1``, so
      a naive check selects the version-1 parser for ``"schema_version": true`` and
      parses it happily. Floats and numeric strings are refused for the same reason.
    * an integer with no parser is refused **by number**, so the error says which
      version arrived rather than which field looked odd.
    """
    version = document.get('schema_version', 1)
    if type(version) is not int:  # noqa: E721 - isinstance would accept bool
        raise ContractViolation(
            f'{where}: schema_version must be an integer, got {version!r} ({type(version).__name__})'
        )
    if version not in SUPPORTED_CONTRACT_VERSIONS:
        raise ContractViolation(f'{where}: unsupported schema_version={version}; this build reads '
                                f'{list(SUPPORTED_CONTRACT_VERSIONS)}')
    return version


def check_string(value: object, where: str, *, allow_absent: bool = False) -> str | None:
    """A real string, not a number that happens to render like one."""
    if value is None and allow_absent:
        return None
    if not isinstance(value, str):
        raise ContractViolation(f'{where}: expected a string, got {value!r}')
    return value


def prefix_digest(entries: Iterable[tuple[str, int, str]]) -> str:
    """Collapse a directory listing into one digest, in the contract's canonical form.

    Each entry renders as ``relpath\\nsize\\nsha256\\n``; the blocks are sorted AS
    STRINGS and concatenated, and the UTF-8 of that is hashed. Sorting the rendered
    blocks rather than the tuples is what makes the result independent of the order a
    listing came back in — and it is why this cannot be approximated: a digest computed
    any other way disagrees with the orchestrator's for the same tree, which reads as
    "the input changed".
    """
    blocks = []
    for relpath, size, sha256 in entries:
        if '\n' in relpath or '\n' in sha256:
            raise ContractViolation(f'prefix listing entry {relpath!r} contains a newline and cannot be framed')
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ContractViolation(f'prefix listing entry {relpath!r} has a bad size {size!r}')
        blocks.append(f'{relpath}\n{size}\n{sha256}\n')
    return hashlib.sha256(''.join(sorted(blocks)).encode('utf-8')).hexdigest()


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

    check_schema_version(document, 'marker')
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
        check_port_name(port, 'marker.produced_ports')
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


def validate_result(document: object, *, raw_bytes: bytes | None = None,
                    recommendations: list | None = None) -> dict:
    """Hold ``result.json`` to its shape and to the 1 MiB ceiling.

    The optional ``facts`` list is checked too, and it is the one part of this document
    that is NOT held to anything: every finding about it is appended to
    ``recommendations`` — a list the caller provides when it wants them — and the
    document is accepted either way. See :class:`Recommendation` for why, and
    :func:`_result_fact_findings` for the rules that are re-stated.
    """
    if raw_bytes is not None and len(raw_bytes) > MAX_RESULT_BYTES:
        raise ContractViolation(
            f'result.json is {len(raw_bytes)} bytes, over the {MAX_RESULT_BYTES}-byte ceiling — '
            f'result.json carries metrics and a summary, and anything approaching a megabyte '
            f'there is payload in the wrong place'
        )
    if not isinstance(document, dict):
        raise ContractViolation(f'result.json must be a JSON object, got {type(document).__name__}')
    check_schema_version(document, 'result.json')
    for field in ('metrics', 'summary'):
        value = document.get(field, {})
        if not isinstance(value, dict):
            raise ContractViolation(f'result.{field} must be an object, got {type(value).__name__}')
    if recommendations is not None:
        recommendations.extend(_result_fact_findings(document))
    return document


def _why_this_meta_is_not_flat(meta: dict) -> str | None:
    """The first reason the orchestrator would refuse this ``meta``, or None. **No recursion.**

    Its rule is FLAT and scalar: at most 32 keys; every key a string of 1 to 64 characters;
    every value one scalar — a string of at most 512 characters, a number that is finite, a
    boolean, or null. Every string, keys included, is held to the same rule as a fact value:
    no control character, no half of a UTF-16 surrogate pair.

    ``meta`` is small display detail for the Runs panel, so nothing anybody writes is being
    refused, and what the flatness buys is worth far more: the accepted shapes are exactly
    the shapes the orchestrator's columns hold, so a fact it accepts cannot then be rejected
    by the store and one hostile display detail can never cost a document its other facts.
    """
    if len(meta) > MAX_FACT_META_KEYS:
        return f'it has {len(meta)} keys, over the {MAX_FACT_META_KEYS} the orchestrator stores'
    for key, value in meta.items():
        if not isinstance(key, str) or not 1 <= len(key) <= MAX_FACT_META_KEY_CHARS:
            return f'the key {key!r} is not a string of 1 to {MAX_FACT_META_KEY_CHARS} characters'
        if CONTROL_CHARACTER_IN_FACT.search(key) or LONE_SURROGATE.search(key):
            return f'the key {key!r} carries a control character or half a UTF-16 surrogate pair'
        if value is None or isinstance(value, (bool, int)):
            continue
        if isinstance(value, float):
            if not math.isfinite(value):
                return f'{key!r} holds {value!r}, which is not a number any other JSON reader accepts'
            continue
        if not isinstance(value, str):
            return f'{key!r} holds a {type(value).__name__}, and a value has to be one scalar'
        if len(value) > MAX_FACT_VALUE_CHARS:
            return f'{key!r} holds {len(value)} characters, over the {MAX_FACT_VALUE_CHARS} stored'
        if CONTROL_CHARACTER_IN_FACT.search(value) or LONE_SURROGATE.search(value):
            return f'{key!r} carries a control character or half a UTF-16 surrogate pair'
    return None


def _result_fact_findings(document: dict) -> list[Recommendation]:
    """Every search fact in this document that the orchestrator would decline to record.

    The rules are its, not ours, and they are re-stated in :data:`FACT_KEY`,
    :data:`MAX_FACT_VALUE_CHARS`, :data:`CONTROL_CHARACTER_IN_FACT` and
    :data:`MAX_RESULT_FACTS`: an entry is an object with a ``key`` matching the key
    grammar, a ``value`` that is a string or an integer — a boolean is not one, because
    ``True`` would be recorded as the string ``'True'`` — non-blank once trimmed, at most
    512 characters and free of control characters, and a ``meta`` that is absent or an
    object. Past the cap the orchestrator drops the remaining entries.

    ``meta`` has one rule beyond "absent or an object": it must be FLAT and scalar (see
    :func:`_why_this_meta_is_not_flat`), which is what makes the accepted shapes exactly the
    shapes the columns hold. A fact VALUE carrying half a UTF-16 surrogate pair is refused on
    the same ground — it has no UTF-8 encoding, so the row cannot be stored. Both arrive out
    of a JSON writer that raised nothing, and the entry is then silently gone, which is
    precisely the outcome a fact exists to prevent.

    A ``facts`` key that is present and is not a list is reported the same way, and is worth
    reporting rather than passing over: the orchestrator logs a warning and records nothing,
    so a node that wrote its facts as an object is findable by none of them and never told.

    Present-and-``null`` counts as present, for both ``facts`` and ``meta``, and that is the
    orchestrator's own distinction rather than pedantry. Its rule is absent-OR-the-right-shape:
    a node with nothing to say leaves the key out, and one that emitted ``null`` has a
    serializer writing a shape nobody checked. Reading ``null`` as absence would make this
    harness quieter than the platform on exactly the documents an author needs told about.
    """
    if 'facts' not in document:
        return []
    facts = document['facts']
    if not isinstance(facts, list):
        return [Recommendation(
            f'result.facts must be a list, got {type(facts).__name__} — the orchestrator records '
            f'nothing from this document and logs a warning; the run is unaffected'
        )]

    findings = [Recommendation(
        f'result.facts carries {len(facts)} entries and only the first {MAX_RESULT_FACTS} are '
        f'recorded (external/contract.py MAX_RESULT_FACTS); the rest are dropped, and a node with '
        f'more things than that to name should record something coarser'
    )] if len(facts) > MAX_RESULT_FACTS else []

    for index, entry in enumerate(facts):
        where = f'result.facts[{index}]'
        if not isinstance(entry, dict):
            findings.append(Recommendation(
                f'{where} must be an object with key/value, got {type(entry).__name__} — '
                f'{_A_BAD_FACT_COSTS_ITSELF}'))
            continue
        key = entry.get('key')
        if not isinstance(key, str) or not FACT_KEY.match(key):
            findings.append(Recommendation(
                f'{where}.key {key!r} does not match the orchestrator\'s key grammar '
                f'{FACT_KEY.pattern} — {_A_BAD_FACT_COSTS_ITSELF}'))
        value = entry.get('value')
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            findings.append(Recommendation(
                f'{where}.value must be a string or an integer, got {value!r} — a boolean is not a '
                f'value, and {_A_BAD_FACT_COSTS_ITSELF}'))
        else:
            trimmed = str(value).strip()
            if not trimmed:
                findings.append(Recommendation(
                    f'{where}.value is blank once trimmed — {_A_BAD_FACT_COSTS_ITSELF}'))
            elif len(trimmed) > MAX_FACT_VALUE_CHARS:
                findings.append(Recommendation(
                    f'{where}.value is {len(trimmed)} characters, over the {MAX_FACT_VALUE_CHARS} the '
                    f'orchestrator stores — {_A_BAD_FACT_COSTS_ITSELF}'))
            elif CONTROL_CHARACTER_IN_FACT.search(trimmed):
                findings.append(Recommendation(
                    f'{where}.value carries a control character — {_A_BAD_FACT_COSTS_ITSELF}'))
            elif LONE_SURROGATE.search(trimmed):
                findings.append(Recommendation(
                    f'{where}.value carries half a UTF-16 surrogate pair, which has no UTF-8 encoding '
                    f'and cannot be stored at all — {_A_BAD_FACT_COSTS_ITSELF}'))
        if 'meta' in entry and not isinstance(entry['meta'], dict):
            findings.append(Recommendation(
                f'{where}.meta must be absent or an object, got {type(entry["meta"]).__name__} — '
                f'{_A_BAD_FACT_COSTS_ITSELF}'))
        elif isinstance(entry.get('meta'), dict):
            not_flat = _why_this_meta_is_not_flat(entry['meta'])
            if not_flat:
                findings.append(Recommendation(
                    f'{where}.meta must be a flat object of scalar values — {not_flat} — so the '
                    f'orchestrator drops this entry: {_A_BAD_FACT_COSTS_ITSELF}'))
    return findings


def validate_manifest(document: object, *, raw_bytes: bytes | None = None) -> dict:
    """Hold ``invocation.json`` to its shape — used to prove the HARNESS emits a valid one."""
    if raw_bytes is not None and len(raw_bytes) > MAX_MANIFEST_BYTES:
        raise ContractViolation(f'the manifest is {len(raw_bytes)} bytes, over the {MAX_MANIFEST_BYTES}-byte ceiling')
    if not isinstance(document, dict):
        raise ContractViolation(f'the manifest must be a JSON object, got {type(document).__name__}')
    check_schema_version(document, 'manifest')
    for field in ('execution_id', 'pipeline_id', 'deployment_id', 'revision_id', 'timeout_seconds'):
        check_positive_int(document.get(field), f'manifest.{field}')
    attempt = check_positive_int(document.get('attempt'), 'manifest.attempt')
    generation = check_positive_int(document.get('generation'), 'manifest.generation')

    # Optional in the contract, so absence is legal — but a value that IS present has to
    # be the right kind of value, or the harness would emit a document the real parser
    # refuses and blame the node for what happened next.
    if document.get('organization_id') is not None:
        check_positive_int(document.get('organization_id'), 'manifest.organization_id')
    check_string(document.get('credentials_file'), 'manifest.credentials_file', allow_absent=True)
    check_string(document.get('staging_prefix'), 'manifest.staging_prefix')
    if not isinstance(document.get('params', {}), dict):
        raise ContractViolation(f'manifest.params must be an object, got {document.get("params")!r}')
    env_names = document.get('env_names', [])
    if not isinstance(env_names, list) or any(not isinstance(name, str) for name in env_names):
        raise ContractViolation(f'manifest.env_names must be a list of strings, got {env_names!r}')

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
        _check_input_port(port)
    return document


def _check_input_port(port: dict) -> None:
    """One input port: its declared shape, its objects, and the prefix rules.

    The prefix half is the part that was missing. A ``layout='prefix'`` port is a whole
    tree, so no single object hash answers "did my input change?" — the digest of the
    sorted listing does, and the contract requires it to be present, to have a relpath on
    every object it is computed from, and to AGREE with those objects. A stated digest
    that disagrees with the listing beside it is worse than no digest, because both sides
    would trust it.
    """
    name = port.get('name')
    check_string(name, 'input port name')
    check_string(port.get('payload_kind'), f'input port {name!r}: payload_kind')
    layout = port.get('layout')
    if layout not in ('file', 'prefix'):
        raise ContractViolation(f'input port {name!r}: layout must be file/prefix, got {layout!r}')
    cardinality = port.get('cardinality')
    if cardinality not in ('one', 'at_least_one', 'many'):
        raise ContractViolation(f'input port {name!r}: bad cardinality {cardinality!r}')
    objects = port.get('objects', [])
    if not isinstance(objects, list):
        raise ContractViolation(f'input port {name!r}: objects must be a list, got {type(objects).__name__}')
    if cardinality == 'one' and len(objects) != 1:
        raise ContractViolation(f'input port {name!r}: cardinality="one" requires exactly 1 object')
    if cardinality == 'at_least_one' and not objects:
        raise ContractViolation(f'input port {name!r}: cardinality="at_least_one" requires 1 or more objects')

    for index, obj in enumerate(objects):
        where = f'input port {name!r} objects[{index}]'
        if not isinstance(obj, dict):
            raise ContractViolation(f'{where} must be an object, got {obj!r}')
        check_string(obj.get('uri'), f'{where}.uri')
        check_sha256(obj.get('sha256'), f'{where}.sha256')
        check_size(obj.get('size'), f'{where}.size')
        if obj.get('relpath') is not None:
            check_relpath(obj.get('relpath'), where)
        if obj.get('upstream_execution_id') is not None:
            check_positive_int(obj.get('upstream_execution_id'), f'{where}.upstream_execution_id')

    digest = port.get('prefix_digest')
    if layout != 'prefix':
        if digest is not None:
            check_sha256(digest, f'input port {name!r}: prefix_digest')
        return
    if not digest:
        raise ContractViolation(f'input port {name!r}: layout="prefix" requires a non-empty prefix_digest')
    check_sha256(digest, f'input port {name!r}: prefix_digest')
    missing = [obj.get('uri') for obj in objects if obj.get('relpath') is None]
    if missing:
        raise ContractViolation(
            f'input port {name!r}: layout="prefix" requires a relpath on every object (missing for '
            f'{missing}) — the prefix_digest is computed over (relpath, size, sha256)'
        )
    duplicated = _duplicates(obj['relpath'] for obj in objects)
    if duplicated:
        raise ContractViolation(f'input port {name!r}: duplicate relpath(s) {duplicated} in a prefix listing')
    recomputed = prefix_digest((obj['relpath'], obj['size'], obj['sha256']) for obj in objects)
    if recomputed != digest:
        raise ContractViolation(
            f'input port {name!r}: prefix_digest {digest} does not match the digest recomputed from its '
            f'own objects ({recomputed})'
        )
