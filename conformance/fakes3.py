"""A fake object store that behaves like the presigned surface a step actually sees.

It serves presigned-style GETs (the pinned inputs and ``invocation.json``) and accepts
presigned-POST multipart uploads under one key prefix, exactly as ``runners/credentials.py``
hands them out. It is small on purpose: everything a step can observe about S3 in this
contract is a URL that works, a URL that has stopped working, and a policy that admits
one prefix and refuses everything else.

**Four things it does that a real store does not — two because it is deliberately
STRICTER, one because a previous version of this file got the model wrong, and one
because a test has to be able to tell when the store has finished.**

1. *It can see the order objects arrived in.* Real S3 has no notion of "you wrote the
   completion marker before you wrote the object it inventories". Every upload here is
   stamped with a monotonic sequence number, so "the marker is written LAST" — the single
   most important ordering rule in the contract, and the one that makes a half-finished
   run distinguishable from a complete one — becomes a testable fact instead of a hope.
   Nothing in production can catch a step that gets this wrong; the collector will happily
   publish an inventory whose objects landed after it. This harness is the only place it
   can be caught, which is why it is caught here.

2. *The prefix fence and the POST policy are checked here rather than assumed.* Real S3
   enforces the policy's ``starts-with`` condition and every field the policy was signed
   with, and answers 403 otherwise. So does this. The difference is that this one records
   the refusal, so a test can prove the fence was exercised rather than merely never
   tripped.

3. *Credentials expire; they are not revoked, and they are never un-authorized in
   flight.* **This is the correction that matters, and this round finished it.**
   Issuing a fresh envelope does NOT invalidate the URLs already handed out: a presigned
   URL is a signature over a deadline, and nothing an issuer does afterwards can take it
   back. It stops working when its own ``expires_at`` passes, and not before. An earlier
   version of this file killed every previous credential the moment a new one was minted,
   which made a perfectly correct expiry-aware step look broken and would have forced
   "re-read the credentials file before every single transfer" — a requirement the
   platform does not make (``agent/creds.py``: the agent refreshes *before* expiry
   precisely "so the workload never has to handle an expired file").

   Expiry is evaluated **once, when a request's HEADERS arrive — before its body has been
   read**, which is how S3 authorizes: the signature and the policy are in the request it
   receives, and a large upload is not re-checked on the way out. A transfer that was
   authorized does not fail retroactively because it took a long time to send. This
   endpoint stamped a POST's arrival AFTER reading and parsing its whole body until this
   round, so a credential expiring while the bytes were still on the wire refused an
   upload that had begun inside its lifetime — the very thing the file claimed not to do.
   ``tests/test_harness_self.py`` now sends a body in two halves across an expiry instant
   to prove it. **This is a modelling choice about S3, not a platform rule**, and the
   self-test that pins it says so in its own label; no orchestrator source describes when
   a store authorizes.

4. *It knows what it has NOT finished.* Every request is on an in-flight ledger from the
   moment it is noted until the moment it has been answered, and :meth:`Endpoint.settle`
   blocks until that ledger is empty. A container killed mid-upload leaves this store
   still finishing a body it has already received in full — the object lands, and the
   reply goes to a socket nobody is reading. A test that snapshotted the store the
   instant the container died would therefore see a SMALLER store than the collector
   eventually will, and would pass a step that left an object landing behind it
   unaccounted for.

What it does NOT emulate: AWS signature verification (the "signature" here is an opaque
token naming a credential generation), request-time skew, multipart *chunked* uploads,
versioning, or eventual consistency. None of those change what the node must do.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, quote, urlparse

#: Serving a synthetic blob larger than the container's memory means never holding it in
#: ours either. One megabyte of deterministic filler, repeated.
SYNTHETIC_BLOCK = bytes(range(256)) * 4096

#: Ceiling on a single upload we are willing to buffer while parsing a multipart body.
#: Mirrors the ``content-length-range`` condition the real POST policy carries.
DEFAULT_MAX_OBJECT_BYTES = 256 * 1024 * 1024


class Refused(Exception):
    """A rule said no. Carries the HTTP status the real store would answer with."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class Blob:
    """One readable object. Either real bytes, or a synthetic body nobody buffers."""

    sha256: str
    size: int
    data: bytes | None = None

    @classmethod
    def of(cls, data: bytes) -> 'Blob':
        return cls(sha256=hashlib.sha256(data).hexdigest(), size=len(data), data=data)

    @classmethod
    def synthetic(cls, size: int) -> 'Blob':
        """A blob of ``size`` bytes that exists only as a rule for generating them."""
        digest = hashlib.sha256()
        remaining = size
        while remaining > 0:
            chunk = SYNTHETIC_BLOCK[: min(remaining, len(SYNTHETIC_BLOCK))]
            digest.update(chunk)
            remaining -= len(chunk)
        return cls(sha256=digest.hexdigest(), size=size, data=None)

    def chunks(self):
        if self.data is not None:
            yield self.data
            return
        remaining = self.size
        while remaining > 0:
            chunk = SYNTHETIC_BLOCK[: min(remaining, len(SYNTHETIC_BLOCK))]
            yield chunk
            remaining -= len(chunk)


@dataclass
class Request:
    """One thing the step asked for, as the endpoint saw it."""

    kind: str  #: ``'manifest'`` | ``'input'`` | ``'upload'``
    name: str  #: blob name for a read, object key for an upload
    token: str  #: the credential generation the caller presented
    index: int  #: 1-based counter within this kind
    size: int = 0  #: uploaded byte count, for an upload
    #: When the endpoint received this request's HEADERS — for an upload, before a single
    #: byte of the body was read. Expiry is judged against THIS, not against "now": S3
    #: authorizes a request when it arrives, and a transfer that was authorized does not
    #: become unauthorized because its body took a while to arrive.
    arrived_at: float = 0.0
    fields: dict = field(default_factory=dict)  #: the POST form fields, for an upload


@dataclass
class Upload:
    """One accepted upload, stamped with the order it arrived in."""

    order: int
    key: str
    relpath: str
    size: int
    sha256: str
    token: str
    at: float


@dataclass
class Rejection:
    """One refused request, so a test can prove a fence was exercised."""

    kind: str
    name: str
    token: str
    status: int
    code: str


@dataclass
class _Hooks:
    """Test-registered behaviour, at the ONE point where behaviour can be injected.

    One hook point, deliberately. There used to be three — before the request, after the
    body had been read, and after the response had been written — and the last two
    existed only to express things that cannot happen: a request being un-authorized
    after it was accepted, and a rotation landing at a moment the client could observe
    before the harness did. Both produced tests that a correct node could fail. A hook
    that fires while the request is still being authorized can express everything the
    suite legitimately needs: refuse it, hold it open, or publish a fresh ``creds.json``
    while it is in flight.
    """

    #: Consulted before the request is authorized. May raise :class:`Refused`, sleep, or
    #: rotate credentials. Hooks run in the order they were appended, which is how a test
    #: says "publish the replacement, THEN hold the response open". Return value ignored.
    on_request: list[Callable[['Endpoint', Request], None]] = field(default_factory=list)


class Endpoint:
    """The fake store. Start it, hand its URLs to a credential envelope, read the log."""

    def __init__(self, *, max_object_bytes: int = DEFAULT_MAX_OBJECT_BYTES):
        self.blobs: dict[str, Blob] = {}
        self.uploads: list[Upload] = []
        self.rejections: list[Rejection] = []
        self.requests: list[Request] = []
        self.key_prefix = ''
        self.max_object_bytes = max_object_bytes
        self.hooks = _Hooks()

        self._bodies: dict[int, bytes] = {}
        self._lock = threading.Lock()
        #: How many requests have been noted and not yet answered, and the condition a
        #: test waits on for that to reach zero. See :meth:`settle`.
        self._inflight = 0
        self._idle = threading.Condition(self._lock)
        self._counters: dict[str, int] = {}
        self._tokens: list[str] = []
        #: token → the monotonic instant it stops being accepted (``inf`` = never).
        self._expiry: dict[str, float] = {}
        #: token → the wall-clock expiry the envelope will STATE, so a step can act on it.
        self._stated_expiry: dict[str, datetime | None] = {}
        #: token → the POST fields issued with it, so an upload can be held to them.
        self._policies: dict[str, dict[str, str]] = {}
        #: Called with the new token whenever :meth:`refresh` runs — the harness wires
        #: this to the atomic replacement of ``creds.json``.
        self.on_rotate: Callable[[str], None] = lambda token: None
        #: Set the first time each kind of request is seen, so a test can synchronise
        #: (send a signal exactly while a GET or a POST is in flight, for instance).
        self.seen: dict[str, threading.Event] = {}

        self._server = ThreadingHTTPServer(('0.0.0.0', 0), _make_handler(self))
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> 'Endpoint':
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> 'Endpoint':
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ---------------------------------------------------------------- credentials

    def mint(self, ttl_s: float | None = None) -> str:
        """Issue a new credential generation. **Earlier ones keep working until they expire.**

        That is the whole correction to this endpoint's credential model. Presigning is a
        signature over a deadline: the issuer cannot take one back, and re-signing does
        not invalidate what it already handed out. A step holding an envelope that has not
        expired is entitled to keep using it, and a harness that punished that would be
        demanding something the platform does not.

        Args:
            ttl_s: Seconds this generation is good for. ``None`` means it never expires,
                which is what every test that is not ABOUT expiry wants.
        """
        with self._lock:
            token = f'gen-{len(self._tokens) + 1}'
            self._tokens.append(token)
            self._expiry[token] = math.inf if ttl_s is None else time.monotonic() + ttl_s
            self._stated_expiry[token] = (
                None if ttl_s is None else datetime.now(timezone.utc) + timedelta(seconds=ttl_s)
            )
        return token

    def refresh(self, ttl_s: float | None = None) -> str:
        """Issue a fresh generation AND publish it — the atomic ``creds.json`` swap.

        This is what the agent does before an envelope expires (``agent/creds.py``:
        "Refresh before expiry, not after"). It replaces the file the container is
        reading; it does not touch the credential the container may already be holding.
        """
        token = self.mint(ttl_s)
        self.on_rotate(token)
        return token

    @property
    def token(self) -> str:
        """The most recently issued generation."""
        with self._lock:
            return self._tokens[-1] if self._tokens else ''

    def stated_expiry(self, token: str) -> datetime | None:
        """The wall-clock expiry an envelope for ``token`` should declare."""
        with self._lock:
            return self._stated_expiry.get(token)

    def is_live(self, token: str, at: float | None = None) -> bool:
        """Whether ``token`` was still valid at monotonic instant ``at`` (default: now)."""
        moment = time.monotonic() if at is None else at
        with self._lock:
            return token in self._expiry and moment < self._expiry[token]

    # ---------------------------------------------------------------------- urls

    def base_url(self, host: str) -> str:
        return f'http://{host}:{self.port}'

    def get_url(self, host: str, name: str, token: str) -> str:
        return f'{self.base_url(host)}/read/{quote(name, safe="")}?tok={token}'

    def post_policy(self, host: str, token: str, key_prefix: str) -> dict:
        """The presigned-POST document, shaped exactly like botocore's.

        ``key`` carries the ``${filename}`` placeholder the real policy uses, and the
        caller is expected to replace it with the full object key. ``key_prefix`` is
        the fence: this endpoint refuses any key outside it, the way S3's
        ``["starts-with", "$key", prefix]`` condition does.

        **Every other field is remembered and enforced on upload.** A real POST is
        accepted only if the form carries the policy document and the signature that was
        computed over it; a step that forwards the URL and drops the rest gets a 403 from
        S3 and used to get a 204 from here, which would have let a genuinely broken node
        pass this harness and fail in production.
        """
        self.key_prefix = key_prefix
        fields = {
            'key': key_prefix + '${filename}',
            'tok': token,
            'policy': 'ZmFrZS1wb2xpY3k=',
            'x-amz-algorithm': 'AWS4-HMAC-SHA256',
            'x-amz-credential': f'AKIACONFORMANCE/20260808/us-east-1/s3/aws4_request/{token}',
            'x-amz-date': '20260808T000000Z',
            'x-amz-signature': f'signature-for-{token}',
        }
        with self._lock:
            self._policies[token] = dict(fields)
        return {'url': f'{self.base_url(host)}/upload', 'fields': dict(fields), 'key_prefix': key_prefix}

    # --------------------------------------------------------------- observation

    def keys_in_order(self) -> list[str]:
        """Every accepted upload's relpath, in the order the store received it."""
        return [upload.relpath for upload in self.uploads]

    def uploaded(self, relpath: str) -> Upload | None:
        """The LAST accepted upload of ``relpath`` — which is what S3 would serve.

        Last, not first. An object store exposes the most recent successful write; a
        harness that answered with the first one could bless bytes that were replaced, or
        reject a legitimate re-upload (a step that retried a refused transfer, say) for
        disagreeing with a version nobody can read any more.
        """
        for upload in reversed(self.uploads):
            if upload.relpath == relpath:
                return upload
        return None

    def last_index(self, relpath: str) -> int | None:
        """Position of the last accepted upload of ``relpath`` in :meth:`keys_in_order`."""
        keys = self.keys_in_order()
        for position in range(len(keys) - 1, -1, -1):
            if keys[position] == relpath:
                return position
        return None

    def names_of(self, kind: str) -> list[str]:
        """The DISTINCT names requested for one kind, in first-seen order.

        Counting requests answers "how much traffic was there"; counting distinct names
        answers "how much of the work was actually done". Three retries of one input are
        three requests and one input, and a test about coverage means the second.
        """
        seen: list[str] = []
        for request in list(self.requests):
            if request.kind == kind and request.name not in seen:
                seen.append(request.name)
        return seen

    def body_of(self, relpath: str) -> bytes:
        upload = self.uploaded(relpath)
        if upload is None:
            raise AssertionError(f'{relpath!r} was never uploaded; got {self.keys_in_order()}')
        return self._bodies[upload.order]

    def wait_for(self, kind: str, timeout: float = 30.0) -> bool:
        """Block until a request of ``kind`` has been received. For signal timing."""
        event = self.seen.setdefault(kind, threading.Event())
        return event.wait(timeout)

    def settle(self, timeout: float = 60.0) -> bool:
        """Block until nothing is in flight — every request received has been answered.

        **Read the ledger only after this returns.** This store, like a real one, commits
        an upload once it has the bytes and the policy allows them, and answers
        afterwards; killing the client in between does not un-write the object, it only
        means nobody hears the acknowledgement. So the set of objects a cancelled run
        leaves behind is not final at the instant the container dies — it is final once
        the store has finished with what it was already holding. A test that snapshots
        before that is asking a question about a moment the collector will never see.

        Returns:
            ``True`` if the store went quiet inside ``timeout``. ``False`` means
            something is still being handled, and every reading taken afterwards is of a
            store that is still changing — which a caller should treat as a failure
            rather than as an answer.
        """
        with self._idle:
            return self._idle.wait_for(lambda: self._inflight == 0, timeout=timeout)

    # ------------------------------------------------------------------ internals

    def _note(
        self,
        kind: str,
        name: str,
        token: str,
        size: int = 0,
        fields: dict | None = None,
        arrived_at: float | None = None,
    ) -> Request:
        """Record one request and put it on the in-flight ledger.

        ``arrived_at`` defaults to now, which is right for a GET — the harness knows
        everything about it the moment the headers land. An upload's identity is inside
        its body, so a POST is noted only after reading it and must pass the instant it
        really arrived, or a credential that expired mid-body would refuse a transfer
        that began while it was alive.
        """
        with self._lock:
            self._counters[kind] = self._counters.get(kind, 0) + 1
            index = self._counters[kind]
            self._inflight += 1
        request = Request(
            kind=kind,
            name=name,
            token=token,
            index=index,
            size=size,
            arrived_at=time.monotonic() if arrived_at is None else arrived_at,
            fields=dict(fields or {}),
        )
        self.requests.append(request)
        self.seen.setdefault(kind, threading.Event()).set()
        return request

    @contextmanager
    def _handling(self, kind: str, name: str, token: str, **noted):
        """Note a request, and take it off the in-flight ledger however it ends."""
        request = self._note(kind, name, token, **noted)
        try:
            yield request
        finally:
            with self._idle:
                self._inflight -= 1
                if self._inflight == 0:
                    self._idle.notify_all()

    def count_of(self, kind: str) -> int:
        with self._lock:
            return self._counters.get(kind, 0)

    def _run(self, hooks, request: Request) -> None:
        for hook in list(hooks):
            hook(self, request)

    def _check_token(self, request: Request) -> None:
        """Refuse a credential that had already expired when this request ARRIVED.

        Judged at arrival, ONCE, the way S3 authorizes a request when it receives it. A
        transfer that was authorized cannot fail retroactively for taking too long, and
        nothing here can un-authorize one — an issuer signs a deadline and cannot take it
        back.
        """
        if self.is_live(request.token, at=request.arrived_at):
            return
        raise Refused(
            403,
            'AccessDenied',
            f'the credential {request.token!r} presented for {request.kind} {request.name!r} had already '
            f'expired when the request arrived; the newest generation is {self.token!r}',
        )

    def _check_post_fields(self, request: Request) -> None:
        """Hold an upload to the whole policy it was signed with, not just its prefix.

        Real S3 validates ``policy`` against ``x-amz-signature`` and refuses if either is
        missing or altered. Checking only the key prefix here would accept a step that
        forwarded the URL and none of the fields — which S3 answers with 403, and which
        this harness would have called a pass.
        """
        with self._lock:
            issued = dict(self._policies.get(request.token, {}))
        if not issued:
            return  # nothing was ever signed for this credential; the token check owns that
        wrong = sorted(
            name
            for name, value in issued.items()
            if name != 'key' and request.fields.get(name) != value
        )
        if wrong:
            raise Refused(
                403,
                'AccessDenied',
                f'the upload of {request.name!r} did not present the presigned POST fields it was signed '
                f'with — {wrong} are missing or altered. S3 validates the policy and its signature, so a '
                f'step that forwards the URL and drops the rest of the form is refused there too',
            )

    def _record_upload(self, request: Request, key: str, payload: bytes) -> None:
        """Commit one accepted upload, stamped with the order it arrived in."""
        with self._lock:
            order = len(self.uploads) + 1
            self.uploads.append(
                Upload(
                    order=order,
                    key=key,
                    relpath=key[len(self.key_prefix):],
                    size=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                    token=request.token,
                    at=time.monotonic(),
                )
            )
            self._bodies[order] = payload

    def _record_rejection(self, request: Request, refused: Refused) -> None:
        self.rejections.append(
            Rejection(kind=request.kind, name=request.name, token=request.token, status=refused.status,
                      code=refused.code)
        )


def _make_handler(endpoint: 'Endpoint'):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        server_version = 'fake-s3/1.0'

        def log_message(self, *args) -> None:  # keep pytest output readable
            pass

        def handle_error(self, *args) -> None:
            """A client that vanished mid-request is a RESULT here, not an error.

            Half of what this endpoint exists to observe is a container being killed
            while a transfer is in flight, which reaches the server as a reset
            connection. Printing a traceback for each one would bury the test output
            under the very thing the test arranged.
            """
            pass

        # ------------------------------------------------------------------ reads

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            token = (parse_qs(parsed.query).get('tok') or [''])[0]
            name = _unquote(parsed.path[len('/read/'):]) if parsed.path.startswith('/read/') else ''
            blob = endpoint.blobs.get(name)
            kind = 'manifest' if name == 'invocation.json' else 'input'
            with endpoint._handling(kind, name, token) as request:
                try:
                    endpoint._run(endpoint.hooks.on_request, request)
                    endpoint._check_token(request)
                    if blob is None:
                        raise Refused(404, 'NoSuchKey', f'no object named {name!r}')
                except Refused as refused:
                    endpoint._record_rejection(request, refused)
                    self._error(refused)
                    return
                self._send_blob(blob)

        def _send_blob(self, blob: Blob) -> None:
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(blob.size))
            self.end_headers()
            try:
                for chunk in blob.chunks():
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the step died mid-transfer; that is a result, not an error here

        # ---------------------------------------------------------------- uploads

        def do_POST(self) -> None:
            """Accept one presigned POST.

            The arrival instant is taken HERE, before a single byte of the body is read,
            and carried into the request record. S3 authorizes what it receives; a
            credential that runs out while the bytes are still coming in does not refuse
            a transfer that began inside its lifetime, and stamping arrival after the
            body — which this endpoint used to do — quietly made it do exactly that.
            """
            arrived_at = time.monotonic()
            length = int(self.headers.get('Content-Length') or 0)
            if length > endpoint.max_object_bytes + (1 << 20):
                with endpoint._handling('upload', '<oversized>', '', arrived_at=arrived_at) as request:
                    refused = Refused(400, 'EntityTooLarge', f'body of {length} bytes exceeds the policy ceiling')
                    endpoint._record_rejection(request, refused)
                    self._error(refused)
                return
            raw = self.rfile.read(length)
            fields, payload = _parse_multipart(self.headers.get('Content-Type', ''), raw)
            key = fields.get('key', '')
            with endpoint._handling(
                'upload',
                key,
                fields.get('tok', ''),
                size=len(payload),
                fields=fields,
                arrived_at=arrived_at,
            ) as request:
                self._store_or_refuse(request, key, payload)

        def _store_or_refuse(self, request: Request, key: str, payload: bytes) -> None:
            """Authorize one upload against the whole policy, then commit it — or refuse."""
            try:
                endpoint._run(endpoint.hooks.on_request, request)
                endpoint._check_token(request)
                endpoint._check_post_fields(request)
                if not key.startswith(endpoint.key_prefix):
                    raise Refused(
                        403,
                        'AccessDenied',
                        f'key {key!r} is outside this launch\'s prefix {endpoint.key_prefix!r}; the policy '
                        f'condition ["starts-with", "$key", prefix] refuses it',
                    )
                if len(payload) > endpoint.max_object_bytes:
                    raise Refused(400, 'EntityTooLarge', f'object of {len(payload)} bytes exceeds the policy ceiling')
            except Refused as refused:
                endpoint._record_rejection(request, refused)
                self._error(refused)
                return
            endpoint._record_upload(request, key, payload)
            self.send_response(204)
            self.send_header('Content-Length', '0')
            self.end_headers()

        # ----------------------------------------------------------------- errors

        def _error(self, refused: Refused) -> None:
            body = (
                f'<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<Error><Code>{refused.code}</Code><Message>{refused.message}</Message></Error>'
            ).encode('utf-8')
            self.send_response(refused.status)
            self.send_header('Content-Type', 'application/xml')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _unquote(value: str) -> str:
    from urllib.parse import unquote

    return unquote(value)


def _parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], bytes]:
    """Split a ``multipart/form-data`` body into its text fields and its one file part.

    Uses the stdlib ``email`` parser rather than a hand-rolled boundary scan: ``cgi``
    is gone in Python 3.13, and boundary handling is exactly the kind of thing that
    looks right until a payload happens to contain the boundary bytes.
    """
    header = f'Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n'.encode('utf-8')
    message = BytesParser(policy=HTTP).parsebytes(header + body)
    fields: dict[str, str] = {}
    payload = b''
    for part in message.iter_parts() if message.is_multipart() else []:
        disposition = part.get('Content-Disposition', '')
        name = part.get_param('name', header='content-disposition') or ''
        content = part.get_payload(decode=True) or b''
        if 'filename' in disposition:
            payload = content
        else:
            fields[str(name)] = content.decode('utf-8', 'replace')
    return fields, payload


# --------------------------------------------------------------------------- hooks
# Small factories so a test says WHAT it wants to happen and WHEN, in one line, instead
# of restating the plumbing. Each returns a callable to append to one of the three hook
# lists on :class:`Endpoint.hooks`.


def matching(kind: str, index: int | None = None, name: str | None = None):
    """Predicate over a :class:`Request`: its kind, optionally its position and name."""

    def predicate(request: Request) -> bool:
        if request.kind != kind:
            return False
        if index is not None and request.index != index:
            return False
        if name is not None and request.name != name:
            return False
        return True

    return predicate


def refresh_when(predicate, *, ttl_s: float | None = None):
    """Publish a fresh ``creds.json``, the way the agent does before an envelope expires.

    It does NOT invalidate the credential the step may already be holding — nothing can.
    A step that is still inside its own expiry keeps working, which is exactly what
    production does and what an earlier version of this harness wrongly punished.

    **Append it BEFORE the hook that holds the response open.** The replacement has to be
    on disk while the request is still in flight; publishing it once the response has
    gone out is a race the step can lose through no fault of its own — it can receive the
    body and re-open ``creds.json`` before the swap lands, read the document that is
    about to be replaced, and fail. A test that intermittently fails a correct
    implementation is worse than no test.
    """

    def hook(endpoint: 'Endpoint', request: Request) -> None:
        if predicate(request):
            endpoint.refresh(ttl_s)

    return hook


def refuse_when(predicate, *, status: int = 503, code: str = 'SlowDown', message: str = 'try again'):
    """Answer with a specific HTTP status — a transient store failure, on demand."""

    def hook(endpoint: 'Endpoint', request: Request) -> None:
        if predicate(request):
            raise Refused(status, code, message)

    return hook


def delay_when(predicate, seconds: float):
    """Hold the request open, so a test can signal the container while it is in flight."""

    def hook(endpoint: 'Endpoint', request: Request) -> None:
        if predicate(request):
            time.sleep(seconds)

    return hook
