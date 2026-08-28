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
4. **It handles a stop request, and notices it without waiting for the network.** This
   process is PID 1 in its container, and Linux gives process 1 no default signal
   handling: without a handler, SIGTERM is discarded entirely and the step runs to
   completion for a run nobody will collect. Installing the handler is only half of it —
   a step that installs one and then sits in a socket call until it times out has spent
   the whole of a grace it was never promised, so the handler also abandons the transfer
   in flight. See :class:`_StoppableTransport` for what that takes and what the obvious
   version of it does instead, which is nothing.
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
import http.client
import io
import json
import logging
import os
import re
import signal
import socket
import sys
import tempfile
import threading
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

#: Socket timeout for one read. It bounds a store that has gone quiet — NOT how long a
#: stop takes to be noticed, which is what this constant used to claim. A stop is noticed
#: at once, because the handler shuts the socket down underneath the call
#: (:class:`_StoppableTransport`); this number is what is left for the case where nobody
#: signalled anything and the other side simply stopped answering.
READ_TIMEOUT_S = 25.0
#: An ELAPSED deadline for getting a connection — the name lookup, every address tried,
#: and the TLS handshake that follows, together. It exists because that whole stretch is
#: the one a stop CANNOT interrupt: it hands out no socket anybody else can reach, and
#: Python's ``ssl`` detaches the plain socket while it wraps it, so shutting that down
#: raises "Bad file descriptor" rather than ending the handshake (measured). What cannot
#: be interrupted has to be bounded.
#:
#: **Elapsed, and that word is the whole of it.** Passing a timeout to
#: ``socket.create_connection`` does NOT bound this: it resolves the name first, with no
#: timeout applied to that at all, and then applies the value **separately to each address
#: it got back** — so a slow resolver is unbounded and a host with four addresses can take
#: four times what you thought you asked for. :func:`_connect_within` is what makes one
#: number mean one number. The handshake that follows inherits whatever is left of it.
#:
#: **Ten seconds, and it is chosen against what a real job needs, not against what makes a
#: test quick.** Reaching an object store is milliseconds, so this is enormous headroom —
#: deliberately, because the cost of being wrong is asymmetric in a way that is easy to get
#: backwards. A deadline that fires on a healthy-but-slow connect **kills the whole job**:
#: there is no automatic retry engine for external steps, every failed attempt is recorded
#: as transient whatever this process returns, and somebody has to notice and retry it by
#: hand (``docs/PROTOCOL.md`` section 6). A deadline that is generous costs, at worst, ten
#: seconds of a stop nobody was promised any of. Ten is the point where a stall is
#: unambiguous and a working network is nowhere near.
CONNECT_DEADLINE_S = 10.0
#: Socket timeout for one upload — deliberately shorter, because an upload's ending is the
#: ambiguous one. Once the body has been sent the store may already have committed the
#: object, so waiting longer buys only a clearer answer about something that has already
#: happened, and this step has recorded that object either way.
UPLOAD_TIMEOUT_S = 10.0
#: An ELAPSED deadline for writing the completion receipt, covering the connection and the
#: upload together. The receipt is the one thing this step will not abandon on a stop, so
#: it is the one thing that needs its own clock: :data:`UPLOAD_TIMEOUT_S` bounds a socket
#: going QUIET, not a transfer taking long, and a peer that sends a byte every few seconds
#: keeps a connection alive for as long as it likes.
#:
#: **Twenty seconds, against a grace of thirty that nobody promises.** Thirty is the
#: agent's own constant, hardcoded as a default and passed by no caller, so it is the most
#: the polite path can ever give (``agent/executors/docker_exec.py`` ``stop``); a fence
#: gives zero, and a cancellation may not even be noticed until the next heartbeat. Twenty
#: leaves the process room to exit and say why before the kill lands, and a receipt that
#: cannot be written in twenty seconds was not going to be written.
RECEIPT_DEADLINE_S = 20.0
#: Every streaming copy moves this much at a time.
CHUNK_BYTES = 1024 * 1024
#: How much a streaming copy may leave in the kernel's page cache before asking for it
#: back. See :func:`_release_page_cache` — this is not a performance knob.
CACHE_DROP_BYTES = 8 * 1024 * 1024
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

#: The transport of the request in flight — one, because this program makes exactly one
#: request at a time. A copy of this file that overlaps requests needs a set here instead.
_IN_FLIGHT: list = []

#: Whether the handler may still abandon what is in flight. It may not once the receipt is
#: being written: by then there is nothing left to rescue by cutting a connection, and a
#: whole run's account of itself to lose. A second stop CAN arrive — the agent asks for one
#: from three different places — and the kill behind it is what bounds this wait anyway.
_ABANDONABLE = True


class _StoppableTransport:
    """An HTTP connection the stop handler can reach — for every wait that can be reached.

    Mixed into whatever connection class urllib chose (:func:`_stoppable_version_of`).
    This file's first attempt kept the RESPONSE object on the ledger and called
    ``close()`` on it, and that fails twice over:

    * **Too late.** A response exists only once the store has begun to answer, so a stop
      landing while the step was still waiting for the first byte of a GET reached nothing
      at all, and was noticed only when the socket timed out — measured at the full length
      of a held-open response, out of a grace that is nominally thirty seconds and
      guaranteed to be nothing at all.
    * **Wrong call.** ``close()`` does not interrupt a read that is ALREADY blocked.
      Measured: mid-body it raises ``RuntimeError: reentrant call inside
      <_io.BufferedReader>`` from inside the handler — where the exception is swallowed —
      and the read then waits out its whole timeout regardless. ``shutdown()`` makes the
      pending call return immediately, which is the entire point of keeping this ledger.

    **The same question has to be asked of every OTHER wait on this path**, which is what
    the shape below is about. There are four, and they are not alike:

    1. **DNS, the TCP connect and the TLS handshake.** All three happen inside one call,
       which hands out no socket anybody else can reach: ``ssl`` detaches the plain socket
       while it wraps it, so shutting that down raises ``OSError: [Errno 9] Bad file
       descriptor`` instead of ending the handshake (measured — the handshake then ran to
       the full read timeout regardless). Nothing here can be interrupted, so it is
       BOUNDED instead, by :data:`CONNECT_DEADLINE_S` — one ELAPSED deadline over the
       lookup, every address and the handshake, because the timeout the standard library
       accepts here is none of those things (:func:`_connect_within`). The flag is
       re-checked the instant the call returns, so a stop that arrived during it does not
       go on to start a request nobody wants.
    2. **Waiting for the store to begin answering.** Interruptible, and the reason the
       connection goes on the ledger before a byte is sent rather than after.
    3. **Reading the body.** Interruptible — but only if the connection is still ON the
       ledger, which is why there is no ``close()`` override here. ``http.client``
       calls ``close()`` on the connection as soon as the headers of a ``will_close``
       response are parsed (and urllib sets ``Connection: close`` on every request), so
       removing the entry there would empty the ledger for the whole of the body.
    4. **Waiting for a store to acknowledge an upload.** Interruptible, same mechanism.

    The socket is also remembered in an attribute of our own, because urllib drops its
    reference to it the moment the response exists (``h.sock = None``, in
    ``AbstractHTTPHandler.do_open``) while the response goes on reading through it.
    """

    def connect(self):
        # On the ledger BEFORE the call that blocks rather than after it. Be exact about
        # what that buys, because the obvious claim is wrong: nothing can be shut down
        # during the call below — there is no socket yet, and the one ``ssl`` builds is
        # detached from this object while it handshakes (note 1). What bounds that stretch
        # is the deadline, and what acts on a stop is the check after it. The entry is
        # still made here because the ledger should never say "nothing in flight" while a
        # transfer is being set up, and it costs one assignment.
        _IN_FLIGHT[:] = [self]
        wanted = self.timeout
        self._deadline = time.monotonic() + CONNECT_DEADLINE_S
        # ``_create_connection`` is an instance attribute ``http.client`` sets in its own
        # ``__init__``, so this replaces it rather than overriding a method — a method
        # would be shadowed by that attribute and silently never called.
        self._create_connection = lambda address, timeout, source=None: _connect_within(
            address, self._deadline, source
        )
        super().connect()
        # Getting here was the deadline's business. Everything after it is a transfer, and
        # transfers get the timeout the caller asked for.
        if wanted:
            self.sock.settimeout(wanted)
        self._transport = self.sock
        if CANCELLED:
            # The stop landed inside a phase nothing could interrupt. It is over now, and
            # this is the first moment ordinary control flow gets a say: do not go on to
            # send a request for a run that has been called off. The receipt is exempt —
            # it is the one request a cancelled run still has to make.
            if _ABANDONABLE:
                raise _Stopped('stop requested while this connection was being made')

    def _tunnel(self):
        """Granting the tunnel spends the deadline too, so what follows gets what is LEFT.

        With a proxy configured there are TWO waits inside one ``connect``: the proxy's
        answer to ``CONNECT``, and then the TLS handshake through it. The socket's timeout
        was set once, on the way out of the TCP connect, and TLS would otherwise re-use
        that whole value — so a proxy taking nine seconds of a ten-second deadline left the
        handshake nearly ten more, and the number meant nothing again. One deadline, read
        twice.
        """
        super()._tunnel()
        deadline = getattr(self, '_deadline', None)
        if deadline is not None and self.sock is not None:
            self.sock.settimeout(max(0.05, deadline - time.monotonic()))

    def stop_now(self) -> None:
        """Make whatever the main thread is waiting for on this socket return, now."""
        transport = self.sock or getattr(self, '_transport', None)
        if transport is not None:
            transport.shutdown(socket.SHUT_RDWR)


def _resolve_within(host: str, port, deadline: float) -> list:
    """Look the host up, and give up if the resolver does not answer in time.

    **The only thread in this program, and it is here because the standard library gives
    no other way.** ``socket.getaddrinfo`` takes no timeout: it is a blocking call into
    the system resolver, whose own limits come from ``/etc/resolv.conf`` and are typically
    several seconds per nameserver, tried more than once. A container with a sick resolver
    therefore stalls for tens of seconds inside a call that owns no socket — so a stop
    cannot be acted on, and the completion receipt, which by then must not be abandoned,
    cannot be written either. Handing the lookup to a thread is what turns that into a
    number.

    The thread is a daemon and is never joined beyond the deadline: a stuck lookup ends
    when the resolver finally answers, writing into a list nobody reads. That is a leak of
    one thread on a path that is already failing, and the alternative is having no bound at
    all on the phase that most often hangs.
    """
    found: list = []
    failed: list = []

    def look_up() -> None:
        try:
            found.extend(socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM))
        except Exception as exc:  # noqa: BLE001 — reported to the caller, never swallowed
            failed.append(exc)

    thread = threading.Thread(target=look_up, daemon=True)
    thread.start()
    thread.join(max(0.0, deadline - time.monotonic()))
    if found:
        return found
    if failed:
        raise TransientError(f'the name {host!r} could not be resolved: {redact(failed[0])}')
    raise TransientError(
        f'the name {host!r} was still being looked up {CONNECT_DEADLINE_S:.0f}s after this '
        f'connection was started'
    )


def _connect_within(address, deadline: float, source_address=None) -> socket.socket:
    """``socket.create_connection`` with ONE deadline over the whole thing.

    The standard library's version takes a timeout and spends it more than once: the name
    lookup happens first with nothing applied to it, and then the value is set on each
    address in turn, so four addresses mean four times the wait. This one resolves within
    the deadline and gives every attempt only what is left of it.

    The socket handed back carries the remainder as its own timeout, which is what the TLS
    handshake will then use — so the honest worst case for the whole establish phase is
    the deadline plus one handshake operation, rather than a multiple of it.
    """
    host, port = address
    refusals: list = []
    for family, kind, proto, _canonical, sockaddr in _resolve_within(host, port, deadline):
        left = deadline - time.monotonic()
        if left <= 0:
            break
        connection = socket.socket(family, kind, proto)
        try:
            connection.settimeout(left)
            if source_address:
                connection.bind(source_address)
            connection.connect(sockaddr)
        except OSError as exc:
            refusals.append(exc)
            connection.close()
            continue
        connection.settimeout(max(0.05, deadline - time.monotonic()))
        return connection
    if refusals:
        raise refusals[-1]
    raise TimeoutError(
        f'no address for {host!r} could be connected to within {CONNECT_DEADLINE_S:.0f}s'
    )


#: Cache of the stoppable subclass built for each connection class urllib hands us.
_STOPPABLE_CLASSES: dict = {}


def _stoppable_version_of(connection_class):
    """The stoppable version of one of urllib's connection classes.

    Built by subclassing whatever urllib passed rather than by naming
    ``http.client.HTTPSConnection`` here, so this file never restates the keyword
    arguments urllib gives its own connection classes — those have changed between Python
    versions, and a node that reimplemented them would break on the next one.
    """
    made = _STOPPABLE_CLASSES.get(connection_class)
    if made is None:
        made = _STOPPABLE_CLASSES[connection_class] = type(
            '_Stoppable' + connection_class.__name__, (_StoppableTransport, connection_class), {}
        )
    return made


def _on_stop(signum, _frame):
    """Set the flag, abandon the transfer in flight, and return. Nothing else.

    **RECOMMENDATION, and the one place this file reads the guidance rather than quoting
    it.** ``docs/AUTHORING.md`` says a signal handler sets a flag and does no work, "and
    especially network work". Shutting down a socket is a single non-blocking syscall: it
    starts nothing, waits for nothing and cannot block, so it is not work in the sense the
    rule is about. The reason the rule gives — that the cancellation path itself crashes —
    is why the flag is set FIRST and why the shutdown's failure is ignored: if it does not
    work, this step is exactly as stopped as it would have been without it, and ordinary
    control flow still sees the flag at its next check. Without the shutdown the flag is
    the only mechanism, and a flag cannot be read by a process parked in a socket call.
    Everything that DECIDES anything still happens in ordinary control flow.
    """
    global CANCELLED
    CANCELLED = True
    if _ABANDONABLE:
        for transport in list(_IN_FLIGHT):
            with contextlib.suppress(Exception):
                transport.stop_now()
    with contextlib.suppress(Exception):
        sys.stderr.write(f'hello-node: stop requested (signal {signum}); finishing up\n')
        sys.stderr.flush()


@contextlib.contextmanager
def _within(seconds: float, what: str):
    """Bound everything inside this block by ELAPSED time, network calls included.

    A socket timeout is not a deadline: it measures silence, so a peer that dribbles one
    byte per window holds a transfer open indefinitely without ever being idle. The alarm
    is what turns "no long silences" into "no long transfer", and it reaches a blocked
    socket call the same way the stop handler does — by shutting the transport down, which
    is the one thing that makes a syscall already in progress return.

    Used for the receipt, which is the transfer this step has promised not to abandon on a
    stop. That promise is what makes an upper bound necessary rather than merely tidy: the
    kill behind the stop arrives on its own schedule, and a step still politely waiting on
    a store when it lands has written nothing and said nothing.
    """

    def _out_of_time(_signum, _frame):
        for transport in list(_IN_FLIGHT):
            with contextlib.suppress(Exception):
                transport.stop_now()
        with contextlib.suppress(Exception):
            sys.stderr.write(f'hello-node: giving up on {what} after {seconds:.0f}s\n')
            sys.stderr.flush()

    previous = signal.signal(signal.SIGALRM, _out_of_time)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class _Stopped(Exception):
    """Raised by ordinary control flow once the flag is seen."""


def _check_stopped() -> None:
    if CANCELLED:
        raise _Stopped('stop requested')


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


def _release_page_cache(handle, *, sync: bool = False) -> None:
    """Ask the kernel to drop the pages this file has put in the cache.

    **Streaming is not enough on its own.** The container's memory limit counts the page
    cache created by its own reads and writes, so a step that never holds more than one
    block in memory can still be OOM-killed for moving a large object through a temporary
    file: the program's own footprint stays flat while the kernel's cache for that file
    grows to the size of the object. This was measured — a 128 MiB input through a 64 MiB
    container is killed without this call and survives with it.

    Dirty pages cannot be dropped, which is why a write has to be flushed and synced
    first. Reads need no sync. Both are best-effort: ``posix_fadvise`` is advice, and it
    does not exist everywhere, so a platform without it simply keeps its cache.
    """
    try:
        if sync:
            handle.flush()
            os.fsync(handle.fileno())
        os.posix_fadvise(handle.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
    except (AttributeError, OSError, ValueError):
        pass


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


class _StoppableHandler:
    """Substitute a stoppable connection for the one urllib was about to construct.

    Intercepting ``do_open`` rather than ``http_open``/``https_open`` is what keeps this
    scheme-agnostic: the plain-HTTP path is what a local demo and this repository's own
    conformance suite exercise, and the TLS path is what every presigned URL in production
    uses, so a fix that covered only the first would be invisible where it matters.
    """

    def do_open(self, http_class, req, **kwargs):
        return super().do_open(_stoppable_version_of(http_class), req, **kwargs)


class _StoppableHTTPHandler(_StoppableHandler, urllib.request.HTTPHandler):
    pass


class _StoppableHTTPSHandler(_StoppableHandler, urllib.request.HTTPSHandler):
    pass


_OPENER = urllib.request.build_opener(_NoRedirects, _StoppableHTTPHandler, _StoppableHTTPSHandler)


def _open(url: str, *, what: str):
    """GET one URL. Raises on a bad status, and never puts the URL in the message."""
    request = urllib.request.Request(url, method='GET', headers={'User-Agent': USER_AGENT})
    try:
        response = _OPENER.open(request, timeout=READ_TIMEOUT_S)
    except _Stopped:
        raise  # the connection refused to start because this run was called off
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
        self._uncached = 0
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
                    self._uncached += len(piece)
                    if self._uncached >= CACHE_DROP_BYTES:
                        # Reading the file back fills the page cache just as writing it
                        # did, and that cache counts against the container's memory limit.
                        _release_page_cache(self._handle)
                        self._uncached = 0
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
        # ``closing`` releases the file handle the body streams from, on every path out.
        # The transfer itself is abandoned through the CONNECTION, which is on the ledger
        # from the moment its socket exists — before a byte of this body is sent.
        with contextlib.closing(body):
            response = _OPENER.open(request, timeout=UPLOAD_TIMEOUT_S)
    except _Stopped:
        raise  # the connection refused to start because this run was called off
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

#: Whether the store has actually ACCEPTED ``result.json``. Being in the inventory is not
#: the same claim: the ordinary write records the document before uploading it (see
#: :func:`publish` for why that order is right for work), so an upload that failed leaves a
#: record of an object nobody holds. The failure path reads this rather than the inventory,
#: because "already named" would tell it there is nothing left to do at exactly the moment
#: there is — and the marker would then send salvage after a file that is not there.
RESULT_UPLOADED = False

#: What this run has done so far, and the facts by which somebody will find it again.
#: Both are at module scope for the same reason the inventory is: the FAILURE path writes
#: a report document too, and a run that died on its third input still touched the first
#: two — which is exactly the run an operator goes looking for.
#:
#: **BEHAVIOUR.** The orchestrator reads the optional ``facts`` list out of ``result.json``
#: when it accepts this attempt's marker, whatever that marker says, and records each
#: entry against the execution. The run is then findable in the Runs search box by
#: ``input:data.csv`` — and by ``task:41`` or ``annotation:7`` for a node that records the
#: Label Studio ids it worked on, which is the convention worth following: record the ids
#: of the things you touched, not a description of what you did.
#:
#: **RECOMMENDATION.** Nothing requires any of this, and nothing punishes getting it
#: wrong: an entry that breaks a rule below is skipped by itself and the rest are still
#: recorded, with no effect on the run. What is said about it is one summary line in the
#: ORCHESTRATOR's log, counting how many were dropped — not one line per entry, and
#: nothing that reaches this container. The rules are the orchestrator's own
#: (``pipelines/facts.py``): each entry is an object with a ``key`` matching
#: ``^[a-z][a-z0-9_]{0,63}\Z``, a ``value`` that is a string or an integer — never a
#: boolean — non-blank, at most 512 characters and free of control characters, and an
#: optional ``meta``: a FLAT object of scalars (at most 32 keys, keys of 1 to 64
#: characters, each value one string, number, boolean or null — no nesting). Those rules
#: are exactly what its columns hold, so an entry it accepts can never be refused by the
#: store afterwards and take the document's other facts with it. At most 10,000 entries are
#: recorded (``external/contract.py`` ``MAX_RESULT_FACTS``), so a node with more things than
#: that to name should record something coarser rather than one fact per row.
#:
#: **RECOMMENDATION — apply those rules HERE, where they are still visible.** ``claim``
#: below does, and that is the part worth copying. A node that writes whatever it has and
#: lets the far side sort it out gets no error, no warning it will ever see and no fact:
#: the entry is dropped inside somebody else's worker log, and the only symptom is a run
#: that answers to nothing when it is searched for six weeks later. Checking at the point
#: of writing is what turns that into a sentence in this run's own report.
FACTS: list[dict] = []
METRICS: dict[str, int] = {'files': 0, 'total_bytes': 0, 'lines': 0}

#: Why the facts this run could not claim, counted rather than listed. The reason a fact
#: is unusable is the same reason for every one of its kind — one relpath scheme that runs
#: long, one job with more inputs than the cap — so a count and a cause say everything a
#: list would, in a bounded number of characters inside a document with a ceiling.
FACTS_UNCLAIMED: dict[str, int] = {'unusable': 0, 'bad_key': 0, 'over_cap': 0}

#: The orchestrator's own ingest limits for one fact, re-stated (``pipelines/facts.py``:
#: ``KEY_RE``, ``MAX_VALUE_LEN``, ``_CONTROL_RE``; ``external/contract.py``:
#: ``MAX_RESULT_FACTS``). ``\Z`` and not ``$``, exactly as the orchestrator writes it: ``$``
#: also matches immediately before a final newline, so a key written with one would pass a
#: check made with it and then be stored with the newline attached, where no search reaches
#: it. The surrogate range is in the control-character class for a harder reason than
#: searchability — half a UTF-16 surrogate pair has no UTF-8 encoding, so the row cannot be
#: stored at all. It gets into a value the ordinary way: a name copied off a filesystem
#: whose bytes are not valid UTF-8 is decoded with surrogate escapes.
_FACT_KEY = re.compile(r'^[a-z][a-z0-9_]{0,63}\Z')
MAX_FACT_VALUE_CHARS = 512
MAX_FACTS = 10_000
_CONTROL_IN_FACT = re.compile(r'[\x00-\x1f\x7f\ud800-\udfff]')

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


def forget(relpath: str) -> None:
    """Take one relpath back out of the inventory, for a record that outlived its object.

    Used on one path only: the report document, recorded before an upload that then failed.
    The ports need no repair — :func:`write_marker` keeps only the relpaths the inventory
    still names — and a second attempt then has a free name to record again.
    """
    INVENTORY[:] = [entry for entry in INVENTORY if entry['relpath'] != relpath]


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
                    uncached = 0
                    for chunk in _stream(source, name):
                        _check_stopped()
                        digest.update(chunk)
                        size += len(chunk)
                        if _is_int(expected_size) and size > expected_size:
                            raise StepError(
                                f'input {name!r} is larger than the {expected_size} bytes the job '
                                f'pinned for it — this is not the object this run was built from')
                        target.write(chunk)
                        uncached += len(chunk)
                        if uncached >= CACHE_DROP_BYTES:
                            _release_page_cache(target, sync=True)
                            uncached = 0
                    _release_page_cache(target, sync=True)
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
        with contextlib.closing(_open(source['get_url'], what=f'input {name!r}')) as response:
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


def claim(key: str, value: str) -> None:
    """Record one search fact, or say in the report why this run could not.

    The rules applied here are the ORCHESTRATOR's, re-stated beside :data:`FACTS`: a key
    matching its grammar, a non-blank value of at most 512 characters with no control
    characters or lone surrogates, and at most 10,000 entries. It applies them itself
    rather than writing whatever it has and letting the far side sort it out, and the
    difference is entirely about who finds out. An entry the orchestrator declines is
    dropped inside a worker log this node's author will never read; the run simply answers
    to nothing when somebody searches for it, weeks later, with no error anywhere to
    explain why. Checked here, the same entry becomes a sentence
    in this run's own report, next to the facts that did get through.

    A skipped fact is never an error and never a reason to stop: the work really happened,
    and all that is lost is one way of finding it again. Counting rather than listing is
    the same discipline the orchestrator applies at its end — the reason one fact is
    unusable is the reason all of its kind are, and a per-entry list inside a document
    with a 1 MiB ceiling is a way of losing the whole document.
    """
    if len(FACTS) >= MAX_FACTS:
        FACTS_UNCLAIMED['over_cap'] += 1
        return
    # The KEY is checked as well as the value, and it is not a formality: a key is chosen in
    # this file, so a bad one is wrong for every fact of that kind on every run — the whole
    # kind vanishes from the search at once, with nothing anywhere to say so.
    if not isinstance(key, str) or not _FACT_KEY.match(key):
        FACTS_UNCLAIMED['bad_key'] += 1
        return
    text = value.strip() if isinstance(value, str) else str(value)
    if not text or len(text) > MAX_FACT_VALUE_CHARS or _CONTROL_IN_FACT.search(text):
        FACTS_UNCLAIMED['unusable'] += 1
        return
    FACTS.append({'key': key, 'value': text})


def _unclaimed_facts_note() -> str | None:
    """One sentence for the summary, or None when everything this run touched was claimed."""
    parts = []
    if FACTS_UNCLAIMED['unusable']:
        parts.append(
            f"{FACTS_UNCLAIMED['unusable']} value(s) the orchestrator would not record "
            f"(blank, over {MAX_FACT_VALUE_CHARS} characters, or carrying a control character "
            f"or half a surrogate pair)")
    if FACTS_UNCLAIMED['bad_key']:
        parts.append(
            f"{FACTS_UNCLAIMED['bad_key']} under a key the orchestrator's grammar refuses "
            f"({_FACT_KEY.pattern})")
    if FACTS_UNCLAIMED['over_cap']:
        parts.append(f"{FACTS_UNCLAIMED['over_cap']} past the {MAX_FACTS}-fact cap")
    if not parts:
        return None
    return 'not claimed as search facts: ' + '; '.join(parts)


def process(creds: Credentials, manifest: dict, scratch: str) -> dict:
    """Copy every input into ``outputs/``, count what went past, and name what it touched."""
    sources = creds.get().get('inputs') or []
    taken: set = set()

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
            # The INPUT's own name, not the copy's path. ``relpath`` is a name only this
            # step ever chose, and it would send anyone searching for the run that touched
            # ``data.csv`` looking for ``outputs/data.csv`` instead; the envelope's name is
            # what the rest of the pipeline calls that object. Recorded after the copy came
            # back accepted, so a fact is a claim about something that really happened.
            claim('input', name)
            METRICS['files'] = len(INVENTORY)
            METRICS['total_bytes'] += size
            METRICS['lines'] += _count_lines(path)

    _write_result(creds, manifest, scratch)
    progress(1.0, 'done')
    # One last look before the caller writes a receipt claiming success. A stop that
    # landed during the final upload has to end this run as the cancellation it is, and
    # the ordinary failure path — which writes a cancelled marker with this same
    # inventory — is a better place to do that than a special case afterwards.
    _check_stopped()
    return dict(METRICS)


def _count_lines(path: str) -> int:
    lines = 0
    uncached = 0
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(CHUNK_BYTES)
            if not chunk:
                _release_page_cache(handle)
                return lines
            lines += chunk.count(b'\n')
            uncached += len(chunk)
            if uncached >= CACHE_DROP_BYTES:
                _release_page_cache(handle)
                uncached = 0


def _write_result(creds: Credentials, manifest: dict, scratch: str, *,
                  inventory_first: bool = True) -> None:
    """The optional report document, kept inside the ceiling its reader is bound by.

    ``params`` is copied verbatim from whatever the pipeline author typed and is bounded
    only by the 8 MiB manifest ceiling, so echoing it into a 1 MiB document is a way to
    publish something the other side is forbidden to read. Its shape is described
    instead of reproduced.

    It goes on its own port. Everything under a port is offered to every downstream step
    wired to it, and a metrics file delivered as if it were a result is a thing downstream
    steps have to learn to ignore.

    It reads :data:`FACTS` and :data:`METRICS` rather than taking them as arguments, for
    the same reason :func:`write_marker` reads the inventory that way: the failure path
    writes this document too, out of a function that can see none of the work's locals.

    Facts this run could not claim are reported here too — see :func:`claim`. They are the
    step's own refusals, so they belong in the report of the run that made them, not only
    in a counter nobody ever sees.

    **The ``facts`` list is the one part of this document anything reads**, and the order
    the three ways out of the ceiling are tried in follows from that. The ``params`` echo
    is a diagnostic nobody reads, so it gives way first; the facts give way second, and
    only as far as they have to, because a run findable by the first of its inputs is
    worth much more than one findable by none of them; and a document that is still over
    the ceiling with both gone is a mistake worth failing loudly on.

    ``inventory_first`` inverts the one ordering this file otherwise treats as settled, and
    only the failure path asks for it. Recording an object BEFORE its upload is right for
    WORK, and for one reason: an ambiguous upload may still commit after the client has
    gone, and an object nobody named is never looked at again. That reason does not reach
    this document. Its facts are read from the staging prefix BY NAME and not from the
    marker's inventory, so a copy the receipt never mentions still delivers everything
    anybody reads it for — while a receipt that names a document the store never took
    sends salvage looking for a file that is not there. This write happens as the run is
    already ending, on credentials that may have died with it, so that is not a remote
    case. Inventorying it only once the store has taken it keeps the receipt true and
    costs nothing that was ever at risk.
    """
    params = manifest.get('params') or {}
    # Two DIFFERENT things can cost this document facts, and they are reported under two
    # different keys on purpose: ``facts_not_claimed`` is what this step declined to write
    # because the orchestrator would not have recorded it, and ``facts_omitted`` below is
    # what a document too large for its ceiling had to give up. Reading one as the other
    # would send an author looking at the wrong end of the problem.
    unclaimed = _unclaimed_facts_note()
    summary = {'attempt': manifest.get('attempt'), 'params': params}
    if unclaimed:
        summary['facts_not_claimed'] = unclaimed
    document = {
        'schema_version': 1,
        'metrics': dict(METRICS),
        'summary': summary,
        'facts': list(FACTS),
    }
    payload = _dump(document)
    if len(payload) > MAX_RESULT_BYTES:
        # The echo is worth having — a step that re-encoded a float or mangled non-ASCII
        # on the way through would be teaching that to everyone who copies this file — but
        # not at the price of publishing a document its reader is forbidden to open. So
        # the echo is what gives way, and it says so rather than going quiet.
        #
        # Only when there is an echo to blame, though. This branch runs on SIZE, and the
        # params are not always what made the document big — a job with tens of thousands
        # of inputs gets here on the facts list alone, with params empty. Naming the wrong
        # cause sends whoever reads the line to look at a pipeline's configuration for a
        # problem that is not in it; the line below, which knows whether facts had to be
        # given up, is the one that names the real one.
        if params:
            log.warning('params are too large to echo into %s (%d bytes); '
                        'recording their shape instead', RESULT_FILENAME, len(payload))
        else:
            log.warning('%s is %d bytes, over its 1 MiB ceiling, with no params to drop',
                        RESULT_FILENAME, len(payload))
        document['summary'] = {
            'attempt': manifest.get('attempt'),
            'params': None,
            'params_omitted': 'too large for the 1 MiB ceiling on this document',
            'params_keys': sorted(params)[:64] if isinstance(params, dict) else None,
            'params_count': len(params) if isinstance(params, dict) else None,
        }
        # Re-stated, because this branch REPLACES the summary rather than editing it: a
        # note that quietly disappeared when the params happened to be large would be
        # worse than no note, since its absence reads as "everything was claimed".
        if unclaimed:
            document['summary']['facts_not_claimed'] = unclaimed
        payload = _dump(document)
    if len(payload) > MAX_RESULT_BYTES:
        # Halved until it fits, and it says how many it dropped in the same place the
        # dropped params say so. Losing the tail of the list costs the run the facts it
        # names; losing the document costs it every fact it has, and the whole point of
        # them is that somebody can find this run afterwards.
        kept = list(FACTS)
        while kept and len(payload) > MAX_RESULT_BYTES:
            kept = kept[: len(kept) // 2]
            document['facts'] = kept
            document['summary']['facts_omitted'] = (
                f'{len(FACTS) - len(kept)} of {len(FACTS)} facts dropped: too large for the '
                f'1 MiB ceiling on this document')
            payload = _dump(document)
        # Only said when facts were actually given up. A document that is still too large
        # with an empty list — the params echo alone can do it, since its replacement keeps
        # up to 64 of the key NAMES — would otherwise be reported as "carried too many
        # facts; kept 0 of 0", which names the wrong cause and sends its reader to look at
        # a list that is not the problem. The line above already said what is.
        if len(kept) < len(FACTS):
            log.warning('%s carried too many facts for its 1 MiB ceiling; kept %d of %d',
                        RESULT_FILENAME, len(kept), len(FACTS))
    if len(payload) > MAX_RESULT_BYTES:
        # Fail loudly at the point of the mistake rather than publish a document that
        # only turns out to be unreadable later, in somebody else's process.
        raise StepError(f'{RESULT_FILENAME} would be {len(payload)} bytes, above its 1 MiB ceiling')

    path = os.path.join(scratch, 'result.json')
    with open(path, 'wb') as handle:
        handle.write(payload)
    digest = _sha256_file(path)
    global RESULT_UPLOADED
    if inventory_first:
        publish(creds, RESULT_FILENAME, path, digest, len(payload), REPORT_PORT, f'{RESULT_FILENAME!r}')
        RESULT_UPLOADED = True
    else:
        write_object(creds, RESULT_FILENAME, path, len(payload), f'{RESULT_FILENAME!r}')
        RESULT_UPLOADED = True
        record(RESULT_FILENAME, digest, len(payload), REPORT_PORT)


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

    **From here on a stop request may not abandon anything, whatever this receipt says.**
    Cutting the upload costs the run its only account of itself and saves nothing: every
    object is already written and the work is over.

    That applies to a receipt claiming SUCCESS too, and this file argued the opposite for
    one release. A stop landing inside a success receipt looks like it turns the document
    into a lie, and the two obvious repairs — abandon that upload, or follow it with a
    correction — both end in the same place: **a write that failed ambiguously may still be
    accepted**, so the correction can commit first and the thing it corrected can land on
    top of it. Two documents for one run have no defined winner. One document has no
    problem to solve.

    So the caller decides what this receipt says BEFORE it is composed, and nothing
    afterwards writes another. What that leaves is a run that finished its work and was
    interrupted while reporting it, whose receipt says ``succeeded`` inside a launch the
    orchestrator has recorded as cancelled — and that is not a lie the platform can act on:
    the outcome is decided from the orchestrator's own journal, a marker can only ever VETO
    a success and never claim one, a stopped attempt's objects are salvaged as diagnostics
    rather than published, and the sentence an operator reads quotes the ``exit_code`` and
    the ``error``, never the ``status``. The work really was done; the stop arrived late.
    """
    global _ABANDONABLE
    _ABANDONABLE = False

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
        with _within(RECEIPT_DEADLINE_S, 'the completion marker'):
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
            # The report document is written HERE too, and before the receipt — on the
            # FAILURE path. The run an operator goes looking for is usually the one that
            # went wrong, and a step that only reports its facts when everything worked
            # leaves that run findable by nothing at all: the inputs it really did copy
            # before it died are as true as any others. Best-effort and wrapped in its own
            # try, so this write may cost the run its report, never its receipt, and the
            # marker stays strictly last. BaseException, because a stop arriving as a
            # KeyboardInterrupt in the middle of this upload must still leave a marker.
            #
            # A CANCELLED run does not attempt it, and that is not a preference. A stopping
            # step cannot open a new connection at all — ``_StoppableTransport.connect``
            # refuses one while the abandon flag is set, and that flag is cleared only
            # inside ``write_marker``, because the receipt is the ONE request a cancelled
            # run still gets to make. So the write here is guaranteed to be refused, and
            # attempting it would spend the last of a grace nobody promised and then print
            # an error blaming this run for doing exactly what it was designed to do. What
            # survives a cancellation is a document written BEFORE the stop.
            #
            # Nor is it attempted when the store has already taken the document, which
            # happens on exactly one path: the stop noticed by the last check in ``process``,
            # after the report was written. A second one would be a duplicate relpath —
            # refused by ``record`` — and the refusal would print a line blaming this run for
            # something that went right. ``inventory_first=False`` is the other half of
            # keeping this receipt honest: see :func:`_write_result`.
            #
            # UPLOADED, not inventoried, and the difference is a whole failure of its own.
            # The ordinary write records the document BEFORE uploading it, so an upload that
            # failed leaves the name in the inventory with nothing behind it — and reading
            # the inventory here would decide there is nothing left to do at the one moment
            # there is, then hand the marker a receipt naming a file salvage cannot find.
            if not RESULT_UPLOADED:
                forget(RESULT_FILENAME)
            if stopped and not RESULT_UPLOADED:
                print(f'hello-node: no {RESULT_FILENAME} for this run: it was stopped before one was '
                      f'written, and a stopping step keeps its last request for the receipt',
                      file=sys.stderr, flush=True)
            elif not RESULT_UPLOADED:
                try:
                    _write_result(creds, manifest or {}, scratch, inventory_first=False)
                except BaseException as result_failure:
                    print(f'hello-node: could not write {RESULT_FILENAME} for the {status} run: '
                          f'{redact(result_failure)}', file=sys.stderr, flush=True)
            # A marker is written even here — especially here. Without one the platform
            # can report THAT the step failed and never why, and everything already
            # uploaded is stranded, because salvage publishes only what the marker names.
            try:
                write_marker(creds, manifest, status=status, exit_code=code, error=reason)
            except Exception as marker_failure:
                print(f'hello-node: could not write the {status} marker: {redact(marker_failure)}',
                      file=sys.stderr, flush=True)
            return code

        # ONE receipt, decided here and not revisited. The flag is read once, before the
        # document is composed, and whatever happens to the write afterwards this process
        # never writes a DIFFERENT receipt to the same name. Two versions of this file
        # tried to correct one receipt with another — first by abandoning the first write,
        # then by letting it finish and following it with a second — and both lose the
        # same way: a write that fails AMBIGUOUSLY may still be accepted (this file says so
        # itself, in ``publish``), so the correction can commit first and the thing it was
        # correcting can land on top of it. Sequential calls are not sequential commits.
        stopped = CANCELLED
        status = 'cancelled' if stopped else 'succeeded'
        code = EXIT_CANCELLED if stopped else EXIT_OK
        try:
            write_marker(creds, manifest, status=status, exit_code=code)
        except StepError as marker_failure:
            # This step's own refusal to produce a valid document — an oversized marker, a
            # name it will not write. Nothing was offered to the store, so there is no
            # doubt about what is there: nothing, and the run has no account of itself.
            print(f'hello-node: the work finished but no valid marker could be written: '
                  f'{redact(marker_failure)}', file=sys.stderr, flush=True)
            return EXIT_PERMANENT
        except BaseException as marker_failure:
            # Ambiguous by nature: a store can accept a body after the client that sent it
            # has gone. So the receipt may be there or may not, and this process must not
            # start telling a different story from the one it already wrote — the code was
            # decided with the document and does not change now. If the document did not
            # land, the platform refuses to publish a success it cannot inventory, which is
            # the correct outcome for a step that cannot prove what it produced.
            print(f'hello-node: the work finished but the {status} marker could not be confirmed '
                  f'(it may or may not have been stored): {redact(marker_failure)}',
                  file=sys.stderr, flush=True)
            return code
        log.info('done — %s file(s), %s bytes', metrics['files'], metrics['total_bytes'])
        return code
    finally:
        import shutil

        shutil.rmtree(scratch, ignore_errors=True)


# ---------------------------------------------------------------------- helpers


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b''):
            digest.update(chunk)
        _release_page_cache(handle)
    return digest.hexdigest()


def _dump(document: dict) -> bytes:
    return json.dumps(document, sort_keys=True, indent=2).encode('utf-8')


if __name__ == '__main__':
    sys.exit(main())
