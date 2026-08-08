# CONFORMANCE: testing a node, and what a test can prove

Three levels, each cheap and each proving something the one before it cannot. Then a
section on what none of them proves, which matters more than it sounds: a suite that
claims more than it establishes teaches wrong code onward.

Labels as elsewhere: **RULE**, **BEHAVIOUR**, **RECOMMENDATION**.

---

## Level 1: run it with a hand-written envelope

No orchestrator, no network, no docker. Write the two documents by hand and run your
program.

`/tmp/job/creds.json`:

```json
{
  "schema_version": 1,
  "scheme": "local",
  "credentials_file": "/tmp/job/creds.json",
  "expires_at": "2099-01-01T00:00:00+00:00",
  "manifest_path": "/tmp/job/invocation.json",
  "inputs": [
    {"port": "input", "relpath": "rows.csv", "name": "rows.csv",
     "sha256": "<the real sha256 of the file below>", "size": 12,
     "local_path": "/tmp/job/in/rows.csv"}
  ],
  "staging": {"mode": "local_path", "path": "/tmp/job/out"}
}
```

`/tmp/job/invocation.json`:

```json
{
  "schema_version": 1,
  "execution_id": 1, "pipeline_id": 1, "deployment_id": 1, "revision_id": 1,
  "image_digest": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "params": {},
  "env_names": [],
  "inputs": [
    {"name": "input", "payload_kind": "csv", "layout": "file", "cardinality": "many",
     "objects": [{"uri": "file:///tmp/job/in/rows.csv",
                  "sha256": "<same real sha256>", "size": 12, "relpath": "rows.csv"}]}
  ],
  "staging_prefix": "file:///tmp/job/out/attempts/1/gen-1",
  "credentials_file": "/tmp/job/creds.json",
  "timeout_seconds": 3600,
  "idempotency_key": "execution-1-attempt-1",
  "attempt": 1,
  "generation": 1
}
```

Then:

```bash
LSPO_CREDENTIALS_FILE=/tmp/job/creds.json \
LSPO_EXECUTION_ID=1 LSPO_ATTEMPT=1 LSPO_GENERATION=1 \
LSPO_IDEMPOTENCY_KEY=execution-1-attempt-1 LSPO_CONTRACT_VERSION=1 \
LSPO_STAGING_PREFIX=file:///tmp/job/out/attempts/1/gen-1 \
LSPO_INVOCATION_URI=file:///tmp/job/invocation.json \
python node.py
```

**RULE, worth knowing before you hand-write a manifest.** `staging_prefix` must end with
`attempts/<attempt>/gen-<generation>` matching the `attempt` and `generation` fields, or
the document is invalid. A single trailing slash is tolerated, since the check strips
trailing slashes before comparing. This is the first thing that rejects a hand-made
manifest, and it is not arbitrary: that suffix is the fence keeping a superseded runner
out of the live attempt's area (`external/contract.py:440-457`).

Everything above was run through the platform's own parser while these documents were
written: the manifest and the marker in this document both validate, and each refusal
claimed in [PROTOCOL.md](PROTOCOL.md#5-the-completion-marker) was exercised against it.

**What level 1 proves.** Your program reads its credentials from the right variable,
parses both documents, verifies input pins, writes objects, and writes a marker with a
consistent inventory. This is most of the contract.

**What it does not prove.** Anything to do with expiry, cancellation, object-store
refusals, or the collection side.

### Validating your own marker

You do not have the orchestrator's parser, so write a twenty-line checker and run it over
your marker in the test. It should refuse:

* a `sha256` that is not exactly 64 lowercase hexadecimal characters (no `sha256:` prefix,
  no uppercase, no trailing newline);
* an integer that is a string, a boolean or a float; any id or counter below 1;
* a relpath that is empty, absolute, contains a backslash, a line break, a `.` or `..`
  component, or an empty component such as `a//b` or a trailing slash;
* a duplicate relpath in `objects`;
* a relpath in `produced_ports` that is absent from `objects`;
* a relpath repeated within one port (across two different ports it is legal);
* a `status` outside `succeeded`, `failed`, `cancelled`;
* a document over 8 MiB.

That list is the complete set of marker rules from
[PROTOCOL.md](PROTOCOL.md#5-the-completion-marker). If your checker passes and collection
still refuses your marker, the disagreement is worth reporting.

---

## Level 2: a fake object-store endpoint

Run your node against a small HTTP server that stands in for the storage service: it
serves the inputs and the job description on presigned-looking GET URLs, and accepts form
POSTs to a fake upload endpoint.

**RECOMMENDATION.** Have the fake refuse a POST whose `key` does not start with the
declared `key_prefix`, and one whose body exceeds the per-object ceiling. Those are the
two conditions the real policy carries, and a node that gets them wrong fails in
production and passes against a permissive fake.

### Testing credential rotation without a clock

**RECOMMENDATION.** Model **generations**, not wall-clock time. A time-based test is
either slow or flaky.

1. Start with envelope A.
2. Let the node use A for its last input.
3. On that request, the fake atomically replaces `creds.json` with envelope B and starts
   refusing A's signatures.
4. The node continues processing.
5. A node that cached A fails deterministically at its first upload.
6. A node that re-reads picks up B and succeeds.

Then rotate B to C after the first output upload. That separates "reloads once, after
processing" from "reloads whenever the envelope says it is stale", which is the property
that actually matters at the end of a long run.

**A property of the test harness, not of your node.** Mount the **directory**, not the
file, if you run this inside docker. A bind-mounted file keeps pointing at the replaced
inode and will never appear to change, so a harness that mounts the file will report every
node as broken. That is exactly why the platform mounts the directory
(`agent/creds.py:10-16`).

### Testing cancellation

**RECOMMENDATION.** Send SIGTERM **during** a transfer, not only between work units. A
cooperative flag cannot be checked while the process is blocked in a socket read, and that
is the case a real cancellation hits. Assert three things: a `cancelled` marker exists, it
inventories the objects that were already uploaded, and the process exited within the 30
second grace.

**BEHAVIOUR to keep in mind while reading the result.** The platform will call the run
cancelled regardless of your exit code, because it asks "was cancellation requested?"
before it looks at the code. So a test that only asserts "the run is cancelled" passes
even for a node that ignores SIGTERM completely. Assert the marker and the timing.

---

## Level 3: the real thing

Register the node, start an agent, run the pipeline. See
[OPERATIONS.md](OPERATIONS.md#registering-a-node).

**What only level 3 proves.** That your image's uid can read its credentials; that the
agent's environment allowlist permits every variable your deployment declares; that your
output ports arrive downstream as the artifact kinds you expected; that collection accepts
your hashes.

**RECOMMENDATION.** Run at least one job whose work takes longer than fifteen minutes
before you trust the node in production. That is the single threshold that separates a
node which reloads its credentials from one which does not, and nothing shorter will
reveal the difference.

---

## What testing cannot prove

**Write order.** Nothing can establish, after the fact, that you wrote the marker last.
The platform does not try: it checks the **consequences**, by copying every object your
marker names into a place your credentials cannot reach and re-reading it there
(`pipelines/external_finalize.py:966-1015`). A marker written too early shows up as a size
or hash mismatch, or as a missing object, and never as "you wrote things in the wrong
order". A local test can record the order your program wrote things in, which is useful,
but it is a statement about your test double and not about the contract.

**That a fake endpoint behaves like the real one.** A fake is either stricter or laxer
than the storage service, never identical. Stricter is the safer error, and it is still an
error: a suite that refuses something the platform permits will send you refactoring
correct code. Say which of the two yours is, in the harness, so a failure can be read.

**That a green run means the contract is satisfied.** Every test has a subject, and a
suite that does not say which is which is unreadable. Three kinds:

1. tests whose subject is **your node**, which you can make pass by editing your node;
2. tests whose subject is **the platform**, the agent or the collector, which need an
   emulator or a fixture and can never be turned green by editing your node;
3. tests whose subject is **the harness itself**.

Without those labels, an implementer cannot tell a real defect from a gap in the harness,
and a green run proves nothing in particular.

**Anything a check validates against itself.** Ask of every check: what independent thing
does this compare against? Four real examples from this project's own history. Byte
accounting measured with the encoder its own test hand-picked. A guard test that set only
the obsolete environment variable it existed to catch, so it encoded the bug. A proposal
to diff generated documentation against its own generator. A drift guard comparing a
snapshot with a version file sitting beside it. All four are green forever and prove
nothing.

**RECOMMENDATION.** Distrust a conformance claim you cannot trace to a specific check in
the platform. A suite built from a prose description of this contract once reported 23
defects in the reference node, of which 6 were contract violations. The rest were the
suite's author's preferences, presented as rules. That is the failure this document set is
organised to avoid, and it is why every statement here carries a label.

---

## If a `conformance/` directory exists in this repository

Read its own README first. It will say which of the three subjects above each test has,
and whether its fake endpoint is stricter or laxer than the storage service. A test in
that suite that fails your node is a reason to read the check, not automatically a reason
to change your node: check it against [PROTOCOL.md](PROTOCOL.md), and if the two disagree,
one of them is wrong and it is worth finding out which.
