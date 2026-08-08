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

**RECOMMENDATION.** Run as uid 10001 unless the agent's operator tells you otherwise; see
[PROTOCOL.md](PROTOCOL.md#13-where-the-credentials-live-and-who-may-read-them) for why
that number, and why "run as root" is the wrong repair.

**RECOMMENDATION.** Set `PYTHONUNBUFFERED=1`, or your language's equivalent. Without it a
buffered stdout means your logs arrive only when the process ends, which is exactly when
you no longer need them.

### 4. Register it and run it once

See [OPERATIONS.md](OPERATIONS.md#registering-a-node).

### 5. Handle the hard parts

**RECOMMENDATION.** Credential expiry, stopping cleanly, and inventory-on-failure. They are
the three things a first version always omits and the three things that decide whether a
real run survives. Not one of them is checked by anything. The skeleton below has all three.
Be clear-eyed about what the second one buys today: **nothing your node writes on the way
out of an externally imposed stop is reliably preserved.** An operator's Cancel delivers
nothing and the runtime deadline delivers nothing; a **fence** delivers only what you had
already written *and* inventoried at the instant of the kill, which under the
write-the-marker-last discipline is nothing
([PROTOCOL.md](PROTOCOL.md#7-cancellation)). Handle the stop for a clean exit and for the
day those gaps are fixed, and put your recovery hopes on the third item, the inventory you
write when your own code decides the run is over.

---

## The skeleton

This paragraph is **background** about the listing that follows. It is the **correct
shape**, in Python for concreteness, and it is not the file in this repository: see
[Known gaps in `node.py`](#known-gaps-in-nodepy) below. None of it is Python-specific, and
none of it imports anything from the orchestrator. The labels inside the code comments
carry their usual meaning.

```python
#!/usr/bin/env python3
"""The shape of a correct external node."""

import contextlib
import hashlib
import json
import os
import signal
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
CANCELLED = False


def _on_sigterm(signum, frame):
    global CANCELLED
    CANCELLED = True


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

    write_marker(creds, manifest, 'succeeded', EXIT_OK, ports=ports)
    return EXIT_OK


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
| Signal handler sets a flag only | Doing work, and especially network work, inside a signal handler is how the cancellation path itself crashes. |
| Streaming everywhere | 1 GiB permitted per object against 2 GiB of container memory. An OOM kill leaves the process no chance to write a marker. |
| Inputs fetched one at a time, and deleted | Nothing bounds the size, the total or the count of your inputs, and the container has no disk quota. Keeping them all is how a node fills the customer's disk. |
| Comparing envelopes with `==`, not `is` | Each read parses a new object, so an identity test is always "changed" and the retry guard never fires. |
| Exit code passed into the marker | So the marker and the process cannot tell two different stories about one run. |

---

## Known gaps in `node.py`

This whole section is **background**, in the sense
[README.md](README.md#how-to-read-this-three-kinds-of-statement) gives that word: it
describes one file that happens to sit in this repository, and imposes nothing on your node.
The `node.py` here is a demonstration of the happy path. It is being repaired separately.
Until then, do not copy these parts of it. Line numbers are for this repository's copy.

| Where | What it does | Why it is wrong |
|---|---|---|
| `node.py:174` | reads `LSPO_CREDENTIALS` | The agent sets `LSPO_CREDENTIALS_FILE`. This works only because `Dockerfile:18` hardcodes the other name. Copy the file without that line and the node dies immediately with a message that names a variable the platform has never heard of. This was run: with only the correct variable set, it exits 1 with `StepError: LSPO_CREDENTIALS is not set`. It is also why the offline example in [CONFORMANCE.md](CONFORMANCE.md#level-1-run-it-with-a-hand-written-envelope) has to set both names. |
| `node.py:260-261` | reads the envelope once, at the start | After roughly fifteen minutes its upload policy is expired, so it can upload neither its outputs nor its marker. Invisible on a default registration, where the run is stopped at that same fifteen-minute mark anyway; fatal the first time an operator raises the node's `timeout_seconds` ([PROTOCOL.md](PROTOCOL.md#24-how-long-you-actually-get)). |
| `node.py:260-261` | bootstrap runs outside the `try` | A failure there escapes `main`, prints a traceback and reports nothing. |
| `node.py:99`, `:142`, `:156` | holds whole objects in memory, twice | Collides with the 1 GiB per-object allowance against a 2 GiB memory limit. |
| `node.py:192`, `:271` | the inventory is local to `process()`; the failure path writes `objects: []` | Everything already uploaded is unrecoverable, because salvage publishes only what the marker inventories. |
| whole file | no signal handling | A stop request is ignored. On the **runtime deadline** the file carries on through its batch after the platform has asked it to stop, and is then killed — writing, if it gets that far, a `succeeded` marker for a run the platform had already given up on. Nothing it writes after the platform asked it to stop is collected on that path ([PROTOCOL.md](PROTOCOL.md#7-cancellation)), so what the missing handler really costs is a clean exit and the work the container goes on doing for nobody. |
| `node.py:236` vs `:274` | marker claims exit code 10 on every failure; the process returns 1 for anything that is not its own `StepError` | Two contradictory accounts of the same run. |
| `node.py:140-141`, `:269` | an HTTP error's text, presigned URL included, reaches stderr and the marker | Container log lines are shipped unredacted. |
| `node.py:161-169` | reads the job description with no size bound and no version check | Proceeds on a malformed or future-version document. |
| `node.py:172-178` | never validates the envelope | Same class of problem, different document. |
| `node.py:198-201` | derives output names from input names | Two inputs sharing a basename produce one relpath twice, which the marker parser refuses. |
| `node.py:140`, `:156` | 120 second and 300 second timeouts | Both are longer than the grace that follows a SIGTERM — at most 30 seconds, and usually much less ([PROTOCOL.md](PROTOCOL.md#7-cancellation)) — so a stop landing during a transfer never reaches the handler at all and the process is killed mid-write. |

One thing on that list which is **not** a defect: `node.py:219` and `:238` claim
`result.json` under the `output` port. That is legal, and the orchestrator's own test of
this example expects exactly `['outputs/rows.csv', 'result.json']` there
(`tests/test_hello_node_example.py:105`). Sending a metrics file to every downstream step
is a poor idea, so use a separate port, but it is a **RECOMMENDATION** and never a rule.

---

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
      writes nothing loses every object it had produced.
* [ ] **RECOMMENDATION.** `exit_code` in the marker equals the code the process returns.

#### Ending

* [ ] **RECOMMENDATION.** A SIGTERM handler sets a flag; the work loop checks it; the
      stopped path writes a marker and exits 20. Without a handler your process, as PID 1,
      discards the signal entirely.
* [ ] **BEHAVIOUR to know while you write that handler, because it decides what it is
      worth.** **Neither of the two stops named here preserves what you write.** An
      **operator pressing Cancel** usually does not reach your process at all — it normally
      arrives as a SIGKILL — and on the rare occasion it arrives as a SIGTERM, nothing you
      write is collected. The **runtime deadline** does begin as a SIGTERM, but its 30
      second grace is cut short by the platform's own next heartbeat (zero to 20 seconds,
      ten on average), your upload credentials expired at the deadline, and the terminal
      report that would have made a marker count is refused — so nothing is collected there
      either, and a container stopped that way leaves its run parked rather than failed
      ([PROTOCOL.md](PROTOCOL.md#7-cancellation)). The third externally imposed stop, a
      fence, is the last item in this section; it is the only one that can deliver
      anything, and only a marker you had already finished writing. Write the handler for a clean exit
      and for the day these gaps are fixed. Do not build a partial-output recovery story on
      top of any of them.
* [ ] **RECOMMENDATION.** Every network timeout is comfortably inside a handful of seconds,
      not merely inside the 30 seconds the grace advertises.
* [ ] **RECOMMENDATION.** Exit 1 for conditions a retry might survive, exit 10 for
      conditions no retry can fix. Nothing acts on the distinction today.
* [ ] **BEHAVIOUR to accept rather than to satisfy.** A fence (a revoked job, an
      unreachable orchestrator, a refused agent identity, and three more —
      [PROTOCOL.md](PROTOCOL.md#71-fencing-the-stop-with-no-grace-period-at-all)) kills the
      container outright with a SIGKILL, so nothing further is written and, under the
      write-the-marker-last discipline, nothing is collected. An ordinary agent shutdown is
      **not** a fence: it waits for your job, or leaves your container running for the next
      agent. Design so that a run losing its last minute of work is survivable.

#### Image

* [ ] **RULE.** Pinned by digest — `registry/name@sha256:<64 hex>` or a bare
      `sha256:<64 hex>`. A tag is refused at registration.
* [ ] **RECOMMENDATION.** Exec-form entrypoint, unbuffered output, and a uid matching the
      agent's (10001 for the shipped agent image). The uid is checked by nothing and
      fails as a permission error on your own credentials file.
