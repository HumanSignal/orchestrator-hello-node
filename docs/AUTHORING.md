# AUTHORING: how to build one

**Background**, about this document. [PROTOCOL.md](PROTOCOL.md) says what is true; this one
says what to do about it, in what order, and it ends with a checklist.

Labels are the same three, and this paragraph is **background** about them: **RULE** (the
platform refuses or fails the run), **BEHAVIOUR** (what the platform does),
**RECOMMENDATION** (what a good node does; the platform permits otherwise).

---

## The recipe

**RECOMMENDATION.** Build in this order. Each step is testable without the one after it,
which is what keeps a mistake in step 3 from being discovered in step 5.

### 1. Decide what your node consumes and produces

**BEHAVIOUR.** Your node receives a list of input objects on a single port named `input`,
and returns objects grouped into output ports you name yourself. Those port names become
the artifact kinds downstream steps ask for
([PROTOCOL.md](PROTOCOL.md#41-where-your-output-goes)).

**RECOMMENDATION.** Decide them now rather than later: renaming a port after a pipeline is
wired means editing every downstream step that selects on it.

**RECOMMENDATION.** Use one port for the real output, and a separate port such as `report`
for diagnostics. Everything under a port is offered to every downstream step wired to it.

### 2. Write the program against a hand-written envelope, with no orchestrator involved

**RECOMMENDATION.** Write a `creds.json` by hand in the `local` shape and run your program
against it. See
[CONFORMANCE.md](CONFORMANCE.md#level-1-run-it-with-a-hand-written-envelope) for the exact
file, and run the second of the two commands there — the one that sets only
`LSPO_CREDENTIALS_FILE` — because that is the one that tests anything. You can get the
whole read, work, write, marker cycle correct here, offline, before anything else exists.

### 3. Build the image

**RULE.** The orchestrator only accepts an image pinned by digest, in one of two spellings
(`external/contract.py:131-137`):

* `registry/name@sha256:<64 lowercase hex>`, a repo digest, which any machine can pull;
* `sha256:<64 lowercase hex>`, a bare image id, which only resolves on a machine that
  already holds the image.

A tag such as `:latest` or `:v3` is refused, because the same tag can mean different bytes
on two different days.

**BEHAVIOUR.** A bare image id is accepted deliberately, so that the first run of a
freshly built node is possible before any registry exists. Registration prints a warning
about it and refuses nothing (`noderegistry/services.py:458-476`). Nothing re-checks it
later, so a bare id on a pool with a second machine simply fails on whichever host does
not hold the image, with a missing-image error.

**RECOMMENDATION.** Use a bare image id only while your pool is one machine. Push and
register a repo digest before a second runner joins.

**RECOMMENDATION.** Use the exec form of `ENTRYPOINT`, `ENTRYPOINT ["python",
"/app/node.py"]`, so your process really is the container's PID 1 and receives signals
directly rather than through a shell that will not forward them.

**RECOMMENDATION.** Run as a non-root user — **any** non-root user. No uid has to match
the agent's: your credentials file is mode `0444` in a `0711` directory, so any user can
open it. This document used to name a number here, and that instruction is withdrawn; see
[PROTOCOL.md](PROTOCOL.md#13-where-the-credentials-live-and-who-may-read-them) for what
changed and why "run as root" was, and still is, the wrong repair.

**RECOMMENDATION.** Set `PYTHONUNBUFFERED=1`, or your language's equivalent. Without it a
buffered stdout means your logs arrive only when the process ends, which is exactly when
you no longer need them.

### 4. Register it and run it once

See [OPERATIONS.md](OPERATIONS.md#registering-a-node).

### 5. Handle the hard parts

**RECOMMENDATION.** Credential expiry, stopping cleanly, and inventory-on-failure. They
are the three things a first version always omits and the three things that decide
whether a real run survives. Not one of them is checked by anything. The skeleton below
has all three. Be clear-eyed about what the second one buys today: **nothing your node
writes on the way out of an externally imposed stop is reliably preserved.** An
operator's Cancel delivers nothing and the runtime deadline delivers nothing; a
**fence** delivers only what you had already written *and* inventoried at the instant of
the kill, which under the write-the-marker-last discipline is almost always nothing —
almost always rather than always, because writing the marker last fixes where in your
program it happens and not that it disappears at the same instant your process does
([PROTOCOL.md](PROTOCOL.md#71-fencing-the-stop-with-no-grace-period-at-all)). Handle the
stop for a clean exit and for the day those gaps are fixed, and put your recovery hopes
on the third item, the inventory you write when your own code decides the run is over.

**BEHAVIOUR, and read it once before you write any of the three.** **No interval on any
stop path is guaranteed** — not the time before your process is signalled, not the time
between that signal and the kill behind it, not how long anything you start afterwards has
to finish in. The reasoning is set out in full at the top of section 7 of
[PROTOCOL.md](PROTOCOL.md#7-cancellation). The consequence for the code below is concrete:
nothing important is scheduled for after the SIGTERM, every network call is given a timeout
short enough that a stop lands between calls rather than inside one, and the design never
assumes there is time to do one more thing.

This paragraph is **background**, about these documents rather than about the platform, and
it matters here because the numbers in them are of three different kinds and are not
interchangeable. A figure for **how long a stop takes** is a typical value, never a limit —
that is the statement above. A figure that is a **setting** says how often something is
attempted, and never how long it takes. Everything else — the values the platform **stamps
or enforces** — is exact and is meant to be reasoned with: the runtime budget, the moment
your upload credentials stop working, the lease stamped when your job is claimed, the 1 GiB
ceiling on a single object, the 8 MiB ceiling on a document. Reading one of those as merely
typical is how a node ends up with a refused upload and a marker the reader will not accept.

### 6. Make the run findable

**RECOMMENDATION, and it costs about four lines.** Write the ids of the things your step
touched into `result.json` as **search facts**, so that six weeks later somebody can find
this run by typing one of them. Every entry in the document's optional `facts` list is
recorded against the execution and becomes a search term in the Runs panel — `task:41`,
`input:rows.csv`, `design:AF7…` — and without them a finished external run is findable only
by an execution number that whoever is asking does not have. The shape, the rules each
entry is held to, and the fact that a bad entry costs itself and nothing else are all in
[PROTOCOL.md](PROTOCOL.md#44-resultjson).

```json
"facts": [{"key": "task", "value": "41"}, {"key": "input", "value": "rows.csv"}]
```

**RECOMMENDATION.** Record **identities, not descriptions**. The value of a fact is that
somebody types it from memory or pastes it from a ticket: an id, a filename, a batch name.
Use `task` and `annotation` for Label Studio ids, because those are the keys the platform's
own steps write and a search for one finds your run beside theirs; invent your own key for
anything else.

**RECOMMENDATION.** Write the document on your **failure** path too, carrying the facts
you had collected by then, and write it **before** the completion marker. The run somebody
comes looking for is usually the one that went wrong, and the facts it collected before it
died are as true as any others. This is the same reasoning as the marker-last rule, and it
costs the same one `try` block: a step that only reports after everything worked answers
the question for every run except the interesting one.

**RECOMMENDATION — and NOT on the cancellation path.** A step that is stopping has, by its
own design, given up its remaining requests: the whole point of noticing a stop is to
abandon the transfers in flight and start no more, and the completion marker is the one
request it still makes. So there is no connection left to write a report over, and a step
that tries anyway spends what little of the grace it has and then prints a failure of its
own making on a run that was stopped deliberately. **BEHAVIOUR.** What survives a
cancellation is whatever was written **before** the stop: the orchestrator reads a document
found beside a `cancelled` marker exactly as it reads one beside a successful marker, so a
step that reports as it goes keeps its facts and one that would only have written them on
the way out keeps none. `node.py` says so in one plain line and writes nothing.

---

## The skeleton

This paragraph is **background** about the listing that follows. It is the **correct
shape**, in Python for concreteness, cut down to the decisions that are easy to get wrong;
it is not the file in this repository, which implements all of it plus the HTTP work and
the error handling a listing this size has to leave out (see
[`node.py` and this skeleton](#nodepy-and-this-skeleton) below). None of it is
Python-specific, and none of it imports anything from the orchestrator. The labels inside
the code comments carry their usual meaning.

```python
#!/usr/bin/env python3
"""The shape of a correct external node."""

import contextlib
import hashlib
import json
import os
import signal
import socket
import sys
import tempfile
import time

MARKER_FILENAME = '__lspo_complete.json'

EXIT_OK, EXIT_TRANSIENT, EXIT_PERMANENT, EXIT_CANCELLED = 0, 1, 10, 20


class Permanent(Exception):
    """A failure no retry can fix: bad input, bad configuration."""


# --- cancellation: a flag, never work inside the handler --------------------
# RECOMMENDATION. PID 1 receives SIGTERM only because we install this. The
# handler sets a flag; ordinary control flow decides what to do about it, which
# is what keeps the partial inventory and lets the marker still be written.
#
# RECOMMENDATION, and this is the half that gets left out — including by the
# first version of this repository's own node.py, which is where the two facts
# below were measured rather than reasoned about. A flag cannot be read by a
# process parked in a socket call, so the handler must also make that call
# return, or your stop latency is your network timeout and nothing else.
#
#   * Closing the RESPONSE object does not do it. Mid-read it raises
#     "reentrant call inside <_io.BufferedReader>" INSIDE the handler, where you
#     will never see it, and the read then waits out its whole timeout anyway.
#   * A response does not exist yet while the store is still deciding whether to
#     answer, so a ledger of responses is empty during the wait that matters.
#
# Shutting the socket down does do it, and it works from the moment the
# connection is made. Nothing here starts anything or waits for anything, which
# is the line this rule is really drawing.
#
# BEHAVIOUR, and it decides one of your timeouts. There is one wait on this path
# that no signal can shorten: DNS, the TCP connect and the TLS handshake happen
# inside a single call that hands out no socket anybody else can reach — `ssl`
# detaches the plain socket while wrapping it, so shutting THAT down raises
# "Bad file descriptor" and the handshake runs to its timeout regardless
# (measured). What cannot be interrupted has to be bounded: give getting a
# connection its own short budget, separate from the timeout you allow a
# transfer, and re-check the flag the moment the call returns so a stop that
# arrived during it does not go on to start a request nobody wants.
CANCELLED = False
# The transport of the request in flight, put here by whatever opens the
# connection — in node.py, a small HTTPConnection subclass that adds itself in
# connect(). It does NOT remove itself in close(): http.client closes the
# connection as soon as it has parsed the headers of a `Connection: close`
# response, which urllib sets on every request, so a ledger that forgets a
# connection there is empty for the whole of the body.
IN_FLIGHT = []


# RECOMMENDATION. One transfer is exempt: the receipt. Cutting that upload
# saves nothing — the work is over and everything else is written — and costs
# the run its only account of itself. It gets a deadline instead (see main).
WRITING_THE_RECEIPT = False


def _on_sigterm(signum, frame):
    global CANCELLED
    # The flag FIRST, then the socket. If the shutdown throws, this step is
    # exactly as stopped as it would have been without it, and ordinary control
    # flow still sees the flag at its next check.
    CANCELLED = True
    if WRITING_THE_RECEIPT:
        return
    for transport in list(IN_FLIGHT):
        with contextlib.suppress(Exception):
            transport.shutdown(socket.SHUT_RDWR)


signal.signal(signal.SIGTERM, _on_sigterm)
signal.signal(signal.SIGINT, _on_sigterm)


# --- credentials: re-read when the envelope says it is stale ---------------
class Credentials:
    """The envelope, re-read when it is at or near its stated expiry.

    RECOMMENDATION. Reloading before literally every transfer is legal but
    unnecessary: a refreshed envelope does not revoke the URLs from the old one.
    What matters is never USING an expired one, and recovering from a refusal.
    """

    MARGIN_S = 60

    def __init__(self):
        # RECOMMENDATION, with no working alternative. Nothing in the platform
        # checks how a node finds its credentials — it cannot; it only sets the
        # variable. But this is the only name the agent sets, so any other one
        # leaves you with nothing to read.
        self.path = os.environ.get('LSPO_CREDENTIALS_FILE')
        if not self.path:
            raise SystemExit('LSPO_CREDENTIALS_FILE is not set')
        self._envelope = None
        self._expires_at = 0.0

    def get(self, force=False):
        if force or self._envelope is None or time.time() >= self._expires_at - self.MARGIN_S:
            with open(self.path, 'rb') as handle:
                envelope = json.loads(handle.read())
            if envelope.get('schema_version') != 1:
                raise Permanent(f'credential envelope version {envelope.get("schema_version")} is not supported')
            self._envelope = envelope
            self._expires_at = _parse_iso8601(envelope.get('expires_at'))
        return self._envelope


# --- the inventory: OUTSIDE the function that fills it ---------------------
# RECOMMENDATION, and it decides whether partial work survives a failure.
# Salvage publishes only what the MARKER inventories. Keep this where the
# failure path can still see it.
INVENTORY = []


def upload(creds, relpath, source_path, size, digest):
    """Upload one object, streaming, and record it in the inventory.

    RECOMMENDATION. Stream. A single object may legally be 1 GiB while the
    container's default memory limit is 2 GiB, so holding one in memory twice
    (as bytes and as a request body) is an OOM kill, and an OOM kill leaves no
    chance to write a marker at all.
    """
    for attempt in (1, 2):
        envelope = creds.get()
        staging = envelope['staging']
        try:
            if staging['mode'] == 'local_path':
                _copy_into(staging['path'], relpath, source_path)
            else:
                _post_streaming(staging['post'], relpath, source_path)
            break
        except _Expired:
            if attempt == 2:
                raise
            # RECOMMENDATION. Retry once, and only if the envelope really
            # changed. If it did not, fail transiently rather than looping.
            #
            # Compare CONTENTS, never object identity. Re-reading the file
            # parses a brand-new dict every time, so `is` (or JavaScript's
            # `===`) is always False and this guard would never fire: it would
            # retry on every refusal, including the ones where nothing changed.
            # `==` on the parsed document answers the question actually being
            # asked. Comparing `expires_at` alone is weaker, because the
            # platform clamps that value to the run's deadline.
            before = envelope
            if creds.get(force=True) == before:
                raise
    INVENTORY.append({'relpath': relpath, 'sha256': digest, 'size': size})


@contextlib.contextmanager
def fetched_and_verified(entry):
    """Stream one input to disk, hold it to its pin, and DELETE it afterwards.

    RECOMMENDATION, the most valuable one there is: the platform verifies what
    you WROTE, never what you READ. This check is yours to make.

    RECOMMENDATION, and the reason this is a context manager rather than a
    function returning a path. Inputs have NO size ceiling — not per object, not
    in total, not in number — and the container is given no disk quota, so a node
    that downloads its inputs and leaves them in /tmp fills up somebody else's
    machine. One input at a time, deleted when its work is done.
    """
    if not entry.get('sha256'):
        raise Permanent(f'input {entry.get("name")!r} arrived with no sha256 pin')
    digest = hashlib.sha256()
    size = 0
    handle = tempfile.NamedTemporaryFile(delete=False, dir='/tmp')  # never $HOME
    try:
        with handle:
            for chunk in _stream(entry):      # local_path or get_url
                digest.update(chunk)
                size += len(chunk)
                handle.write(chunk)
        if digest.hexdigest() != entry['sha256'] or size != entry.get('size', size):
            raise Permanent(f'input {entry.get("name")!r} is not the object this run was built from')
        yield handle.name
    finally:
        # In a `finally`, so the cancellation and failure paths clean up too.
        with contextlib.suppress(OSError):
            os.unlink(handle.name)


def write_marker(creds, manifest, status, exit_code, error=None, ports=None):
    """The terminal receipt. ALWAYS the last thing this program writes.

    RECOMMENDATION, and the strongest one there is: write this LAST. Nothing
    observes write order — collection starts after the process has exited — so
    no check will ever catch you writing it early. What it buys is that the
    marker's existence means everything it names is really there.

    RULE. Every relpath in produced_ports must appear in objects. Relpaths in
    objects are unique. Port names are non-blank and free of control characters.
    Identity must match the manifest. On a successful run a marker is required
    and its status must be "succeeded".
    """
    marker = {
        'schema_version': 1,
        'execution_id': manifest['execution_id'],
        'attempt': manifest['attempt'],
        'generation': manifest['generation'],
        'idempotency_key': manifest.get('idempotency_key'),
        'status': status,
        'exit_code': exit_code,        # RECOMMENDATION: the code we really return
        'objects': INVENTORY,
        'produced_ports': ports or {},
        'error': error,
    }
    body = json.dumps(marker, sort_keys=True, indent=2).encode('utf-8')
    # RECOMMENDATION. Protected from the stop handler, and bounded by a clock of
    # its own. Those two go together: the moment nothing may abandon this
    # transfer, nothing but elapsed time can end it, and a socket timeout is not
    # elapsed time — it measures silence, so a peer sending one byte per window
    # holds you open for as long as it likes. Size the deadline under whatever
    # grace the platform gives a stopped container.
    global WRITING_THE_RECEIPT
    WRITING_THE_RECEIPT = True
    with _elapsed_deadline(RECEIPT_DEADLINE_S):        # shuts IN_FLIGHT down when it fires
        _write_raw(creds.get(force=True), MARKER_FILENAME, body)   # fresh credentials


def main():
    # RECOMMENDATION. Bootstrap INSIDE the guard. With no credentials there is
    # no staging area and no marker is possible, so the only honest outcome is a
    # short redacted line on stderr and a transient exit, never a traceback.
    try:
        creds = Credentials()
        manifest = _read_manifest(creds.get(), limit=8 * 1024 * 1024)
    except Exception as exc:
        print(f'node: could not start: {_redact(exc)}', file=sys.stderr, flush=True)
        return EXIT_TRANSIENT

    try:
        ports = do_the_work(creds, manifest)     # fills INVENTORY as it goes
    except BaseException as failure:             # includes the cancellation path
        cancelled = CANCELLED or isinstance(failure, KeyboardInterrupt)
        status = 'cancelled' if cancelled else 'failed'
        code = EXIT_CANCELLED if cancelled else (
            EXIT_PERMANENT if isinstance(failure, Permanent) else EXIT_TRANSIENT
        )
        # RECOMMENDATION. The message reaches durable logs. Redact URLs: an HTTP
        # library puts the full presigned URL, signature and all, into its error.
        print(f'node: {status}: {_redact(failure)}', file=sys.stderr, flush=True)
        try:
            write_marker(creds, manifest, status, code, error=_redact(failure))
        except Exception as marker_failure:
            print(f'node: could not write the marker: {_redact(marker_failure)}', file=sys.stderr, flush=True)
        return code

    # RECOMMENDATION, and it is the one this document got wrong twice. Decide what
    # the receipt says ONCE, from the flag, before composing it — and if the write
    # fails, do not write a different receipt to the same name afterwards. A write
    # that fails ambiguously may still be accepted, so a "correction" can commit
    # first and the thing it corrected can land on top of it: two documents for one
    # run, and no defined winner. The exit code is decided with the document and is
    # not revised either, so they never differ by decision — only ever because a kill
    # landed between the document and this process's own exit, which nothing can prevent.
    stopped = CANCELLED
    status, code = ('cancelled', EXIT_CANCELLED) if stopped else ('succeeded', EXIT_OK)
    try:
        write_marker(creds, manifest, status, code, ports=ports)
    except Permanent as exc:          # nothing was offered to the store
        print(f'node: no valid marker could be written: {_redact(exc)}', file=sys.stderr, flush=True)
        return EXIT_PERMANENT
    except Exception as exc:          # it may or may not be there; say so, write nothing else
        print(f'node: the {status} marker could not be confirmed: {_redact(exc)}', file=sys.stderr, flush=True)
    return code


if __name__ == '__main__':
    sys.exit(main())
```

**RECOMMENDATION, for every row of the table below.** What each awkward-looking decision in
that skeleton is buying. Nothing in the platform checks any of it; each row is there because
the obvious alternative fails on a real run, later, saying something unrelated:

| Shape | Why it is like that |
|---|---|
| `INVENTORY` outside `do_the_work` | Salvage publishes only what the marker lists. An inventory local to the work function is empty in the failure path, and everything already uploaded is stranded. |
| Credentials behind an accessor | The file is replaced under you, without a signal. An accessor makes "re-read near expiry" one line instead of a decision at every call site. |
| `force=True` before the marker | The marker is written last, which on a long run is the moment the original envelope is most likely to be dead. |
| Bootstrap in its own `try` | Before credentials exist there is nowhere to write a marker. That failure has to be reported on stderr and by exit code alone. |
| Signal handler sets a flag, and abandons the transfer in flight | Doing work, and especially network work, inside a signal handler is how the cancellation path itself crashes — so nothing there decides anything, and its one further act cannot block: a socket shutdown starts nothing and waits for nothing. Without it the flag is unreadable for as long as your socket timeout, because the process is inside the call. |
| Streaming everywhere | 1 GiB permitted per object against 2 GiB of container memory. An OOM kill leaves the process no chance to write a marker. |
| Inputs fetched one at a time, and deleted | Nothing bounds the size, the total or the count of your inputs, and the container has no disk quota. Keeping them all is how a node fills the customer's disk. |
| Comparing envelopes with `==`, not `is` | Each read parses a new object, so an identity test is always "changed" and the retry guard never fires. |
| Exit code passed into the marker | So the marker and the process do not tell two different stories about one run. Not *cannot*: a SIGKILL landing after the marker commits and before your process returns leaves your `exit_code: 0` beside the 137 the runner observes, and no ordering of yours closes that. What this buys is that the two never differ because of a DECISION you made. |

---

## `node.py` and this skeleton

This whole section is **background**, in the sense
[README.md](README.md#how-to-read-this-three-kinds-of-statement) gives that word: it
describes one file that happens to sit in this repository, and imposes nothing on your
node.

`node.py` used to be a demonstration of the happy path with a documented list of defects,
and this section used to be that list. It has been rewritten and now implements
everything the skeleton above shows: it reads `LSPO_CREDENTIALS_FILE`, re-reads its
credentials, streams in both directions, keeps its inventory at module scope, handles a
stop request, writes the marker last on every path with the exit code the process really
returns, classifies transient and permanent failures apart, and redacts every URL before
it reaches a log. The whole conformance suite is green against it.

**The defects it used to have are still worth reading**, because each one is a mistake a
first version makes and the record says what each cost:
[CONFORMANCE-BASELINE.md](../CONFORMANCE-BASELINE.md) has the measurement, the citation
and the consequence for all twenty.

Three things in `node.py` are still worth pointing at rather than copying blindly:

* **It has no HTTP-library dependency, and that is deliberate.** `requests` builds a
  multipart upload body in memory, so `files={'file': ...}` holds the whole object — the
  exact collision between a legal 1 GiB object and a 2 GiB container that
  [PROTOCOL.md](PROTOCOL.md#35-memory-two-defaults-that-collide) is about. Streaming an
  upload with it needs a further dependency. The standard library does it in about sixty
  lines. If you bring your own HTTP client, check what it does with a large body before
  you trust it.
* **Its two network timeouts differ on purpose, and neither of them is its stop latency.**
  That sentence used to read the other way round here — reads were given the longer
  timeout because a stop was said to be noticed "as soon as the next block arrives", and
  uploads the shorter one because after the body has been sent "nothing can shorten that
  wait". The first half was measured and found false, which is what produced the socket
  shutdown in the skeleton above; the second half is false for the same reason. A stop is
  noticed at once on both paths now, and the timeouts bound something else entirely: a
  store that has gone quiet with nobody signalling anything. The upload's is the shorter
  of the two because an upload's ending is the ambiguous one — the store may already have
  committed the object — so waiting longer only buys a clearer answer about something that
  has already happened.

* **It inventories its report document AFTER uploading it, and everything else before.**
  Recording an object before its upload starts is the rule everywhere else in that file,
  and it is right for work, for one reason: an ambiguous upload may still commit after the
  client has gone, and an object nobody named is never looked at again — so the cost of
  naming something that never arrives is smaller than the cost of losing something that
  did. The report document written on the way out of a failure inverts both halves of that
  sum, which is why it is the exception. Its search facts are read from the staging prefix
  **by name**, not from the marker's inventory, so a copy the receipt never mentions still
  delivers everything anybody reads it for — there is nothing to lose by naming it late.
  And it is written when the run is already ending, on credentials that may have died with
  it, so an upload that simply fails is the ordinary case rather than the remote one — and
  a receipt naming a document the store never took sends salvage looking for a file that is
  not there. If you copy the pattern, copy the reasoning with it: the ordering is not a
  preference, and it is not a rule you can lift into the rest of your step either.

One thing that is **not** a defect: `node.py` claims `result.json` under a `report` port
rather than under `output`. Both are legal. The orchestrator's own test of its example
expects `result.json` among the `output` port's paths, so if you are matching that example
exactly, claim it there instead — but sending a metrics file to every downstream step is a
poor idea, and this is a **RECOMMENDATION**, never a rule.

## The checklist

**RECOMMENDATION.** Run through this before you register a revision.

**Every item is labelled, and the labels are the point of the list.** This paragraph is
**background** about that. A **RULE** is a specific check in the platform: fail it and your
job is refused or your run fails, every time. A **RECOMMENDATION** is something no check
will ever catch — which does not make it optional in practice, only invisible until a real
run goes wrong. An unlabelled checklist mixes the two, and a reader who cannot tell them
apart either treats advice as law or treats law as advice. Both are expensive.

#### Bootstrap

* [ ] **RECOMMENDATION.** Reads the credentials path from `LSPO_CREDENTIALS_FILE`, with no
      fallback that hides a missing variable. Nothing checks how you find the path; there
      is simply nothing else to read.
* [ ] **RULE, enforced by the kernel rather than by a check on your node.** OPENS that
      path. Does **not** list the directory it is in to discover the file: the agent
      mounts that directory `0711`, which grants traversal but not enumeration, so a
      listing is a permission error for every user except the agent. You were given the
      name, so nothing needs the listing
      ([PROTOCOL.md](PROTOCOL.md#13-where-the-credentials-live-and-who-may-read-them)).
* [ ] **RECOMMENDATION.** Refuses an envelope whose `schema_version` it does not
      implement, and a `scheme` or `staging.mode` it does not support.
* [ ] **RECOMMENDATION.** Ignores envelope and manifest fields it does not recognise,
      rather than rejecting the document.
* [ ] **RECOMMENDATION.** Reports a bootstrap failure as one short redacted line on stderr
      plus a transient exit code, with no traceback and no attempt to write a marker.

#### Inputs

* [ ] **RECOMMENDATION**, and the most valuable one in this document. Verifies every input
      against its pinned `sha256` and `size`, and refuses an input with no pin. The
      platform checks what you wrote and never what you read, so nothing but your own code
      can catch a changed input.
* [ ] **RECOMMENDATION.** Handles a job with **no** input port at all — an empty `inputs`
      list is legitimate, not an error.
* [ ] **RECOMMENDATION.** Handles two inputs that share a basename.
* [ ] **RECOMMENDATION.** Bounds its read of the job description, and refuses a
      `schema_version` it does not implement.
* [ ] **RECOMMENDATION.** Deletes each input when it is done with it. Nothing bounds input
      size or count and the container has no disk quota.

#### Work

* [ ] **RECOMMENDATION.** Streams rather than buffering whole objects, in both directions.
* [ ] **RECOMMENDATION.** Writes scratch files to `/tmp` or to staging, never under the
      image user's home directory. (In local demo mode the platform overrides your
      container's user, so its home directory may not be writable.)
* [ ] **RECOMMENDATION**, and treat it as non-negotiable. Prints no presigned URL, no
      token, no credential, on any path, including the text of HTTP errors. Container logs
      are shipped unredacted; nothing will warn you.
* [ ] **RECOMMENDATION.** Says the important things in few lines, knowing that only a tail
      of 1000 entries survives anywhere.

#### Outputs

* [ ] **RULE.** Output relpaths are canonical — non-empty, relative, no backslash, no
      control character, no `.` or `..` component, no empty component. The marker parser
      refuses anything else.
* [ ] **RULE.** Relpaths in the inventory are unique, and no relpath repeats within one
      output port.
* [ ] **RULE.** Every output port name is non-empty after trimming and free of control
      characters.
* [ ] **RULE.** Every upload key starts with `staging.post.key_prefix`. This one is
      enforced by the storage service itself, so the refusal is an HTTP error rather than
      a message from us.
* [ ] **RULE.** No single object exceeds 1 GiB. Enforced by the upload policy and checked
      again on the published copy.
* [ ] **RECOMMENDATION.** Output names are derived rather than echoed from input names, so
      two inputs sharing a basename cannot collide.
* [ ] **RECOMMENDATION.** Re-reads the credentials file at or near `expires_at`, and again
      before the marker.
* [ ] **RECOMMENDATION.** Retries once on an expiry refusal, only when the envelope
      actually changed (compared by contents, not by object identity), and never blindly
      on an ambiguous POST failure.
* [ ] **RECOMMENDATION.** Records what the run touched as search facts in `result.json`,
      on the failure path as well as the success one — and not on the cancellation path,
      where the connection to write one no longer exists — so the run can be found
      afterwards by an id somebody actually has (section 6 above). Nothing checks it, and a
      run nobody can find is a run nobody can answer questions about.
* [ ] **RECOMMENDATION.** Applies the orchestrator's own recording rules to each fact
      BEFORE writing it (non-blank, at most 512 characters, no control characters, at most
      10,000 entries) and says in the summary what it could not claim. An entry that breaks
      one is dropped on the far side, in a log you will never read, and the run is simply
      unfindable with nothing to explain why.

#### Marker

* [ ] **RULE.** On a run that exits 0, a marker exists and its `status` is `"succeeded"`.
* [ ] **RULE.** `execution_id`, `attempt` and `generation` are copied from this job's
      description, never from a previous attempt. Collection refuses a mismatch.
* [ ] **RULE.** Every relpath in `produced_ports` appears in `objects`.
* [ ] **RULE.** Hashes are exactly 64 lowercase hex characters, with no `sha256:` prefix,
      no uppercase and no trailing newline.
* [ ] **RULE.** Every integer is a real JSON integer, at least 1 for ids and counters.
* [ ] **RULE.** The document is at most 8 MiB.
* [ ] **RECOMMENDATION**, and the strongest in this set. Written strictly last, after
      every object it names. Nothing observes write order; what a marker written too early
      produces is a hash or size mismatch at collection, and the whole delivery is
      discarded.
* [ ] **RECOMMENDATION.** Written on the failure and cancellation paths too, with the
      inventory of whatever was already uploaded. Not required — and a failing node that
      writes nothing loses every object it had produced. (This is the MARKER. The report
      document is a different question and a different answer: failure path yes,
      cancellation path no — section 6 says why.)
* [ ] **RECOMMENDATION.** `exit_code` in the marker equals the code the process returns.

#### Ending

* [ ] **RECOMMENDATION.** A SIGTERM handler sets a flag; the work loop checks it; the
      stopped path writes a marker and exits 20. Without a handler your process, as PID 1,
      discards the signal entirely.
* [ ] **RECOMMENDATION, and it is the one that is usually missing from a handler that
      exists.** The handler also makes the network call you are inside return — shut the
      socket down; closing the response does nothing (see the skeleton). A handler that
      only sets a flag leaves your stop latency equal to your socket timeout, which on a
      stop that gives you no warning is the difference between a receipt and silence.
* [ ] **RECOMMENDATION.** Ask that question of **every** wait on the path, not the one you
      thought of first. There are four, and they are reached through different objects:
      getting a connection (DNS, TCP, TLS), waiting for the store to begin answering,
      reading the body, and waiting for an upload to be acknowledged. This repository
      fixed the second, shipped it, and had the first and third still costing the full
      timeout — the same defect twice more, in the same file, a week apart.
* [ ] **RECOMMENDATION.** Give **getting a connection** its own deadline, separate from the
      timeout you allow a transfer: it is the one wait nothing can interrupt, so its length
      IS your stop latency there. Two traps, both measured. **A timeout is not a deadline:**
      `socket.create_connection` resolves the name before there is a socket to time, then
      applies your number *separately to each address* — one name on three addresses spent
      12 seconds of a 4-second "timeout". And **`getaddrinfo` takes no timeout at all**, so
      a sick resolver hangs you for as long as `/etc/resolv.conf` says to be patient
      (measured: 40 s), inside a call owning no socket, which is also where a receipt you
      have promised not to abandon goes to die. Bounding the lookup needs a thread.
* [ ] **RECOMMENDATION.** Choose that number for a real job, not for a quick test. A
      deadline that fires on a healthy-but-slow connect kills the whole run: nothing retries
      an external step automatically, so a person has to notice
      ([PROTOCOL.md](PROTOCOL.md#6-exit-codes)). Generous costs you seconds of a stop; tight
      costs somebody a job.
* [ ] **RECOMMENDATION, and this document has now been wrong about it twice.** Write
      **one** receipt, or none. Decide what it says from the stop flag *before* you compose
      it, protect that write from your own handler, and if it fails do **not** write a
      different receipt to the same name afterwards — report the ambiguity through your exit
      code and your log and stop there. Neither repair that suggests itself works: cutting
      the upload does not revoke a body the store already has, and following it with a
      correction races it, because a write that failed ambiguously may still be accepted and
      may commit *after* the correction. Sequential calls are not sequential commits, and
      two documents for one run have no defined winner.
* [ ] **BEHAVIOUR, and it is why the paragraph above can be so relaxed.** A receipt saying
      `succeeded` beside a launch the platform recorded as cancelled is not a state the
      platform can act on. The outcome is decided from the orchestrator's own journal; a
      marker can only ever *veto* a success, never claim one; a stopped attempt's objects are
      salvaged as diagnostics with no output port, so nothing is delivered; the cascade sits
      behind a compare-and-set a cancellation wins; and the sentence an operator reads quotes
      your `exit_code` and `error`, never your `status`. A step that finished its work and
      was interrupted while *reporting* it has genuinely succeeded — the stop arrived late,
      and there is nothing to correct.
* [ ] **RECOMMENDATION.** Give that protected write a deadline of its own, in elapsed time.
      The two go together: the moment nothing may abandon a transfer, nothing but a clock can
      end it — and a socket timeout is not a clock, it measures silence, so a peer sending one
      byte per window holds you open indefinitely while never being idle.
* [ ] **BEHAVIOUR you must size that deadline against, and it is uncomfortable.** **Nothing
      tells your container how long it has after a stop.** Not the injected variables, not the
      credentials envelope (its `expires_at` is a signature's lifetime), not the job
      description (`timeout_seconds` is a *requested* budget with no start time attached), and
      not the stop object the orchestrator composes on its heartbeat — that reaches the agent
      and stops there. The interval before the kill is the agent's to choose and can be
      nothing at all. So a save deadline of your own is **best-effort by construction**: no
      positive number can be honoured against a remaining grace of zero, and your alarm may be
      killed before it can log that it fired. Choose one anyway — long enough that a receipt
      lands on a store that is working, short enough that a step which will not land one stops
      trying while there may still be time to say so — and do not write down a justification
      that depends on a grace nobody gave you. The number worth wanting is the remaining stop
      deadline itself, which is an open platform task.
* [ ] **BEHAVIOUR to know while you write that handler, because it decides what it is
      worth.** **Neither of the two stops named here preserves what you write.** An
      **operator pressing Cancel** usually does not reach your process at all — it normally
      arrives as a SIGKILL — and on the rare occasion it arrives as a SIGTERM, nothing you
      write is collected. The **runtime deadline** normally begins as a SIGTERM — normally,
      not always — but whatever interval follows it is cut short by the platform's own
      next heartbeat, your upload credentials
      expired at the deadline, and the terminal
      report that would have made a marker count is refused — so nothing is collected there
      either, and a container stopped that way leaves its run parked at "Waiting for
      runner" rather than failed, holding a quota slot until an operator cancels it
      ([PROTOCOL.md](PROTOCOL.md#7-cancellation)). The third externally imposed stop, a
      fence, is the last item in this section; it is the only one that can deliver
      anything, and only a marker you had already finished writing. Write the handler for a clean exit
      and for the day these gaps are fixed. Do not build a partial-output recovery story on
      top of any of them.
* [ ] **RECOMMENDATION.** Every network timeout is comfortably inside a handful of seconds.
      There is no grace period to size them against: no interval on any stop path is
      guaranteed, so the target is a stop landing *between* your calls rather than inside
      one ([PROTOCOL.md](PROTOCOL.md#7-cancellation)).
* [ ] **RECOMMENDATION.** Exit 1 for conditions a retry might survive, exit 10 for
      conditions no retry can fix. Nothing acts on the distinction today.
* [ ] **BEHAVIOUR to accept rather than to satisfy.** A fence (a revoked job, an
      unreachable orchestrator, a refused agent identity, and three more —
      [PROTOCOL.md](PROTOCOL.md#71-fencing-the-stop-with-no-grace-period-at-all)) kills the
      container outright with a SIGKILL, so nothing further is written and, under the
      write-the-marker-last discipline, almost always nothing is collected — the exception
      is a fence that lands after your marker was already uploaded and before your container
      was seen to go, which collects what that marker names. An ordinary agent shutdown is
      **not** a fence: it waits for your job, or leaves your container running for the next
      agent. Design so that a run losing everything it has not already had collected is
      survivable — which, until your marker lands, is all of it.

#### Image

* [ ] **RULE.** Pinned by digest — `registry/name@sha256:<64 hex>` or a bare
      `sha256:<64 hex>`. A tag is refused at registration.
* [ ] **RECOMMENDATION.** Exec-form entrypoint, unbuffered output, and a non-root user of
      your own choosing. **No particular uid is required** — the credentials file is
      `0444` in a `0711` directory, so any user can open it. An earlier version of this
      checklist demanded a uid matching the agent's; that is withdrawn
      ([PROTOCOL.md](PROTOCOL.md#13-where-the-credentials-live-and-who-may-read-them)).
