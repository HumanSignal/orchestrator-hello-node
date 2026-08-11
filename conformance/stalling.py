"""A listener that says exactly as much as it is told to, and then nothing, for ever.

This is not a store, and it deliberately speaks no HTTP of its own. It exists because the
fake store cannot express the two waits that decide whether a stop is noticed on the way
IN and on the way OUT:

* **A TLS handshake that never completes.** The store is plain HTTP, and no request hook
  can model a peer that accepts a connection and then refuses to negotiate. This one does
  it by doing nothing at all — a handshake stalls before any certificate is offered, so no
  certificate is needed to stall one, and the harness gains a TLS test without a
  certificate authority, an ``openssl`` dependency, or a line of trust configuration in
  the image.
* **A body that stops arriving half way.** ``Endpoint``'s hooks fire while a request is
  still being authorised, which is BEFORE a single byte of the response is written, so a
  delay there models "the store has not answered yet" and cannot model "the store answered
  and then went quiet". The distinction matters because they are different waits in the
  client, reached through different objects, and a node can be interruptible in one and
  not the other — which is exactly what this repository shipped.

**Every connection is announced on evidence, never on a timer**, and that is the whole
shape of :meth:`_serve`: read something first, send everything second, announce third. A
test that signalled as soon as the connection was ACCEPTED would be signalling before the
client had begun negotiating, and an implementation with an unbounded handshake could pass
it on a lucky schedule — the exact hole these tests exist to close. Reading first means the
client has really begun to speak; sending a preamble larger than any socket buffer means
``sendall`` cannot return until the client has really begun to listen.
"""

from __future__ import annotations

import socket
import threading
import time

#: Localhost, for the one connection this module makes to itself: the wake-up that ends a
#: blocked ``accept``. Closing a listening socket from another thread does NOT reliably
#: wake one on Linux.
LOOPBACK = '127.0.0.1'

#: What an HTTP proxy says when it has agreed to tunnel a connection. Sent late and
#: followed by silence, it produces the two-phase wait a proxied TLS connect really has.
TUNNEL_GRANTED = b'HTTP/1.1 200 Connection established\r\n\r\n'


def a_body_that_stops(sent: int = 16 * 1024 * 1024, promised: int = 64 * 1024 * 1024) -> bytes:
    """Response headers promising ``promised`` bytes, followed by ``sent`` of them.

    ``sent`` is deliberately far larger than any socket buffer on either side. That is not
    about volume: it is what makes the listener's own ``sendall`` unable to return until
    the CLIENT has consumed most of it, which is the difference between "the bytes reached
    a kernel" and "the client is inside the body read". Without it a test has only a sleep,
    and a sleep proves nothing about a client it cannot see.
    """
    headers = (
        f'HTTP/1.1 200 OK\r\nContent-Length: {promised}\r\nConnection: close\r\n\r\n'
    ).encode('ascii')
    return headers + b'x' * sent


class StallingEndpoint:
    """Accept connections, read, send ``preamble``, announce, then say nothing for ever."""

    def __init__(self, preamble: bytes = b'', *, answer_after: float = 0.0) -> None:
        self.preamble = preamble
        #: How long to hold the request before sending ``preamble``. A peer that answers
        #: LATE and then goes quiet is a different shape from one that never answers, and
        #: it is the shape a proxy has: the CONNECT is granted, slowly, and the TLS
        #: handshake behind it then stalls. Whether the two waits share one budget or get
        #: one each is invisible to any test where the first wait is instant.
        self.answer_after = answer_after
        #: Every connection accepted, kept so the sockets stay open. A closed socket would
        #: end the client's wait, which is the one thing this class must never do early.
        self.accepted: list[socket.socket] = []
        self._connected = threading.Event()
        self._lock = threading.Lock()
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(('0.0.0.0', 0))
        self._listener.listen(8)
        self.port = self._listener.getsockname()[1]
        self._running = False
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> 'StallingEndpoint':
        self._running = True
        self._thread.start()
        return self

    def stop(self) -> None:
        """Shut down without leaving the serve thread blocked anywhere.

        There are three places it can be waiting, and closing a descriptor from another
        thread does not reliably return any of them:

        * in ``accept``, for the next client — woken by connecting to ourselves;
        * in ``recv``, for a client that connected and said nothing;
        * in ``sendall``, for a client that spoke and then read nothing.

        The last two are why the accepted connections are shut down BEFORE the join rather
        than after it. ``shutdown`` is what returns a syscall already in progress; ``close``
        on its own can leave the thread parked and the join times out silently. Whatever is
        accepted during the wind-down is closed after the thread has gone, so nothing leaks
        either way.
        """
        self._running = False
        self._release_accepted()
        try:
            with socket.create_connection((LOOPBACK, self.port), timeout=2.0):
                pass
        except OSError:
            pass
        self._thread.join(timeout=5.0)
        self._release_accepted()
        try:
            self._listener.close()
        except OSError:
            pass

    def _release_accepted(self) -> None:
        """Unblock and close every connection taken so far, however it is being used."""
        with self._lock:
            accepted, self.accepted = list(self.accepted), []
        for connection in accepted:
            for release in (lambda: connection.shutdown(socket.SHUT_RDWR), connection.close):
                try:
                    release()
                except OSError:
                    pass

    def url(self, host: str, *, scheme: str = 'http', path: str = '/held-open') -> str:
        return f'{scheme}://{host}:{self.port}{path}'

    def wait_for_stall(self, timeout: float = 60.0) -> bool:
        """Block until a client is really waiting on this listener.

        Not "until something connected": until it has spoken (so a TLS client has sent its
        hello) and, when there is a preamble, until it has consumed nearly all of it (so an
        HTTP client is inside the body rather than the headers). That is the moment worth
        interrupting, and the only one these tests may signal on.
        """
        return self._connected.wait(timeout)

    def _serve(self) -> None:
        while self._running:
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return  # the listener was closed: teardown, not a fault
            if not self._running:
                connection.close()  # the wake-up from stop(); nothing else to do
                return
            with self._lock:
                self.accepted.append(connection)
            try:
                spoken = connection.recv(4096)  # the ClientHello, or the HTTP request line
                if not spoken:
                    continue  # it connected and closed without asking for anything
                if self.answer_after:
                    time.sleep(self.answer_after)
                if self.preamble:
                    connection.sendall(self.preamble)
            except OSError:
                continue  # it went away mid-exchange: nobody is stalled on us
            # Announced ONLY on the success of both, and the ``continue``s above are the
            # point. Announcing after an EOF or a broken pipe would be the instrument
            # lying in the direction that makes tests pass: a test would signal its
            # container believing a client was parked here, when the client had gone.
            self._connected.set()

    def __enter__(self) -> 'StallingEndpoint':
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
