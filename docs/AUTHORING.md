# AUTHORING: how to build one

[PROTOCOL.md](PROTOCOL.md) says what is true. This document says what to do about it, in
what order, and it ends with a checklist.

Labels are the same three: **RULE** (the platform refuses or fails the run), **BEHAVIOUR**
(what the platform does), **RECOMMENDATION** (what a good node does; the platform permits
otherwise).

---

## The recipe

Build in this order. Each step is testable without the one after it.

### 1. Decide what your node consumes and produces

Your node receives a list of input objects on a single port named `input`, and returns
objects grouped into output ports you name yourself. Decide those port names now; they
become the artifact kinds downstream steps ask for.

**RECOMMENDATION.** Use one port for the real output, and a separate port such as `report`
for diagnostics. Everything under a port is offered to every downstream step wired to it.

### 2. Write the program against a hand-written envelope, with no orchestrator involved

Write a `creds.json` by hand in the `local` shape and run your program against it. See
[CONFORMANCE.md](CONFORMANCE.md#level-1-run-it-with-a-hand-written-envelope) for the exact
file. You can get the whole read, work, write, marker cycle correct here, offline, before
anything else exists.

### 3. Build the image

**RULE.** The orchestrator only accepts an image pinned by digest, in one of two spellings
(`external/contract.py:130-136`):

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

Credential expiry, cancellation, and inventory-on-failure. They are the three things a
first version always omits and the three things that decide whether a real run survives.
The skeleton below has all three.

---

## The skeleton

This is the **correct shape**, in Python for concreteness. It is not the file in this
repository: see [Known gaps in `node.py`](#known-gaps-in-nodepy) below. Nothing here is
required to be Python, and nothing here imports anything from the orchestrator.

```python
#!/usr/bin/env python3
"""The shape of a correct external node."""

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
        self.path = os.environ.get('LSPO_CREDENTIALS_FILE')  # RULE: this name
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
    (as bytes and as a request body) is an OOM kill, and an OOM kill writes no
    marker at all.
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
            before = envelope
            if creds.get(force=True) is before:
                raise
    INVENTORY.append({'relpath': relpath, 'sha256': digest, 'size': size})


def fetch_and_verify(entry):
    """Stream one input to disk, hashing as we go, and hold it to its pin.

    RECOMMENDATION, the most valuable one there is: the platform verifies what
    you WROTE, never what you READ. This check is yours to make.
    """
    if not entry.get('sha256'):
        raise Permanent(f'input {entry.get("name")!r} arrived with no sha256 pin')
    digest = hashlib.sha256()
    size = 0
    handle = tempfile.NamedTemporaryFile(delete=False, dir='/tmp')  # never $HOME
    with handle:
        for chunk in _stream(entry):          # local_path or get_url
            digest.update(chunk)
            size += len(chunk)
            handle.write(chunk)
    if digest.hexdigest() != entry['sha256'] or size != entry.get('size', size):
        raise Permanent(f'input {entry.get("name")!r} is not the object this run was built from')
    return handle.name


def write_marker(creds, manifest, status, exit_code, error=None, ports=None):
    """The terminal receipt. ALWAYS the last thing this program writes.

    RULE. Every relpath in produced_ports must appear in objects. Relpaths in
    objects are unique. Identity must match the manifest. On a successful run a
    marker is required and its status must be "succeeded".
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

What each awkward-looking decision is buying:

| Shape | Why it is like that |
|---|---|
| `INVENTORY` outside `do_the_work` | Salvage publishes only what the marker lists. An inventory local to the work function is empty in the failure path, and everything already uploaded is stranded. |
| Credentials behind an accessor | The file is replaced under you, without a signal. An accessor makes "re-read near expiry" one line instead of a decision at every call site. |
| `force=True` before the marker | The marker is written last, which on a long run is the moment the original envelope is most likely to be dead. |
| Bootstrap in its own `try` | Before credentials exist there is nowhere to write a marker. That failure has to be reported on stderr and by exit code alone. |
| Signal handler sets a flag only | Doing work, and especially network work, inside a signal handler is how the cancellation path itself crashes. |
| Streaming everywhere | 1 GiB permitted per object against 2 GiB of container memory. An OOM kill writes no marker. |
| Exit code passed into the marker | So the marker and the process cannot tell two different stories about one run. |

---

## Known gaps in `node.py`

The `node.py` in this repository is a demonstration of the happy path. It is being repaired
separately. Until then, do not copy these parts of it. Line numbers are for this
repository's copy.

| Where | What it does | Why it is wrong |
|---|---|---|
| `node.py:174` | reads `LSPO_CREDENTIALS` | The agent sets `LSPO_CREDENTIALS_FILE`. This works only because `Dockerfile:18` hardcodes the other name. Copy the file without that line and the node dies immediately with a message that names a variable the platform has never heard of. |
| `node.py:260-261` | reads the envelope once, at the start | After roughly fifteen minutes its upload policy is expired, so it can upload neither its outputs nor its marker. |
| `node.py:260-261` | bootstrap runs outside the `try` | A failure there escapes `main`, prints a traceback and reports nothing. |
| `node.py:99`, `:142`, `:156` | holds whole objects in memory, twice | Collides with the 1 GiB per-object allowance against a 2 GiB memory limit. |
| `node.py:192`, `:271` | the inventory is local to `process()`; the failure path writes `objects: []` | Everything already uploaded is unrecoverable, because salvage publishes only what the marker inventories. |
| whole file | no signal handling | A cancelled run is ignored: it finishes the batch and writes a `succeeded` marker after a human pressed stop, or is killed after 30 seconds. |
| `node.py:236` vs `:274` | marker claims exit code 10 on every failure; the process returns 1 for anything that is not its own `StepError` | Two contradictory accounts of the same run. |
| `node.py:140-141`, `:269` | an HTTP error's text, presigned URL included, reaches stderr and the marker | Container log lines are shipped unredacted. |
| `node.py:161-169` | reads the job description with no size bound and no version check | Proceeds on a malformed or future-version document. |
| `node.py:172-178` | never validates the envelope | Same class of problem, different document. |
| `node.py:198-201` | derives output names from input names | Two inputs sharing a basename produce one relpath twice, which the marker parser refuses. |
| `node.py:140`, `:156` | 120 second and 300 second timeouts | Both are longer than the 30 second cancellation grace, so cancellation during a transfer yields neither marker nor salvage. |

One thing on that list which is **not** a defect: `node.py:219` and `:238` claim
`result.json` under the `output` port. That is legal, and the orchestrator's own test of
this example expects exactly `['outputs/rows.csv', 'result.json']` there
(`tests/test_hello_node_example.py:105`). Sending a metrics file to every downstream step
is a poor idea, so use a separate port, but it is a **RECOMMENDATION** and never a rule.

---

## The checklist

Run through this before you register a revision.

**Bootstrap**

* [ ] Reads the credentials path from `LSPO_CREDENTIALS_FILE`, with no fallback that hides
      a missing variable.
* [ ] Refuses an envelope whose `schema_version` it does not implement, and a `scheme` or
      `staging.mode` it does not support.
* [ ] Ignores envelope and manifest fields it does not recognise, rather than rejecting
      the document.
* [ ] Reports a bootstrap failure as one short redacted line on stderr plus a transient
      exit code, with no traceback and no attempt to write a marker.

**Inputs**

* [ ] Verifies every input against its pinned `sha256` and `size`, and refuses an input
      with no pin.
* [ ] Handles a job with **no** input port at all.
* [ ] Handles two inputs that share a basename.
* [ ] Bounds its read of the job description, and refuses a `schema_version` it does not
      implement.

**Work**

* [ ] Streams rather than buffering whole objects, in both directions.
* [ ] Writes scratch files to `/tmp` or to staging, never under the image user's home
      directory.
* [ ] Prints no presigned URL, no token, no credential, on any path, including the text of
      HTTP errors.
* [ ] Says the important things in few lines, knowing the tail is what survives.

**Outputs**

* [ ] Output relpaths are canonical, unique, and derived rather than echoed from input
      names.
* [ ] Every upload key starts with `staging.post.key_prefix`.
* [ ] Re-reads the credentials file at or near `expires_at`, and again before the marker.
* [ ] Retries once on an expiry refusal, only when the envelope actually changed, and
      never blindly on an ambiguous POST failure.

**Marker**

* [ ] Written strictly last, after every object it names.
* [ ] Identity copied from this job's description, never from a previous attempt.
* [ ] Every relpath in `produced_ports` appears in `objects`; no relpath repeats within a
      port; relpaths in `objects` are unique.
* [ ] Hashes are 64 lowercase hex characters, computed over the bytes actually written.
* [ ] Written on the failure and cancellation paths too, with the inventory of whatever
      was already uploaded.
* [ ] `exit_code` in the marker equals the code the process returns.

**Ending**

* [ ] A SIGTERM handler sets a flag; the work loop checks it; the cancelled path writes a
      marker and exits 20.
* [ ] Every network timeout is comfortably under the 30 second cancellation grace.
* [ ] Exit 1 for conditions a retry might survive, exit 10 for conditions no retry can
      fix.

**Image**

* [ ] Pinned by digest, exec-form entrypoint, unbuffered output, uid matching the agent's.
