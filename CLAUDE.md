# Working in this repository

This is the template for an **external node** of the Label Studio Pipeline Orchestrator
(LSPO): a pipeline step that runs as somebody's own container, on their own machine,
built from their own code. The orchestrator never sees the source. It sees an image
digest, hands the container a job description and short-lived credentials, and collects
whatever the container declares it produced.

Two audiences read this repository, and they want opposite things. Someone building a
node wants to copy `node.py` and start editing. Someone deciding whether the platform can
be trusted wants to know which statements are enforced and which are advice. Everything
below exists to serve both without lying to either.

## What is here

| Path | What it is |
|---|---|
| `node.py` | A complete, correct example step. Copy it and edit |
| `Dockerfile` | The smallest image that is a real external step |
| `docs/` | **The documentation. Start here** |
| `conformance/` | A black-box harness: it builds this image, runs it, judges it from the outside only |
| `tests/` | The tests that harness runs |
| `CONFORMANCE-BASELINE.md` | The measured record of what the harness found |

## The three labels, and why they are load-bearing

Every normative statement in `docs/` carries one of three labels, and every test declares
one of three bases. **They are not decoration, and collection fails without them.**

* **RULE** / `basis_contract` — the platform refuses or fails the run. A `basis_contract`
  citation must name an authoritative source file *and* quote a rule out of it. Naming
  the area a rule lives in is not citing the rule.
* **BEHAVIOUR** — what the platform does, which you must plan for.
* **RECOMMENDATION** / `basis_reference_quality` — what a good node does. The platform
  permits otherwise, and no check will ever catch you.
* `basis_our_policy` — a compatibility or hygiene choice **this repository** makes, which
  nothing in the platform states.

The distinction matters more than it looks. Some of the most important things a node must
do — writing the marker last, verifying its inputs, re-reading its credentials — are
**recommendations**, because nothing in the platform checks them. They are no less
important for that. What the label tells you is what happens when you get it wrong: a
refusal you can see, or a failure somewhere else with no explanation attached.

**When you write or change a test, get its basis right.** A reference-quality expectation
promoted to `basis_contract` teaches a preference as a rule, and someone will then build
a node around it.

## Running the harness

```bash
pip install -r requirements-dev.txt
python -m pytest                     # this is CI
python -m pytest --red-for-real      # the true result, ignoring expected-red bookkeeping
python -m pytest --collect-only -q --print-labels   # what every test claims, and on whose authority
```

It needs a real Docker daemon, and it **refuses to run rather than simulate a container**.
A harness that fakes the container proves nothing. It builds the image from this
repository's own Dockerfile and never patches it — no volume over `/app/node.py`, no
entrypoint override — because if it could change what runs, none of its verdicts would
mean anything.

Expect a full run to take two to four minutes; several tests deliberately hold a request
open for twelve seconds or drive a stop through a thirty-second grace period.

## The expected-red mechanism

Tests marked `expected_red_until_fixed` are turned into **strict** expected failures. CI
is therefore green while a known gap exists, with the gap visible in the report as
`xfailed`.

**The moment somebody fixes one, that test passes unexpectedly and strict xfail turns the
run red.** That is not a bug in CI, it is the whole point: it is the prompt to move the
test into `conforms_today` and update `CONFORMANCE-BASELINE.md`. A fix is not finished
until the record of what is wrong has been corrected too.

As of the "correct reference node" change there are **no** `expected_red_until_fixed`
tests left. If you add one, add it with a basis and a citation, and say in
`CONFORMANCE-BASELINE.md` what was measured.

## Invariants for `node.py`

`node.py` is a **reference**. Someone will copy it verbatim into production. Every one of
these is something a first version leaves out and a real run then punishes; each is
covered by a test, and none of them is enforced by the platform.

1. **Read `LSPO_CREDENTIALS_FILE`.** That is the variable the agent sets, for every job,
   and its value is the path the *orchestrator* chose. Fall back to `LSPO_CREDENTIALS`
   only when the current name is absent (images in the field bake it), then to
   `/lspo/creds/creds.json`. Never bake a credentials path into the image.
2. **Verify every input** against both the pinned `sha256` and the pinned `size`, and
   refuse one with no pin. The platform verifies what you *wrote*, never what you *read*.
3. **Stream, in both directions — and release the page cache while you do.** A single
   object may legally be 1 GiB against a 2 GiB container. Holding one in memory twice is
   an out-of-memory kill, and an OOM kill leaves no chance to write a marker at all. This
   is why there is **no HTTP library dependency**: `requests` builds a multipart body in
   memory, so using it would contradict this rule in the file that exists to demonstrate
   it. Streaming is necessary and not sufficient: the container's memory limit counts the
   page cache your own reads and writes create, so a perfectly streaming step is still
   killed for moving a large object through a temporary file. `posix_fadvise(…,
   POSIX_FADV_DONTNEED)` every few megabytes is what closes that, and the difference is
   measured — 128 MiB through a 64 MiB container dies without it.
4. **Re-read the credentials.** The agent replaces the file underneath a running container,
   atomically and with no signal. A node that reads it once cannot upload anything —
   including its own marker — after about fifteen minutes.
5. **Keep the inventory at module scope.** Salvage publishes only what the marker lists,
   so an inventory local to the work function strands everything already uploaded.
   Record an object *before* its upload starts: a store can accept a body after the client
   is gone, and an object nobody named is never looked at again.
6. **Handle SIGTERM.** This process is PID 1, and Linux gives process 1 no default signal
   handling — without a handler the signal is discarded entirely. Set a flag, never do
   work in the handler, and check the flag *between units of work* so a stop changes what
   the step does next rather than only how it ends.
7. **Write the marker last**, and write one on the failure and cancellation paths too,
   with the real exit code and an inventory of whatever already landed.
8. **Classify exits honestly.** 0 succeeded, 1 a later attempt might survive, 10 no retry
   can fix, 20 stopped on request. Reporting a momentary `503` from an object store as
   permanent tells the platform never to run that work again.
9. **Never print a credential.** A presigned URL's query string *is* the credential, and
   container output is stored with the execution and searchable. Route every message that
   can reach a log through the redaction helper — an HTTP library puts the whole URL,
   signature included, into the text of its errors.
10. **Run as uid 10001.** The agent bind-mounts the credentials directory mode `0700`
    owned by its own uid; a mismatch is "permission denied" on the step's own credentials
    and nothing in the platform warns about it. Do not "fix" it by running as root.

## House style

* The documents explain **why**, not only what. A rule without its consequence gets
  followed until it is inconvenient.
* Prefer a sentence that names the failure to a sentence that names the practice.
* Do not write down an interval, an ordering or an exclusivity that no platform check
  enforces. Earlier drafts quoted figures for how long a stop takes; every review round
  falsified another one, so they were removed rather than hedged.
* Code comments in `node.py` carry the same labels as the docs. If a comment says a thing
  is a rule, a `RULE` in `docs/PROTOCOL.md` must say so too.
