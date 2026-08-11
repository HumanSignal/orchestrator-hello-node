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
| `logs.sh` | Follows the agent and the job containers it starts, live. An operator's convenience, not part of the contract |
| `docs/` | **The documentation. Start here** |
| `conformance/` | A black-box harness: it builds this image, runs it, judges it from the outside only |
| `tests/` | The tests that harness runs |
| `CONFORMANCE-BASELINE.md` | The measured record of what the harness found |
| `CONTRIBUTING.md` | The two-minute version of making this repository your own node |

## Building your own node from this template

**Background**, about this repository and the order to work in. You have somebody's task
description and a clone of this repository, and you have to finish at a node the
orchestrator can run. Every step below points at the document that owns the detail rather
than repeating it, because a rule written down twice goes stale in one of the two places
and the stale copy is the one somebody is reading.

1. **Read before you write.**
   [docs/README.md](docs/README.md#how-to-read-this-three-kinds-of-statement) first — it
   says how to read the three labels, and it routes everything else — then
   [docs/AUTHORING.md](docs/AUTHORING.md#the-recipe) end to end: the recipe, the
   [skeleton](docs/AUTHORING.md#the-skeleton), and the
   [checklist](docs/AUTHORING.md#the-checklist) you come back to before you ship. Where the
   shorter documents leave you guessing, [docs/PROTOCOL.md](docs/PROTOCOL.md) is the
   authority; the [shipped-capability matrix](docs/README.md#shipped-capability-matrix) is
   what keeps you from building against something the contract models and the orchestrator
   does not send.

2. **Implement the task by editing `node.py`.** Copy and edit it; do not rewrite it from
   memory. Every one of the [invariants](#invariants-for-nodepy) below is something a
   from-scratch version leaves out and a real run then punishes, and
   [docs/AUTHORING.md](docs/AUTHORING.md#nodepy-and-this-skeleton) says which of its
   choices are choices rather than rules. Another language is fine — nothing here imports
   anything from the orchestrator — but then `node.py` is your behavioural reference rather
   than your starting point, and the `Dockerfile`, which copies `node.py` and nothing else,
   becomes yours to change too.

3. **Verify with the harness. This is the step that makes the difference**, because after
   step 2 it is judging **your** container: it builds the image from this repository's own
   `Dockerfile` and never patches it. The commands are in
   [Running the harness](#running-the-harness) — run them from the repository root, as
   written. The bar, honestly — and **green is not it**, which is the whole of the third
   bullet and the only part of this step worth memorising:

   * `python -m pytest` is how you run it. A green result means your node also behaves
     like the reference one, which is more than the contract asks of it; a red one is the
     third bullet's business, not a verdict. Exactly one skip is expected and is not about
     your node: the verbatim-citation check, which needs a checkout of the orchestrator.
   * `--red-for-real` differs from the plain run only when some test carries
     `expected_red_until_fixed`, and none does today — see
     [the expected-red mechanism](#the-expected-red-mechanism). Run both anyway: the day
     they disagree, a known defect is being absorbed and you want to know whose.
   * **A red test is a question, not an instruction.** Only tests in the `conforms_today`
     group have `node.py` as their subject today, so nothing you write moves a
     `subject_is_platform` or `harness_self_test` result; and most of that group rests on
     `basis_reference_quality` or `basis_our_policy`, which by
     [the definitions below](#the-three-labels-and-why-they-are-load-bearing) are things
     the contract permits a node to do differently. Several assert that this node
     **copies** its inputs to its outputs — a node that transforms them fails those
     honestly, as `tests/test_inputs.py` says in its own opening docstring. Get the split
     for your run from `--print-labels`, read the check, and change your node only if what
     you broke is the contract
     ([docs/CONFORMANCE.md](docs/CONFORMANCE.md#if-a-conformance-directory-exists-in-this-repository)).
   * [CONFORMANCE-BASELINE.md](CONFORMANCE-BASELINE.md) measures the **reference** node,
     before and after its repair. It is the most useful thing here to read before writing
     your own, and it describes nothing about yours: its counts predict nothing about your
     run, and it is not a document to rewrite in your node's image.

4. **Build the image and take its digest.** `docker build` first, then whichever of the two
   commands in [docs/OPERATIONS.md](docs/OPERATIONS.md#the-image-reference) matches how the
   image will travel — the repo digest of a pushed image, or the bare id of one built
   locally, which that section explains only resolves on a machine that already holds it.
   Use them as they are written rather than a variant of your own.

5. **Write the README the human gets.** What the node does; the input and output
   assumptions you made, port names first, since
   [docs/AUTHORING.md](docs/AUTHORING.md#1-decide-what-your-node-consumes-and-produces)
   explains what those names decide downstream; the two commands from step 4; and the
   connect steps from step 6. [CONTRIBUTING.md](CONTRIBUTING.md) is a fair model for length.

6. **Hand back the digest, and where to paste it.** Point the human at the registration
   steps instead of summarising them, and hand over the start line the registration reply
   prints instead of one you wrote yourself. **BEHAVIOUR.** A node is registered from
   Settings, the **External Nodes** tab, **Connect node**, where that digest is pasted; the
   reply creates the pool, the deployment and the revision, and prints the agent's
   `docker run` line already filled in with the orchestrator's address, the pool name and —
   only when that registration minted one — the pool token
   ([docs/OPERATIONS.md](docs/OPERATIONS.md#registering-a-node), and
   [starting the agent](docs/OPERATIONS.md#starting-the-agent) for the general form). That
   line is run on the machine that will run your container. Registering is not running:
   nothing executes until a pipeline node points at the deployment
   ([docs/OPERATIONS.md](docs/OPERATIONS.md#pointing-a-pipeline-node-at-your-deployment)).

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
6. **Handle SIGTERM, and notice it without waiting for the network.** This process is
   PID 1, and Linux gives process 1 no default signal handling — without a handler the
   signal is discarded entirely. Set a flag, never do work in the handler, and check the
   flag *between units of work* so a stop changes what the step does next rather than only
   how it ends. The flag is not enough on its own: a process parked in a socket call
   cannot read it, so the handler also shuts down the transport in flight. Two measured
   facts decide the shape of that — closing the *response* does nothing (mid-read it
   raises `reentrant call inside <_io.BufferedReader>` inside the handler, where it is
   swallowed, and the read waits out its whole timeout), and a response does not exist at
   all while the store is still deciding whether to answer. Registering the *connection*
   and calling `shutdown` on its socket is what works, and it took this file from 12.2
   seconds to 0.2. Then ask it of every OTHER wait on the path, because that fix shipped
   with two of them still open: the connection must go on the ledger before the call that
   blocks, and it must NOT come off when `http.client` closes it after the headers of a
   `Connection: close` response, or the ledger is empty for the whole body. Getting a
   connection (DNS, TCP, TLS) cannot be interrupted at all — `ssl` detaches the socket
   while wrapping it — so bound it with its own ELAPSED deadline and re-check the flag when
   it returns. A timeout is not a deadline: `create_connection` spends yours once per
   address, `getaddrinfo` ignores it entirely (so the lookup needs a thread), and a proxy's
   CONNECT spends it a second time unless you recompute after the tunnel. Size it for a real
   job — nothing retries an external step automatically.
7b. **One receipt, or none.** Decide what it says from the flag BEFORE composing it, protect
   that write from your own handler, give it an elapsed deadline (a socket timeout measures
   silence, not duration), and never write a second, correcting document to the same name: a
   write that failed ambiguously may still be accepted and may commit after its own
   correction. This repository shipped both repairs — abandonment, then correction — and
   both lost the same way. Give that write an ELAPSED deadline (a socket timeout measures
   silence), and size it knowing it is best-effort: **nothing tells the container how long
   it has after a stop** — not the injected variables, not the credentials envelope, not
   the job description, and the orchestrator's stop object stops at the agent — so no
   positive number survives a remaining grace of zero. What makes that safe is measured on the platform side: the
   orchestrator decides an outcome from its own journal, a marker can only veto a success,
   a stopped attempt's objects are salvaged as diagnostics rather than published, and the
   operator's sentence quotes `exit_code` and `error` but never `status`.
7. **Write the marker last**, and write one on the failure and cancellation paths too,
   with the real exit code and an inventory of whatever already landed.
8. **Classify exits honestly.** 0 succeeded, 1 a later attempt might survive, 10 no retry
   can fix, 20 stopped on request. Reporting a momentary `503` from an object store as
   permanent tells the platform never to run that work again.
9. **Never print a credential.** A presigned URL's query string *is* the credential, and
   container output is stored with the execution and searchable. Route every message that
   can reach a log through the redaction helper — an HTTP library puts the whole URL,
   signature included, into the text of its errors.
10. **Run as a non-root user — any of them.** No uid has to match the agent's. The
    credentials directory is mounted `0711` with the file inside it `0444`, so any user
    can open it; confidentiality comes from an ancestor directory nobody else can
    traverse. What the modes *do* still require: open the exact path in
    `LSPO_CREDENTIALS_FILE`, never list its directory — `0711` grants traversal, not
    enumeration. This rule used to say "run as uid 10001"; that was true of an older
    platform and telling an author to build for it is now the harmful answer.

## House style

* The documents explain **why**, not only what. A rule without its consequence gets
  followed until it is inconvenient.
* Prefer a sentence that names the failure to a sentence that names the practice.
* Do not write down an interval, an ordering or an exclusivity that no platform check
  enforces. Earlier drafts quoted figures for how long a stop takes; every review round
  falsified another one, so they were removed rather than hedged.
* Code comments in `node.py` carry the same labels as the docs. If a comment says a thing
  is a rule, a `RULE` in `docs/PROTOCOL.md` must say so too.
