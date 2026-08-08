"""A fake object store that behaves like the presigned surface a step actually sees.

It serves presigned-style GETs (the pinned inputs and ``invocation.json``) and accepts
presigned-POST multipart uploads under one key prefix, exactly as ``runners/credentials.py``
hands them out. It is small on purpose: everything a step can observe about S3 in this
contract is a URL that works, a URL that has stopped working, and a policy that admits
one prefix and refuses everything else.

**This endpoint is deliberately STRICTER than real S3, in three ways, and the difference
is the point.**

1. *It can see the order objects arrived in.* Real S3 has no notion of "you wrote the
   completion marker before you wrote the object it inventories". Every upload here is
   stamped with a monotonic sequence number, so "the marker is written LAST" — the single
   most important ordering rule in the contract, and the one that makes a half-finished
   run distinguishable from a complete one — becomes a testable fact instead of a hope.
   Nothing in production can catch a step that gets this wrong; the collector will happily
   publish an inventory whose objects landed after it. This harness is the only place it
   can be caught, which is why it is caught here.

2. *A credential can be invalidated on command, deterministically.* Real credentials expire
   on a wall clock, so a test for "the step must reload its credentials before every
   transfer" would either take fifteen minutes or depend on a clock somebody can skew.
   Here a credential stops working because the test said so, at a point in the traffic the
   test chose — see :meth:`Endpoint.rotate`.

3. *The prefix fence is checked here rather than assumed.* Real S3 enforces the policy's
   ``starts-with`` condition and returns 403. So does this. The difference is that this
   one records the refusal, so a test can prove the fence was exercised rather than merely
   never tripped.

What it does NOT emulate: AWS signature verification (the "signature" here is an opaque
token naming a credential generation), request-time skew, multipart *chunked* uploads,
versioning, or eventual consistency. None of those change what the node must do.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
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
    """Test-registered behaviour, in the two places where behaviour can be injected."""

    #: Consulted before anything else happens. May raise :class:`Refused`, sleep, or
    #: rotate credentials. Return value ignored.
    on_request: list[Callable[['Endpoint', Request], None]] = field(default_factory=list)
    #: Consulted once the request body is fully read but before a response is written.
    #: This is where "the credential expired mid-operation" lives: the store accepted
    #: the bytes and then refused, which is exactly what an expiring policy looks like.
    on_accepted: list[Callable[['Endpoint', Request], None]] = field(default_factory=list)
    #: Consulted after the response has been fully written. This is where a rotation
    #: that must NOT affect the request it follows lives — "the credentials changed the
    #: moment the last input finished downloading".
    on_responded: list[Callable[['Endpoint', Request], None]] = field(default_factory=list)


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
        self._counters: dict[str, int] = {}
        self._tokens: list[str] = []
        self._live: set[str] = set()
        #: Called with the new token whenever :meth:`rotate` runs — the harness wires
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

    def mint(self) -> str:
        """Issue a new credential generation and invalidate every earlier one.

        Generations, not clocks. ``A`` becomes ``B`` because a test said so at a point
        in the traffic it chose, so "did the step reload its credentials before this
        transfer?" has a deterministic answer instead of a racy one.
        """
        with self._lock:
            token = f'gen-{len(self._tokens) + 1}'
            self._tokens.append(token)
            self._live = {token}
        return token

    def rotate(self) -> str:
        """Mint the next generation AND publish it — the atomic ``creds.json`` swap."""
        token = self.mint()
        self.on_rotate(token)
        return token

    @property
    def token(self) -> str:
        """The generation currently accepted."""
        with self._lock:
            return self._tokens[-1] if self._tokens else ''

    def is_live(self, token: str) -> bool:
        with self._lock:
            return token in self._live

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
        """
        self.key_prefix = key_prefix
        return {
            'url': f'{self.base_url(host)}/upload',
            'fields': {
                'key': key_prefix + '${filename}',
                'tok': token,
                'policy': 'ZmFrZS1wb2xpY3k=',
                'x-amz-algorithm': 'AWS4-HMAC-SHA256',
                'x-amz-signature': f'signature-for-{token}',
            },
            'key_prefix': key_prefix,
        }

    # --------------------------------------------------------------- observation

    def keys_in_order(self) -> list[str]:
        """Every accepted upload's relpath, in the order the store received it."""
        return [upload.relpath for upload in self.uploads]

    def uploaded(self, relpath: str) -> Upload | None:
        for upload in self.uploads:
            if upload.relpath == relpath:
                return upload
        return None

    def body_of(self, relpath: str) -> bytes:
        upload = self.uploaded(relpath)
        if upload is None:
            raise AssertionError(f'{relpath!r} was never uploaded; got {self.keys_in_order()}')
        return self._bodies[upload.order]

    def wait_for(self, kind: str, timeout: float = 30.0) -> bool:
        """Block until a request of ``kind`` has been received. For signal timing."""
        event = self.seen.setdefault(kind, threading.Event())
        return event.wait(timeout)

    # ------------------------------------------------------------------ internals

    def _note(self, kind: str, name: str, token: str, size: int = 0) -> Request:
        with self._lock:
            self._counters[kind] = self._counters.get(kind, 0) + 1
            index = self._counters[kind]
        request = Request(kind=kind, name=name, token=token, index=index, size=size)
        self.requests.append(request)
        self.seen.setdefault(kind, threading.Event()).set()
        return request

    def count_of(self, kind: str) -> int:
        with self._lock:
            return self._counters.get(kind, 0)

    def _run(self, hooks, request: Request) -> None:
        for hook in list(hooks):
            hook(self, request)

    def _check_token(self, request: Request) -> None:
        if not self.is_live(request.token):
            raise Refused(
                403,
                'AccessDenied',
                f'the credential {request.token!r} presented for {request.kind} {request.name!r} has been '
                f'superseded; the live generation is {self.token!r}',
            )

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
            request = endpoint._note(kind, name, token)
            try:
                endpoint._run(endpoint.hooks.on_request, request)
                endpoint._check_token(request)
                if blob is None:
                    raise Refused(404, 'NoSuchKey', f'no object named {name!r}')
                endpoint._run(endpoint.hooks.on_accepted, request)
                # Asked again AFTER the accept hooks, so "the credential expired between
                # the store accepting this request and answering it" is expressible: a
                # hook rotates, and the answer is the 403 a real expiring policy gives.
                endpoint._check_token(request)
            except Refused as refused:
                endpoint._record_rejection(request, refused)
                self._error(refused)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(blob.size))
            self.end_headers()
            try:
                for chunk in blob.chunks():
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the step died mid-transfer; that is a result, not an error here
            endpoint._run(endpoint.hooks.on_responded, request)

        # ---------------------------------------------------------------- uploads

        def do_POST(self) -> None:
            length = int(self.headers.get('Content-Length') or 0)
            if length > endpoint.max_object_bytes + (1 << 20):
                request = endpoint._note('upload', '<oversized>', '')
                refused = Refused(400, 'EntityTooLarge', f'body of {length} bytes exceeds the policy ceiling')
                endpoint._record_rejection(request, refused)
                self._error(refused)
                return
            raw = self.rfile.read(length)
            fields, payload = _parse_multipart(self.headers.get('Content-Type', ''), raw)
            key = fields.get('key', '')
            token = fields.get('tok', '')
            request = endpoint._note('upload', key, token, size=len(payload))
            try:
                endpoint._run(endpoint.hooks.on_request, request)
                endpoint._check_token(request)
                if not key.startswith(endpoint.key_prefix):
                    raise Refused(
                        403,
                        'AccessDenied',
                        f'key {key!r} is outside this launch\'s prefix {endpoint.key_prefix!r}; the policy '
                        f'condition ["starts-with", "$key", prefix] refuses it',
                    )
                if len(payload) > endpoint.max_object_bytes:
                    raise Refused(400, 'EntityTooLarge', f'object of {len(payload)} bytes exceeds the policy ceiling')
                endpoint._run(endpoint.hooks.on_accepted, request)
                endpoint._check_token(request)
            except Refused as refused:
                endpoint._record_rejection(request, refused)
                self._error(refused)
                return
            with endpoint._lock:
                order = len(endpoint.uploads) + 1
                endpoint.uploads.append(
                    Upload(
                        order=order,
                        key=key,
                        relpath=key[len(endpoint.key_prefix):],
                        size=len(payload),
                        sha256=hashlib.sha256(payload).hexdigest(),
                        token=token,
                        at=time.monotonic(),
                    )
                )
                endpoint._bodies[order] = payload
            self.send_response(204)
            self.send_header('Content-Length', '0')
            self.end_headers()
            endpoint._run(endpoint.hooks.on_responded, request)

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


def rotate_when(predicate):
    """Replace ``creds.json`` and invalidate every earlier generation."""

    def hook(endpoint: 'Endpoint', request: Request) -> None:
        if predicate(request):
            endpoint.rotate()

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
