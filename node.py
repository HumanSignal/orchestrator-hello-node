#!/usr/bin/env python3
"""hello-node — the smallest program that is a real LSPO external step.

It reads its inputs, VERIFIES each one against the hash and the size the orchestrator
pinned for it, copies each one into its output area, writes a small report, and finishes
by writing the completion marker.

**This file deliberately imports nothing from the orchestrator, and nothing from PyPI.**
Copy it into your own repository and start editing: the contract is four JSON documents
and a directory, not a library. The standard library is the whole dependency list, and
that is not an aesthetic choice — see `Why no HTTP library`_ below.

What the orchestrator gives you
-------------------------------

``LSPO_CREDENTIALS_FILE`` names a JSON file the agent mounts read-only. It carries a
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

The five things this file does that a first version always leaves out
---------------------------------------------------------------------

Each of them is a RECOMMENDATION in the documents — nothing in the platform checks any
of them — and each is the difference between a node that works on the demo and one that
survives a real job. ``docs/AUTHORING.md`` explains every one at length.

1. **It streams, in both directions, and never holds an object in memory.** A single
   object may legally be 1 GiB while the container's default memory limit is 2 GiB, so
   holding one as bytes and again as a request body is an out-of-memory kill — and an
   OOM kill leaves no chance to write a marker at all.
2. **It re-reads its credentials.** The agent replaces the file underneath a running
   container, atomically and with no signal. A node that reads it once cannot upload
   its outputs, or its own marker, after about fifteen minutes.
3. **It keeps its inventory where the failure path can see it.** Salvage publishes only
   what the marker lists, so an inventory local to the work function strands everything
   already uploaded.
4. **It handles a stop request.** This process is PID 1 in its container, and Linux
   gives process 1 no default signal handling: without a handler, SIGTERM is discarded
   entirely and the step runs to completion for a run nobody will collect.
5. **It never prints a credential.** A presigned URL's query string IS a read credential
   for that object, and container output is stored with the execution, shown to everyone
   who can see the run, and searchable.

.. _Why no HTTP library:

Why no HTTP library
-------------------

This file used to depend on ``requests``, and that dependency quietly contradicted point
1 above. ``requests`` builds a multipart upload body by concatenating it in memory
(``urllib3.filepost.encode_multipart_formdata``), so ``files={'file': ...}`` holds the
whole object — the very thing the 1 GiB-object-against-2 GiB-of-memory warning is about.
Streaming an upload with it needs a further dependency (``requests_toolbelt``). The
standard library can do it in about sixty lines, which is what :class:`_MultipartBody`
and :func:`_post_object` are, so the dependency list is empty and the recommendation is
actually followed.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import io
import json
import logging
import os
import re
import signal
import sys
import tempfile
import time
import urllib.error
import urllib.request

MARKER_FILENAME = '__lspo_complete.json'
RESULT_FILENAME = 'result.json'
OUTPUT_PORT = 'output'
REPORT_PORT = 'report'
OUTPUT_DIR = 'outputs'

#: The variable the agent sets for every job. This is the one to read.
CREDENTIALS_ENV = 'LSPO_CREDENTIALS_FILE'
#: A name only this repository's own images ever set. Kept working on purpose; see
#: :func:`resolve_credentials_path`.
LEGACY_CREDENTIALS_ENV = 'LSPO_CREDENTIALS'
#: Where the orchestrator mounts credentials when the envelope says nothing
#: (``external/contract.py`` ``DEFAULT_CREDENTIALS_FILE``).
DEFAULT_CREDENTIALS_FILE = '/lspo/creds/creds.json'

EXIT_OK = 0
EXIT_TRANSIENT = 1
EXIT_PERMANENT = 10
EXIT_CANCELLED = 20

#: The contract refuses a job description or a marker above this.
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
#: ``result.json`` is a contract document and its reader is bounded at 1 MiB. Writing a
#: bigger one publishes something the other side is forbidden to read.
MAX_RESULT_BYTES = 1024 * 1024
#: The upload policy refuses a single object above this.
MAX_OBJECT_BYTES = 1024 * 1024 * 1024

#: Socket timeout for one read. A stop landing during a read is noticed as soon as the
#: next block arrives, because the handler closes the response underneath it.
READ_TIMEOUT_S = 25.0
#: Socket timeout for one upload — deliberately shorter. Once the body has been sent the
#: step is waiting for the store's answer, and there is nothing left to close: no signal
#: can shorten that wait, so the timeout is the only thing that bounds it. It has to stay
#: well inside the grace a stop is given, or ignoring a stop costs a runner slot.
UPLOAD_TIMEOUT_S = 10.0
#: Every streaming copy moves this much at a time.
CHUNK_BYTES = 1024 * 1024
#: Re-read the envelope this long before it says it expires.
CREDS_MARGIN_S = 5.0

USER_AGENT = 'lspo-hello-node/2 (+https://github.com/HumanSignal/orchestrator-hello-node)'


class StepError(Exception):
    """Something no retry can fix: a bad input, a bad document, a bad configuration."""


class TransientError(Exception):
    """Something a later attempt might survive: a timeout, a 5xx, an expired signature."""


class _Expired(TransientError):
    """A refusal that looks like expired or rejected credentials."""


# ------------------------------------------------------------------ cancellation

#: Set by the signal handler; read by ordinary control flow. Never do work in a handler.
CANCELLED = False

#: Transfers currently in flight. The handler closes them, which is what turns "the step
#: was asked to stop" into an immediate return from a socket read that would otherwise
#: block for as long as the other side felt like holding it open. Without this, a stop
#: that lands inside a transfer is not noticed until the transfer ends on its own.
_IN_FLIGHT: set = set()


def _on_stop(signum, _frame):
    global CANCELLED
    CANCELLED = True
    for handle in list(_IN_FLIGHT):
        with contextlib.suppress(Exception):
            handle.close()
    with contextlib.suppress(Exception):
        sys.stderr.write(f'hello-node: stop requested (signal {signum}); finishing up\n')
        sys.stderr.flush()


class _Stopped(Exception):
    """Raised by ordinary control flow once the flag is seen."""


def _check_stopped() -> None:
    if CANCELLED:
        raise _Stopped('stop requested')


@contextlib.contextmanager
def _in_flight(handle):
    _IN_FLIGHT.add(handle)
    try:
        yield handle
    finally:
        _IN_FLIGHT.discard(handle)
        with contextlib.suppress(Exception):
            handle.close()


# --------------------------------------------------------------------- logging

# Everything this program writes to stdout or stderr is captured by the runner, sent to
# the orchestrator and shown in the run's log — so `print()` works, and so does `logging`
# ONCE IT IS CONFIGURED. Without this line a bare `log.info(...)` prints NOTHING: Python's
# default emits WARNING and above, and only to stderr. That is the single most common
# reason a node author says "my logs disappeared".
#
# One thing NOT to write here: secrets. Whatever this program prints is stored with the
# execution, shown to anyone who can see the run, and searchable — so no tokens, no
# credentials, no customer data you would not put in a ticket. The credentials this step
# is handed are short-lived, but a leaked one is still a leak. Everything below goes
# through `redact()` for exactly that reason.
logging.basicConfig(level=logging.INFO, stream=sys.stdout, format='%(levelname)s %(message)s')
log = logging.getLogger('hello-node')

_URL_RE = re.compile(r'\b(?:https?|s3)://[^\s\'"<>|\\]+', re.IGNORECASE)


def redact(text: object) -> str:
    """Reduce every URL to scheme, host and path.

    A presigned URL's query string is the credential. The natural way to report a failed
    fetch — letting the exception's own text through — is exactly what publishes it,
    because HTTP libraries build that text out of the URL.
    """

    def _strip(match: re.Match) -> str:
        url = match.group(0)
        for separator in ('?', '#'):
            cut = url.find(separator)
            if cut != -1:
                return url[:cut] + separator + 'REDACTED'
        return url

    return _URL_RE.sub(_strip, str(text)).replace('\r', ' ').strip()


_progress_sent = 0


def progress(fraction: float, phase: str) -> None:
    """The one stdout line the agent consumes as progress.

    A run that emits none is indistinguishable from a stuck one, which is the only
    thing an operator watching a long step has to go on. The shape is exact: the
    prefix ``@lspo:progress `` including its trailing space, then a JSON object whose
    ``fraction`` is between 0.0 and 1.0.
    """
    global _progress_sent
    if _progress_sent > 200:  # a log is a tail of 1000 lines; do not spend it on this
        return
    _progress_sent += 1
    payload = json.dumps({'fraction': round(max(0.0, min(1.0, float(fraction))), 4),
                          'phase': str(phase)[:64]})
    sys.stdout.write('@lspo:progress ' + payload + '\n')
    sys.stdout.flush()


# ----------------------------------------------------------------- credentials


def resolve_credentials_path() -> str:
    """Where the credentials file is, and a warning when the two names disagree.

    ``LSPO_CREDENTIALS_FILE`` is the one the agent sets, for every job, and its value is
    the path the ORCHESTRATOR chose — so it wins. ``LSPO_CREDENTIALS`` is a name only
    this repository's own older images set; it is honoured when it is the only one
    present, because images that bake it are in the field. With neither set, the
    contract's own default path is a better answer than giving up.

    The disagreement warning names the two VARIABLES and never their values: a path is
    not a secret, but a habit of printing whatever is in an environment variable is how
    one eventually gets printed. It stays quiet when they agree — a line that appears on
    every run is not a warning, it is the noise that teaches people to skip the first ten
    lines of a log.
    """
    current = os.environ.get(CREDENTIALS_ENV)
    legacy = os.environ.get(LEGACY_CREDENTIALS_ENV)

    if current and legacy and os.path.normpath(current) != os.path.normpath(legacy):
        log.warning(
            '%s and %s name different files; using %s, which is the one the agent sets',
            CREDENTIALS_ENV, LEGACY_CREDENTIALS_ENV, CREDENTIALS_ENV,
        )
    if current:
        return current
    if legacy:
        return legacy
    return DEFAULT_CREDENTIALS_FILE


def _parse_iso8601(value: object) -> float:
    """ISO 8601 to epoch seconds. Unparseable means "treat it as already stale"."""
    if not isinstance(value, str) or not value.strip():
        return 0.0
    text = value.strip()
    if text.endswith(('Z', 'z')):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


class Credentials:
    """The envelope, re-read whenever it is at or near its stated expiry.

    The agent asks for a fresh envelope shortly before the current one dies and replaces
    ``creds.json`` atomically underneath the container, by writing a new file and renaming
    it over the old one. **You are not signalled.** The directory is mounted rather than
    the file precisely so that the replacement is visible to a process that reads the path
    again — so an accessor like this one, rather than a variable in ``main()``, is the
    whole difference between a node that can upload after fifteen minutes and one that
    cannot.
    """

    def __init__(self) -> None:
        self.path = resolve_credentials_path()
        self._envelope: dict | None = None
        self._expires_at = 0.0

    def get(self, *, force: bool = False, allow_stale: bool = False) -> dict:
        if force or self._envelope is None or time.time() >= self._expires_at - CREDS_MARGIN_S:
            try:
                envelope = self._load()
            except Exception:
                # Falling back is for one caller only: the marker. A presigned URL keeps
                # working until its own expiry whatever happens to the file it came out
                # of, and a run that can still explain itself is worth more than one that
                # cannot. Everywhere else a vanished credentials file stops the work,
                # because it is the platform's signal that this attempt is over.
                if self._envelope is None or not allow_stale:
                    raise
                log.warning('the credentials file could not be re-read; '
                            'attempting the marker with the envelope already held')
                return self._envelope
            self._envelope = envelope
            self._expires_at = _parse_iso8601(envelope.get('expires_at'))
        return self._envelope

    def _load(self) -> dict:
        try:
            with open(self.path, 'rb') as handle:
                raw = handle.read(64 * 1024 * 1024)
        except FileNotFoundError:
            raise TransientError(
                f'no credentials file at the path {CREDENTIALS_ENV} names; '
                f'the agent did not mount one, or this job is no longer its'
            )
        except OSError as exc:
            raise TransientError(f'the credentials file could not be read: {redact(exc)}')

        try:
            envelope = json.loads(raw.decode('utf-8'))
        except Exception as exc:
            raise TransientError(f'the credentials file is not valid JSON: {redact(exc)}')
        if not isinstance(envelope, dict):
            raise TransientError('the credentials file is not a JSON object')

        version = envelope.get('schema_version', 1)
        if not _is_int(version) or version != 1:
            raise StepError(f'credentials envelope schema_version {version!r} is not supported')
        staging = envelope.get('staging')
        if not isinstance(staging, dict) or staging.get('mode') not in ('local_path', 'presigned_post'):
            raise StepError('the credentials envelope carries no staging area this step can write to')
        return envelope


def _is_int(value: object) -> bool:
    """A real JSON integer. In Python ``True == 1``, which is the trap this closes."""
    return isinstance(value, int) and not isinstance(value, bool)


# ------------------------------------------------------------------------- http


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    """A short, URL-free description of a refusal, including the store's own error code."""
    body = ''
    with contextlib.suppress(Exception):
        body = exc.read(4096).decode('utf-8', 'replace')
    with contextlib.suppress(Exception):
        exc.close()
    code = re.search(r'<Code>([^<]{1,64})</Code>', body)
    if code:
        return f'HTTP {exc.code} {exc.reason} ({code.group(1)})'
    if body.strip():
        return f'HTTP {exc.code} {exc.reason} ({redact(" ".join(body.split())[:160])})'
    return f'HTTP {exc.code} {exc.reason}'


#: Store error codes that mean "this could work if you tried again", even though they can
#: arrive with a 4xx status that would otherwise read as final.
_RETRYABLE_CODES = ('slowdown', 'requesttimeout', 'operationaborted', 'requestthrottled',
                    'throttling', 'toomanyrequests', 'internalerror', 'serviceunavailable')
#: …and the ones that mean the credential is dead, so re-reading the file may help.
_EXPIRED_CODES = ('expired', 'accessdenied', 'invalidaccesskeyid', 'signaturedoesnotmatch',
                  'tokenrefreshrequired', 'requesttimetooskewed', 'authorization')


def _classify(exc: urllib.error.HTTPError, detail: str, what: str) -> Exception:
    """Turn one refusal into the right exception, which decides the exit code.

    Getting this wrong is not cosmetic: reporting a momentary 503 from an object store as
    permanent tells the platform never to run this work again.
    """
    flat = detail.lower().replace(' ', '')
    message = f'{what} was refused: {detail}'
    if exc.code in (400, 401, 403) and any(code in flat for code in _EXPIRED_CODES):
        return _Expired(message)
    if exc.code >= 500 or exc.code == 429 or any(code in flat for code in _RETRYABLE_CODES):
        return TransientError(message)
    if 300 <= exc.code < 400:
        return TransientError(message)
    return StepError(message)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect rather than silently turning a POST into a bodyless GET.

    urllib answers 301/302/303 by re-issuing the request as a GET with no body. An upload
    redirected that way comes back 200 with the object never written, and the step then
    inventories a file that does not exist — which costs the whole delivery when
    collection checks its hash.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, 'the storage endpoint redirected the request', headers, fp
        )


_OPENER = urllib.request.build_opener(_NoRedirects)


def _open(url: str, *, what: str):
    """GET one URL. Raises on a bad status, and never puts the URL in the message."""
    request = urllib.request.Request(url, method='GET', headers={'User-Agent': USER_AGENT})
    try:
        response = _OPENER.open(request, timeout=READ_TIMEOUT_S)
    except urllib.error.HTTPError as exc:
        raise _classify(exc, _http_error_detail(exc), f'reading {what}')
    except urllib.error.URLError as exc:
        raise TransientError(f'reading {what} failed: {redact(getattr(exc, "reason", exc))}')
    except Exception as exc:
        raise TransientError(f'reading {what} failed: {redact(exc)}')
    status = int(getattr(response, 'status', 200) or 200)
    if not 200 <= status < 300:
        response.close()
        raise TransientError(f'reading {what} returned HTTP {status}')
    return response


class _MultipartBody:
    """A form body that streams: the fields, then the file, then the closing boundary.

    ``http.client`` reads an object with a ``read`` method in blocks, so the file never
    exists in memory. That is the whole reason this class exists rather than a call to
    a friendlier HTTP library — see the module docstring.
    """

    def __init__(self, head: bytes, source_path: str, file_size: int, tail: bytes) -> None:
        self._head = memoryview(head)
        self._tail = memoryview(tail)
        self._source_path = source_path
        self._remaining = file_size
        self._handle = None
        self._stage = 0  # 0 head, 1 file, 2 tail, 3 done
        self._head_at = 0
        self._tail_at = 0
        self.length = len(head) + file_size + len(tail)

    def __len__(self) -> int:
        return self.length

    def read(self, size: int = -1) -> bytes:
        if size is None or size <= 0:
            size = CHUNK_BYTES
        while True:
            if self._stage == 0:
                if self._head_at < len(self._head):
                    piece = bytes(self._head[self._head_at:self._head_at + size])
                    self._head_at += len(piece)
                    return piece
                self._stage = 1
                self._handle = open(self._source_path, 'rb')
            elif self._stage == 1:
                # Never send more of the file than Content-Length promised: a body longer
                # than its declared length desynchronises the connection rather than
                # failing cleanly.
                piece = self._handle.read(min(size, self._remaining))
                if piece:
                    self._remaining -= len(piece)
                    return piece
                self._handle.close()
                self._handle = None
                self._stage = 2
            elif self._stage == 2:
                if self._tail_at < len(self._tail):
                    piece = bytes(self._tail[self._tail_at:self._tail_at + size])
                    self._tail_at += len(piece)
                    return piece
                self._stage = 3
            else:
                return b''

    def close(self) -> None:
        if self._handle is not None:
            with contextlib.suppress(Exception):
                self._handle.close()
            self._handle = None


def _post_object(post: dict, key: str, source_path: str, size: int, what: str) -> None:
    """Upload one object through the presigned POST policy, streaming from disk.

    ``key`` is set explicitly rather than left to the ``${filename}`` placeholder: nested
    relpaths then work predictably, and the policy's ``starts-with`` condition means the
    store itself refuses anything outside this job's prefix.
    """
    boundary = '----lspo' + hashlib.sha256(
        f'{key}|{size}|{time.time()}'.encode('utf-8')).hexdigest()[:32]
    dash = ('--' + boundary).encode('ascii')

    head = io.BytesIO()
    fields = dict(post.get('fields') or {})
    fields['key'] = key
    for name, value in fields.items():
        head.write(dash + b'\r\n')
        head.write(b'Content-Disposition: form-data; name="%s"\r\n\r\n'
                   % str(name).replace('"', '').encode('utf-8'))
        head.write(str(value).encode('utf-8') + b'\r\n')
    filename = os.path.basename(key).replace('"', '') or 'object'
    head.write(dash + b'\r\n')
    head.write(b'Content-Disposition: form-data; name="file"; filename="%s"\r\n'
               % filename.encode('utf-8'))
    head.write(b'Content-Type: application/octet-stream\r\n\r\n')

    body = _MultipartBody(head.getvalue(), source_path, size, b'\r\n' + dash + b'--\r\n')
    request = urllib.request.Request(
        post['url'], data=body, method='POST',
        headers={'Content-Type': 'multipart/form-data; boundary=' + boundary,
                 'Content-Length': str(len(body)),
                 'User-Agent': USER_AGENT},
    )
    try:
        with _in_flight(body):
            response = _OPENER.open(request, timeout=UPLOAD_TIMEOUT_S)
    except urllib.error.HTTPError as exc:
        raise _classify(exc, _http_error_detail(exc), f'the upload of {what}')
    except urllib.error.URLError as exc:
        # Ambiguous by nature: the store may already have accepted it. Never retried.
        raise TransientError(
            f'the upload of {what} failed before an answer arrived: '
            f'{redact(getattr(exc, "reason", exc))}')
    except Exception as exc:
        raise TransientError(f'the upload of {what} failed before an answer arrived: {redact(exc)}')
    with contextlib.closing(response):
        status = int(getattr(response, 'status', 200) or 200)
        if not 200 <= status < 300:
            raise TransientError(f'the upload of {what} returned HTTP {status}')


# -------------------------------------------------------------- staging writes

#: Kept at module scope on purpose. Salvage publishes only what the MARKER inventories,
#: so an inventory local to the work function is empty on the failure path and everything
#: already uploaded is stranded in a staging area nobody will look at again.
INVENTORY: list[dict] = []
PORTS: dict[str, list[str]] = {}

_BAD_IN_RELPATH = re.compile(r'[\x00-\x1f\\]')


def check_relpath(relpath: str) -> str:
    """The canonical-relpath rule, applied before writing rather than after.

    The marker parser refuses anything else, and it refuses the whole document — so one
    bad name costs every object in it.
    """
    if not isinstance(relpath, str) or not relpath or relpath.startswith('/'):
        raise StepError(f'output relpath {relpath!r} must be a non-empty relative path')
    if _BAD_IN_RELPATH.search(relpath):
        raise StepError(f'output relpath {relpath!r} carries a backslash or a control character')
    if any(part in ('', '.', '..') for part in relpath.split('/')):
        raise StepError(f'output relpath {relpath!r} has an empty, "." or ".." component')
    return relpath


def write_object(creds: Credentials, relpath: str, source_path: str, size: int, what: str) -> None:
    """Write one object into the job's staging area, from a file on disk.

    Every write in this program goes through this ONE function — which is what makes
    "the marker is written last" a property of the code rather than a hope, and what
    lets a test record the order things were written in.
    """
    check_relpath(relpath)
    if size > MAX_OBJECT_BYTES:
        raise StepError(f'{what} is {size} bytes, above the 1 GiB the upload policy allows')

    for attempt in (1, 2):
        envelope = creds.get()
        staging = envelope['staging']
        try:
            if staging['mode'] == 'local_path':
                _copy_into(staging['path'], relpath, source_path)
            else:
                post = staging['post']
                _post_object(post, post['key_prefix'] + relpath, source_path, size, what)
            return
        except _Expired:
            if attempt == 2:
                raise
            # Retry once, and only if the envelope really changed. Compare the DOCUMENTS,
            # never the objects: re-reading parses a brand new dict every time, so an
            # identity test is always "different" and this guard would fire on every
            # refusal instead of the ones that mean something.
            if creds.get(force=True) == envelope:
                raise
            log.info('%s was refused; the credentials had been refreshed, retrying once', what)


def _copy_into(staging_path: str, relpath: str, source_path: str) -> None:
    destination = os.path.join(staging_path, relpath)
    os.makedirs(os.path.dirname(destination) or '.', exist_ok=True)
    partial = destination + '.partial'
    with open(source_path, 'rb') as source, open(partial, 'wb') as target:
        while True:
            chunk = source.read(CHUNK_BYTES)
            if not chunk:
                break
            target.write(chunk)
        target.flush()
        os.fsync(target.fileno())
    os.replace(partial, destination)


def record(relpath: str, digest: str, size: int, port: str | None) -> None:
    for existing in INVENTORY:
        if existing['relpath'] == relpath:
            raise StepError(f'relpath {relpath!r} would appear twice in the inventory')
    INVENTORY.append({'relpath': relpath, 'sha256': digest, 'size': size})
    if port:
        PORTS.setdefault(port.strip(), []).append(relpath)


def publish(creds: Credentials, relpath: str, source_path: str, digest: str, size: int,
            port: str | None, what: str) -> None:
    """Inventory one finished file and then upload it, in that order.

    The inventory entry goes in BEFORE the upload starts, on purpose. A store can accept
    a body after the client that sent it is gone, so an object recorded only on success
    can end up present in staging and named by nobody — and salvage looks at nothing it
    was not told about. The other way round is safe: on a failed or cancelled run each
    object is verified on its own and one that cannot be found costs itself alone. On a
    successful run the distinction never arises, because the marker is written only after
    every upload has come back accepted.
    """
    record(relpath, digest, size, port)
    write_object(creds, relpath, source_path, size, what)


# ------------------------------------------------------------------------ inputs


@contextlib.contextmanager
def fetched_and_verified(creds: Credentials, index: int, scratch: str):
    """Stream one input to disk, hold it to its pin, and delete it afterwards.

    Verifying is not ceremony. **The platform verifies what you WROTE, never what you
    READ**: the URL is presigned and points at a key, and "the bytes at that key today"
    is not automatically "the bytes that were there when this job was built". A step that
    skips this can process the wrong version of its input and produce output that passes
    every check the orchestrator makes on the way back, because those checks are about
    what you wrote. A missing pin is refused for the same reason: an input nobody can
    verify is an input nobody should act on.

    It streams to a temporary file and deletes it on every path out — including the
    failure and cancellation ones — because nothing bounds the size, the total or the
    number of inputs, and the container is given no disk quota at all.
    """
    handle = tempfile.NamedTemporaryFile(delete=False, dir=scratch, prefix='in-', suffix='.bin')
    path = handle.name
    handle.close()
    try:
        for attempt in (1, 2):
            envelope = creds.get()
            source = (envelope.get('inputs') or [])[index]
            name = source.get('relpath') or source.get('name') or f'input-{index}'
            expected_sha = source.get('sha256')
            expected_size = source.get('size')
            if not expected_sha:
                raise StepError(
                    f'input {name!r} arrived with no sha256 pin; refusing to work on unverified bytes')

            digest = hashlib.sha256()
            size = 0
            try:
                with open(path, 'wb') as target:
                    for chunk in _stream(source, name):
                        _check_stopped()
                        digest.update(chunk)
                        size += len(chunk)
                        if _is_int(expected_size) and size > expected_size:
                            raise StepError(
                                f'input {name!r} is larger than the {expected_size} bytes the job '
                                f'pinned for it — this is not the object this run was built from')
                        target.write(chunk)
                break
            except _Expired:
                if attempt == 2:
                    raise
                if creds.get(force=True) == envelope:
                    raise
                log.info('reading input %r was refused; the credentials had been refreshed, '
                         'retrying once', name)

        if _is_int(expected_size) and size != expected_size:
            raise StepError(f'input {name!r} is {size} bytes, but the job pinned {expected_size}')
        actual = digest.hexdigest()
        if actual != expected_sha:
            raise StepError(
                f'input {name!r} hashes to {actual}, but the job pinned {expected_sha} — '
                f'this is not the object this run was built from')
        yield name, path, actual, size
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def _stream(source: dict, name: str):
    if source.get('get_url'):
        with _in_flight(_open(source['get_url'], what=f'input {name!r}')) as response:
            while True:
                chunk = response.read(CHUNK_BYTES)
                if not chunk:
                    return
                yield chunk
    elif source.get('local_path'):
        try:
            with open(source['local_path'], 'rb') as handle:
                while True:
                    chunk = handle.read(CHUNK_BYTES)
                    if not chunk:
                        return
                    yield chunk
        except OSError as exc:
            raise TransientError(f'input {name!r} could not be read: {redact(exc)}')
    else:
        raise StepError(f'input {name!r} has neither a get_url nor a local_path')


def read_manifest(creds: Credentials) -> dict:
    """The job description, from wherever the envelope says it lives."""
    envelope = creds.get()
    if envelope.get('manifest_get'):
        with contextlib.closing(_open(envelope['manifest_get'], what='the job description')) as response:
            raw = response.read(MAX_DOCUMENT_BYTES + 1)
    elif envelope.get('manifest_path'):
        try:
            with open(envelope['manifest_path'], 'rb') as handle:
                raw = handle.read(MAX_DOCUMENT_BYTES + 1)
        except OSError as exc:
            raise TransientError(f'the job description could not be read: {redact(exc)}')
    else:
        raise StepError('the credential envelope names neither manifest_get nor manifest_path')

    if len(raw) > MAX_DOCUMENT_BYTES:
        raise StepError('the job description is larger than the 8 MiB the platform publishes')
    try:
        manifest = json.loads(raw.decode('utf-8'))
    except Exception as exc:
        # An expired signed URL usually arrives here as an XML error page.
        raise TransientError(
            f'the job description did not parse as JSON, which usually means a signed URL '
            f'returned an error page: {redact(exc)}')
    if not isinstance(manifest, dict):
        raise StepError('the job description is not a JSON object')
    version = manifest.get('schema_version', 1)
    if not _is_int(version) or version != 1:
        raise StepError(f'job description schema_version {version!r} is not supported')
    return manifest


# -------------------------------------------------------------------------- work


def output_relpath(port: str, name: str, taken: set) -> str:
    """Where one input's copy goes, disambiguated only when it has to be.

    Two ports may legitimately carry a file of the same name — they are different files —
    and the envelope carries the ``port`` beside every entry for exactly this reason.
    Deriving the output name from the input name ALONE collapses them onto one path,
    which loses one input's bytes and writes an inventory with a duplicate relpath; the
    collector refuses that document outright, so the run exits 0 and publishes nothing.

    Namespacing everything by port unconditionally would avoid that and cost more than it
    saves: it renames every output on any job that happens to use two ports, including
    the ones whose names never clashed. So the plain ``outputs/<name>`` is used until it
    is actually taken, then the port disambiguates, then a counter — nothing in the
    contract forbids two objects on ONE port sharing a name either.
    """
    for candidate in (f'{OUTPUT_DIR}/{name}', f'{OUTPUT_DIR}/{port}/{name}'):
        if candidate not in taken:
            taken.add(candidate)
            return candidate
    base = f'{OUTPUT_DIR}/{port}/{name}'
    stem, dot, extension = base.rpartition('.')
    suffix = 2
    while True:
        candidate = f'{stem}-{suffix}{dot}{extension}' if dot else f'{base}-{suffix}'
        if candidate not in taken:
            taken.add(candidate)
            return candidate
        suffix += 1


def process(creds: Credentials, manifest: dict, scratch: str) -> dict:
    """Copy every input into ``outputs/`` and count what went past."""
    sources = creds.get().get('inputs') or []
    taken: set = set()
    total_bytes = 0
    total_lines = 0

    for index in range(len(sources)):
        # Checked BEFORE each input, which is what makes a stop change what the step does
        # next rather than only how it ends. A step that notices the flag only at the end
        # has not been stopped; it has finished, for a run nobody will collect.
        _check_stopped()
        progress(index / max(len(sources), 1), 'copying inputs')
        with fetched_and_verified(creds, index, scratch) as (name, path, digest, size):
            port = (creds.get().get('inputs') or [])[index].get('port') or 'input'
            relpath = output_relpath(port, name, taken)
            publish(creds, relpath, path, digest, size, OUTPUT_PORT, f'output {relpath!r}')
            total_bytes += size
            total_lines += _count_lines(path)

    metrics = {'files': len(INVENTORY), 'total_bytes': total_bytes, 'lines': total_lines}
    _write_result(creds, manifest, metrics, scratch)
    progress(1.0, 'done')
    return metrics


def _count_lines(path: str) -> int:
    lines = 0
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(CHUNK_BYTES)
            if not chunk:
                return lines
            lines += chunk.count(b'\n')


def _write_result(creds: Credentials, manifest: dict, metrics: dict, scratch: str) -> None:
    """The optional report document, kept inside the ceiling its reader is bound by.

    ``params`` is copied verbatim from whatever the pipeline author typed and is bounded
    only by the 8 MiB manifest ceiling, so echoing it into a 1 MiB document is a way to
    publish something the other side is forbidden to read. Its shape is described
    instead of reproduced.

    It goes on its own port. Everything under a port is offered to every downstream step
    wired to it, and a metrics file delivered as if it were a result is a thing downstream
    steps have to learn to ignore.
    """
    params = manifest.get('params') or {}
    document = {
        'schema_version': 1,
        'metrics': metrics,
        'summary': {'attempt': manifest.get('attempt'), 'params': params},
    }
    payload = _dump(document)
    if len(payload) > MAX_RESULT_BYTES:
        # The echo is worth having — a step that re-encoded a float or mangled non-ASCII
        # on the way through would be teaching that to everyone who copies this file — but
        # not at the price of publishing a document its reader is forbidden to open. So
        # the echo is what gives way, and it says so rather than going quiet.
        log.warning('params are too large to echo into %s (%d bytes); '
                    'recording their shape instead', RESULT_FILENAME, len(payload))
        document['summary'] = {
            'attempt': manifest.get('attempt'),
            'params': None,
            'params_omitted': 'too large for the 1 MiB ceiling on this document',
            'params_keys': sorted(params)[:64] if isinstance(params, dict) else None,
            'params_count': len(params) if isinstance(params, dict) else None,
        }
        payload = _dump(document)
    if len(payload) > MAX_RESULT_BYTES:
        # Fail loudly at the point of the mistake rather than publish a document that
        # only turns out to be unreadable later, in somebody else's process.
        raise StepError(f'{RESULT_FILENAME} would be {len(payload)} bytes, above its 1 MiB ceiling')

    path = os.path.join(scratch, 'result.json')
    with open(path, 'wb') as handle:
        handle.write(payload)
    publish(creds, RESULT_FILENAME, path, _sha256_file(path), len(payload), REPORT_PORT,
            f'{RESULT_FILENAME!r}')


# ------------------------------------------------------------------------ marker


def identity(manifest: dict | None) -> dict:
    """Who this attempt is — from the job description, or from the environment.

    The marker's ``execution_id``, ``attempt`` and ``generation`` are cross-checked
    against the launch the platform believes it is collecting, so they have to be right.
    They normally come from the job description. When that could not be read at all, the
    agent has already put the same four values in the environment, and this is what they
    are for: a step that can only identify itself by quoting the document it failed to
    fetch has no way to report the failure that matters most.
    """
    fields: dict = {}
    for key, variable in (('execution_id', 'LSPO_EXECUTION_ID'),
                          ('attempt', 'LSPO_ATTEMPT'),
                          ('generation', 'LSPO_GENERATION')):
        value = (manifest or {}).get(key)
        if not _is_int(value) or value < 1:
            raw = os.environ.get(variable, '')
            value = int(raw) if raw.strip().lstrip('-').isdigit() else None
        if not _is_int(value) or value < 1:
            raise StepError(f'this attempt has no usable {key}')
        fields[key] = value
    key = (manifest or {}).get('idempotency_key') or os.environ.get('LSPO_IDEMPOTENCY_KEY')
    fields['idempotency_key'] = key if isinstance(key, str) and key.strip() else None
    return fields


def write_marker(creds: Credentials, manifest: dict | None, *, status: str, exit_code: int,
                 error: str | None = None) -> None:
    """The terminal receipt. ALWAYS the last thing this program writes.

    Nothing observes the order — collection begins only after the process has exited — so
    no check will ever catch a marker written early. What writing it last buys is that the
    marker's existence MEANS everything it names is already there, which turns a whole
    class of failure from "detected afterwards as a hash mismatch" into "impossible".

    It states its own identity — execution, attempt, generation — so the orchestrator can
    tell a receipt for THIS run from one a superseded copy of the job left behind.
    """
    inventoried = {entry['relpath'] for entry in INVENTORY}
    ports = {}
    for name, relpaths in PORTS.items():
        kept = [relpath for relpath in dict.fromkeys(relpaths) if relpath in inventoried]
        if kept:
            ports[name] = kept

    marker = {
        'schema_version': 1,
        'status': status,
        # The code this process is really about to return. A marker that says one thing
        # while the process says another leaves two contradictory accounts of one run.
        'exit_code': exit_code,
        'objects': list(INVENTORY),
        'produced_ports': ports,
        'error': redact(error)[:500] if error else None,
        **identity(manifest),
    }
    payload = _dump(marker)
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise StepError('the completion marker is larger than the 8 MiB its reader accepts')

    handle = tempfile.NamedTemporaryFile(delete=False, dir='/tmp', prefix='marker-', suffix='.json')
    try:
        with handle:
            handle.write(payload)
        # force=True: the marker is written last, which on a long run is exactly when the
        # envelope this step started with is most likely to be dead.
        creds.get(force=True, allow_stale=True)
        write_object(creds, MARKER_FILENAME, handle.name, len(payload), 'the completion marker')
    finally:
        with contextlib.suppress(OSError):
            os.unlink(handle.name)


# -------------------------------------------------------------------------- main


def main() -> int:
    """Run the step. Never raises: every ending becomes a marker plus an exit code."""
    signal.signal(signal.SIGTERM, _on_stop)
    signal.signal(signal.SIGINT, _on_stop)

    try:
        creds = Credentials()
        creds.get()
    except Exception as failure:
        # Before the credentials exist there is nowhere to write anything, so the only
        # honest ending is one short redacted line and an exit code — never a traceback.
        print(f'hello-node: could not start: {redact(failure)}', file=sys.stderr, flush=True)
        return EXIT_PERMANENT if isinstance(failure, StepError) else EXIT_TRANSIENT

    manifest: dict | None = None
    scratch = tempfile.mkdtemp(dir='/tmp', prefix='hello-node-')
    try:
        try:
            manifest = read_manifest(creds)
            log.info('execution %s attempt %s', manifest.get('execution_id'), manifest.get('attempt'))
            metrics = process(creds, manifest, scratch)
        except BaseException as failure:
            stopped = CANCELLED or isinstance(failure, (_Stopped, KeyboardInterrupt))
            status = 'cancelled' if stopped else 'failed'
            if stopped:
                code = EXIT_CANCELLED
                reason = f'stopped on request (the operation in flight ended with: {redact(failure)})'
            else:
                code = EXIT_PERMANENT if isinstance(failure, StepError) else EXIT_TRANSIENT
                reason = redact(failure)
            print(f'hello-node: {status.upper()}: {reason}', file=sys.stderr, flush=True)
            # A marker is written even here — especially here. Without one the platform
            # can report THAT the step failed and never why, and everything already
            # uploaded is stranded, because salvage publishes only what the marker names.
            try:
                write_marker(creds, manifest, status=status, exit_code=code, error=reason)
            except Exception as marker_failure:
                print(f'hello-node: could not write the {status} marker: {redact(marker_failure)}',
                      file=sys.stderr, flush=True)
            return code

        try:
            write_marker(creds, manifest, status='succeeded', exit_code=EXIT_OK)
        except BaseException as marker_failure:
            # The work is done and every object is in staging, but with no marker there is
            # no inventory, so the run fails with no account of itself. Say why, in one
            # line, and classify the ending rather than letting a traceback out.
            print(f'hello-node: the work finished but the marker could not be written: '
                  f'{redact(marker_failure)}', file=sys.stderr, flush=True)
            return EXIT_PERMANENT if isinstance(marker_failure, StepError) else EXIT_TRANSIENT
        log.info('done — %s file(s), %s bytes', metrics['files'], metrics['total_bytes'])
        return EXIT_OK
    finally:
        import shutil

        shutil.rmtree(scratch, ignore_errors=True)


# ---------------------------------------------------------------------- helpers


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _dump(document: dict) -> bytes:
    return json.dumps(document, sort_keys=True, indent=2).encode('utf-8')


if __name__ == '__main__':
    sys.exit(main())
