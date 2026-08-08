# CONFORMANCE: testing a node, and what a test can prove

This preamble is **background** — it is about this document. Three levels follow, each
cheap and each proving something the one before it does not, and then a section on what
none of them proves, which matters more than it sounds: a suite that claims more than it
establishes teaches wrong code onward.

Labels as elsewhere — **RULE**, **BEHAVIOUR**, **RECOMMENDATION** — and each one governs
the statement it opens, including any list or table that continues it, until the next
label or the next heading. The paragraph you are reading now is **background**, and that is
the convention working: it describes this document rather than the platform. Two other
kinds of paragraph here are background for the same reason and carry no label: the ones
headed *What ... proves* / *What it does not prove* and the ones saying how a list here was
produced or how complete it is, which are about the method and the evidence rather than
about the platform; and the captions that introduce a fixture or a command. See
[README.md](README.md#how-to-read-this-three-kinds-of-statement).

---

## Level 1: run it with a hand-written envelope

**RECOMMENDATION, for this whole section.** No orchestrator, no network, no docker: build
the fixture, write the two documents by hand, and run your program. Everything below is
literal — copy it and run it.

First the input file — this paragraph is **background** about the fixture. It is twelve
bytes, and the hash below is its real sha256; check it yourself with the last line.

```bash
mkdir -p /tmp/job/in /tmp/job/out/attempts/1/gen-1
printf 'a,b\n1,2\n3,4\n' > /tmp/job/in/rows.csv

wc -c /tmp/job/in/rows.csv     # -> 12
sha256sum /tmp/job/in/rows.csv # -> b9485148546419a0f6a85e8d708c923557c15d7f3c7d078ef1fa7f7c0f57d5a5
```

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
     "sha256": "b9485148546419a0f6a85e8d708c923557c15d7f3c7d078ef1fa7f7c0f57d5a5", "size": 12,
     "local_path": "/tmp/job/in/rows.csv"}
  ],
  "staging": {"mode": "local_path", "path": "/tmp/job/out/attempts/1/gen-1"}
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
                  "sha256": "b9485148546419a0f6a85e8d708c923557c15d7f3c7d078ef1fa7f7c0f57d5a5",
                  "size": 12, "relpath": "rows.csv"}]}
  ],
  "staging_prefix": "file:///tmp/job/out/attempts/1/gen-1",
  "credentials_file": "/tmp/job/creds.json",
  "timeout_seconds": 900,
  "idempotency_key": "execution-1-attempt-1",
  "attempt": 1,
  "generation": 1
}
```

**RECOMMENDATION, and it is the whole point of splitting this in two.** Then two separate
runs, from the root of this repository. **They are not variants of one command and neither
substitutes for the other**: the first shows a complete program working end to end, and the
second is the one that actually tests whether your node reads its credentials from the
right place. Run both.

#### Run A — the whole cycle, against this repository's node

```bash
LSPO_CREDENTIALS_FILE=/tmp/job/creds.json LSPO_CREDENTIALS=/tmp/job/creds.json \
LSPO_EXECUTION_ID=1 LSPO_ATTEMPT=1 LSPO_GENERATION=1 \
LSPO_IDEMPOTENCY_KEY=execution-1-attempt-1 LSPO_CONTRACT_VERSION=1 \
LSPO_STAGING_PREFIX=file:///tmp/job/out/attempts/1/gen-1 \
LSPO_INVOCATION_URI=file:///tmp/job/invocation.json \
python3 node.py
```

**BEHAVIOUR, with one RECOMMENDATION inside it: why two credentials variables, when the
platform only sets one.** The agent sets `LSPO_CREDENTIALS_FILE` and nothing else
([PROTOCOL.md](PROTOCOL.md#11-environment-variables)), and that is the one your node should
read. The `node.py` in this repository still reads `LSPO_CREDENTIALS` — a known defect,
listed in [AUTHORING.md](AUTHORING.md#known-gaps-in-nodepy) — so this run sets the obsolete
name purely as a **compatibility shim**, to get a complete read-work-write-marker cycle out
of the file that is really here. **Your own node should read `LSPO_CREDENTIALS_FILE` and
nothing else**, and then it needs only the first of the two.

This is what run A prints, verbatim:

```
INFO execution 1 attempt 1
INFO done — 1 file(s), 12 bytes
```

it exits 0, and it leaves exactly three files behind:

```
/tmp/job/out/attempts/1/gen-1/outputs/rows.csv
/tmp/job/out/attempts/1/gen-1/result.json
/tmp/job/out/attempts/1/gen-1/__lspo_complete.json
```

**What run A proves, and it is less than it looks.** That a program in the right shape can
complete the cycle against these two fixtures. It proves **nothing whatever** about which
environment variable a node reads, because it sets both of them — a node that reads only
the obsolete name passes it just as happily as a correct one. A green run here is
compatibility evidence, not conformance evidence, and it is split out for exactly that
reason: a check that cannot fail for the thing it is named after is worse than no check,
because it reports success.

#### Run B — the check that the credentials variable is right

Delete the staging directory, recreate it empty, then run with **only** the real name:

```bash
rm -rf /tmp/job/out/attempts/1/gen-1 && mkdir -p /tmp/job/out/attempts/1/gen-1

env -u LSPO_CREDENTIALS \
LSPO_CREDENTIALS_FILE=/tmp/job/creds.json \
LSPO_EXECUTION_ID=1 LSPO_ATTEMPT=1 LSPO_GENERATION=1 \
LSPO_IDEMPOTENCY_KEY=execution-1-attempt-1 LSPO_CONTRACT_VERSION=1 \
LSPO_STAGING_PREFIX=file:///tmp/job/out/attempts/1/gen-1 \
LSPO_INVOCATION_URI=file:///tmp/job/invocation.json \
python3 node.py
```

**RECOMMENDATION.** This is the run to put in your own suite. A correct node produces the
same output and the same three files as run A. Assert the exit code, the three filenames,
and that the staging directory is otherwise empty.

**BEHAVIOUR of this repository's example, and it is a known defect rather than a surprise.**
`node.py` **fails run B**, and that is the correct result for the file as it stands. This is
verbatim what it does:

```
Traceback (most recent call last):
  File ".../node.py", line 297, in <module>
    sys.exit(main())
             ~~~~^^
  File ".../node.py", line 260, in main
    envelope = load_envelope()
  File ".../node.py", line 176, in load_envelope
    raise StepError('LSPO_CREDENTIALS is not set; the agent did not mount a credentials file')
StepError: LSPO_CREDENTIALS is not set; the agent did not mount a credentials file
```

It exits **1** and leaves the staging directory **empty** — no outputs, no `result.json`, no
marker. Two of the defects listed in [AUTHORING.md](AUTHORING.md#known-gaps-in-nodepy) are
visible in those nine lines at once: the wrong variable name, and a bootstrap outside the
`try` that turns a configuration mistake into an uncaught traceback instead of one short
line on stderr. Do not treat this failure as a broken fixture; treat it as the check doing
its job on a node that has the defect.

`timeout_seconds` is 900 in the fixture because that is what a registered node really gets;
see [PROTOCOL.md](PROTOCOL.md#24-how-long-you-actually-get). Nothing in either offline run
enforces it — it is there so the number your code reads offline is the number it will read
in production.

**RULE, worth knowing before you hand-write a manifest.** `staging_prefix` must end with
`attempts/<attempt>/gen-<generation>` matching the `attempt` and `generation` fields, or
the document is invalid. A single trailing slash is tolerated, since the check strips
trailing slashes before comparing. This is the first thing that rejects a hand-made
manifest, and it is not arbitrary: that suffix is the fence keeping a superseded runner
out of the live attempt's area (`external/contract.py:476-492`).

Everything above is **background** about this document: it was all executed while these
documents were written, at the commit named in [README.md](README.md#provenance). The
fixture was built, **both** runs were performed, and the output shown for each — the two
log lines and three files for run A, the traceback and empty directory for run B — is what
they produced. The manifest and the credentials envelope shown here, the marker `node.py`
wrote, and the marker shown in
[PROTOCOL.md](PROTOCOL.md#5-the-completion-marker) were all parsed with the orchestrator's
own `InvocationManifest` and `CompletionMarker`, and every refusal listed below was
exercised against them one at a time. The input file's twelve bytes and its hash were
computed from the file itself.

What was **not** executed is everything else: no container, agent, orchestrator run or
upload was exercised while writing this document set, so every statement about the agent's
behaviour, the storage service and collection is read from the source and reasoned about
rather than measured.

**What level 1 proves.** From run B: that your program finds its credentials through the
one variable the platform sets. From either run: that it parses both documents, verifies
input pins, writes objects, and writes a marker with a consistent inventory. Together that
is most of the contract.

**What it does not prove.** Anything to do with expiry, cancellation, object-store
refusals, or the collection side.

### Validating your own marker

**RECOMMENDATION.** You do not have the orchestrator's parser, so write a short checker
and run it over your marker in the test. Everything in the two lists below is a **RULE** —
each one is a refusal the real parser makes, so a marker breaking any of them fails your
run. That is why these are worth reproducing in a test at all; the recommendations
elsewhere in these documents are not checkable this way.

**How this list was produced, because it matters for how much you should trust it.** It
was not written from memory. A harness read the parser's own model definition, enumerated
every field it declares and every cross-field check it runs, and then fed it about ninety
mutated markers one at a time, recording which the parser accepted and which it refused.
Every field and every check came back with at least one exercised refusal, and nothing in
the model was left without one. See [README.md](README.md#provenance) for the commit and
for what "exercised" means here.

**RULE, for every item in the list that follows.** Each one is a refusal the marker parser
really makes (`external/contract.py:519-636`), so a marker breaking any of them fails your
run. **RECOMMENDATION**, separately and for the whole list: reproduce them in your own
checker, since you do not have that parser.

*The document as a whole*

* a payload that is not a JSON object at all — an array, a bare string, `null`;
* a `schema_version` that is not a real integer, in particular JSON `true` (which in
  Python compares equal to 1 and would otherwise be read as version 1), the string `"1"`,
  or `1.0`;
* a `schema_version` that is a whole number this contract has no parser for, such as `2`;
* a document over **8 MiB**, refused by the reader before it is parsed at all.

*Fields that must be there*

* a missing `execution_id`, `attempt`, `generation` or `status`. All four are required and
  a document without any one of them is invalid.

*Numbers*

* `execution_id`, `attempt` or `generation` that is `null`, a string such as `"1"`, JSON
  `true`, a float such as `1.0`, zero, or negative — they are strict integers of at least
  1;
* an object `size` that is `null`, a string, a boolean, a float, or negative. Zero **is**
  accepted: an empty file is a real file;
* an `exit_code` that is a string, a boolean or a float. It is optional, and a **negative**
  value is accepted deliberately — that is how a signal death is reported.

*Text*

* a `status` that is anything other than exactly `succeeded`, `failed` or `cancelled`.
  `"SUCCEEDED"` is refused: the match is case-sensitive;
* an `idempotency_key` that is empty, whitespace-only, or not a string. It is optional and
  may be absent or `null`;
* an `error` that is not a string — a number, an object, a list. It is optional. Note that
  control characters inside it are **cleaned, not refused** (see the second list);
* a `sha256` that is not exactly 64 lowercase hexadecimal characters: no `sha256:` prefix,
  no uppercase, no trailing newline, not 63 or 65 characters, no non-hex letters, and not
  a non-string.

*Paths and port names*

* a relpath that is not a string, or is empty, absolute, contains a backslash, contains
  **any control character in U+0000–U+001F — tab, newline, NUL and escape included** — has
  a `.` or `..` component, or has an empty component such as `a//b` or a trailing slash;
* **the same relpath rules applied to the relpaths listed under `produced_ports`**, which
  are validated in their own right and not merely looked up in the inventory;
* an output port name that is empty, whitespace-only, contains a control character, or is
  not a string.

*Shape*

* an `objects` that is not a list (`null`, or an object keyed by relpath), or a list whose
  entries are not objects (a bare string, `null`);
* a `produced_ports` that is not an object (a list), or a port whose value is not a list of
  relpaths (a bare string, `null`, an object);
* an object entry missing its `relpath`, its `sha256` or its `size`.

*The three cross-field checks*

* a duplicate relpath in `objects` — one file, one entry;
* a relpath in `produced_ports` that is absent from `objects`;
* a relpath repeated **within one port**.

**BEHAVIOUR, for the whole list below: the platform ACCEPTS every one of these.** That is a
different kind of fact from the list above — those name a check that refuses you, these name
the absence of one — and it is why this list does not carry the RULE label even though it
sits beside one. **RECOMMENDATION**, for the same list: do not let your own checker refuse
them, because a checker stricter than the platform sends you rewriting correct code.

* a negative `exit_code`, and an absent or `null` one;
* an object whose `size` is 0;
* a field the checker has never heard of, anywhere in the document. Unknown fields are
  ignored by design, so that adding one is not a breaking change;
* control characters inside `error` — they are stripped out as the document is parsed, and
  the rest of your sentence survives. A checker that refuses a marker over its error text
  is stricter than the platform;
* the **same relpath under two different ports**. Only repeating it within one port is
  refused;
* a port whose relpath list is empty;
* a `failed` or `cancelled` marker that still declares `produced_ports`.

**BEHAVIOUR, and it is the one surprise in the set.** A port name is checked but **not
trimmed**. `" output "` with its spaces is accepted exactly as written and becomes the
artifact kind downstream steps have to match, spaces and all. Trim your own port names.

**How complete this is, stated exactly.** Every field the marker parser declares and every
cross-field check it runs is represented above by at least one refusal that was actually
executed against it, and the field list was read off the parser rather than typed from
memory — so no field or check is missing. That is a different claim from "every value that
would ever be refused is named here", which no list can make. **The parser is the
authority.** If your checker passes and collection still refuses your marker, that
disagreement is a defect in this list and is worth reporting.

---

## Level 2: a fake object-store endpoint

**RECOMMENDATION, for this whole section.** Run your node against a small HTTP server that
stands in for the storage service: it serves the inputs and the job description on
presigned-looking GET URLs, and accepts form POSTs to a fake upload endpoint.

**RECOMMENDATION.** Have the fake refuse a POST whose `key` does not start with the
declared `key_prefix`, and one whose body exceeds the per-object ceiling. Those are the
two conditions the real policy carries, and a node that gets them wrong fails in
production and passes against a permissive fake.

### Testing credential rotation without a clock

**RECOMMENDATION.** Model **generations plus a fake clock**, not wall-clock time. A
time-based test is either slow or flaky.

**RECOMMENDATION, and do not confuse rotation with revocation, or the test fails a correct
node.** Installing a new envelope revokes nothing: a presigned URL from envelope A stays valid
until **its own** `expires_at`, whatever happens to the file it arrived in
([PROTOCOL.md](PROTOCOL.md#43-credentials-expire-during-your-run)). A fake that starts
refusing A's signatures the moment B is written is modelling a platform we do not have,
and it fails exactly the nodes that are right: caching A until A says it is stale is
correct behaviour, explicitly permitted, and arguably the better implementation.

So the fake needs a clock it controls, and A has to actually **expire** before it is
refused:

1. The harness owns a clock the node reads — an injected time source, a monkeypatched
   `time.time`, or a `faketime`-style shim. Give envelope A an `expires_at` a few
   simulated minutes ahead.
2. Start the node. Let it use A for its inputs. The fake accepts A's signatures, because
   they are valid.
3. Atomically replace `creds.json` with envelope B, whose `expires_at` is later. **The
   fake keeps accepting A**: nothing has expired yet.
4. Advance the fake clock past A's `expires_at`. **Now** the fake refuses A's signatures,
   with whatever the storage service really returns for an expired policy, and accepts
   B's.
5. A node that cached A and never looks again fails deterministically at its first upload.
   A node that re-reads at or near `expires_at` picks up B and succeeds. A node that
   cached A but watches its expiry also succeeds — and must, because that is a correct
   implementation.

Then rotate B to C, again with an expiry the harness steps past, after the first output
upload. That separates "reloads once, after processing" from "reloads whenever the
envelope says it is stale", which is the property that actually matters at the end of a
long run.

**RECOMMENDATION.** Have the fake refuse an expired signature the way the storage service
does — an HTTP error, not a Python exception of your own invention — and assert that your
node recognises it. A node that treats "expired" as an unknown transport error will retry
blindly instead of re-reading its credentials.

**RECOMMENDATION, and it is a property of the test harness rather than of your node.** Mount
the **directory**, not the file, if you run this inside docker. A bind-mounted file keeps pointing at the replaced
inode and will never appear to change, so a harness that mounts the file will report every
node as broken. That is exactly why the platform mounts the directory
(`agent/creds.py:10-16`).

### Testing the stop path

**RECOMMENDATION.** Send SIGTERM **during** a transfer, not only between work units. A
cooperative flag cannot be checked while the process is blocked in a socket read, and that
is the case a real stop hits. Assert three things: a `cancelled` marker exists, it
inventories the objects that were already uploaded, and the process exited **within a few
seconds** — not merely within the 30 seconds the grace nominally offers, for the reason
below.

**BEHAVIOUR, and it decides what this test is evidence of.** A local SIGTERM models your
node's own behaviour on a stop, and nothing more. It is not a model of what the platform
then does with the result, on any of the three stop paths. An **operator pressing Cancel**
usually arrives as a SIGKILL your process never sees, and even on the narrow path where it
arrives as a SIGTERM, nothing the node writes is collected. The **runtime deadline** does
begin as a SIGTERM, but the grace is cut short by the platform's own next heartbeat, the
upload credentials expired at the deadline, and the terminal report that would have made a
marker count is refused ([PROTOCOL.md](PROTOCOL.md#7-cancellation)). So label this test for
what it proves — that your node stops cleanly and promptly when asked — and do not let it
stand as evidence that a stopped run delivers a partial result, because today no stop path
does.

**BEHAVIOUR to keep in mind while reading the result.** When the agent classifies at all, it
calls the run cancelled regardless of your exit code, because it asks "was cancellation
requested?" before it looks at the code. So a test that only asserts "the run is cancelled"
passes even for a node that ignores SIGTERM completely. Assert the marker and the timing.

---

## Level 3: the real thing

**RECOMMENDATION.** Register the node, start an agent, run the pipeline. See
[OPERATIONS.md](OPERATIONS.md#registering-a-node).

**What only level 3 proves.** That your image's uid can read its credentials; that the
agent's environment allowlist permits every variable your deployment declares; that your
output ports arrive downstream as the artifact kinds you expected; that collection accepts
your hashes.

**RECOMMENDATION, and it takes one deliberate step to set up.** Run at least one job whose
work takes longer than fifteen minutes before you trust the node in production. That is
the single threshold that separates a node which reloads its credentials from one which
does not, and nothing shorter will reveal the difference.

**BEHAVIOUR, and it is why that test needs a setup step.** A node registered either
documented way is given a runtime budget of **900 seconds**, and at that moment the agent
begins stopping your container — a SIGTERM, then a SIGKILL that in practice arrives well
inside the thirty seconds it advertises ([PROTOCOL.md](PROTOCOL.md#7-cancellation)). So a
job "longer than fifteen minutes" is simply stopped, the run is left parked rather than
reported, and it proves nothing about credentials.

**RECOMMENDATION, and nothing in the platform requires it — it is a precondition of the
test, not a rule about your node.** Set `timeout_seconds` on the pipeline node (for example
5400) before you try it; that takes effect immediately, without publishing a new revision.
See [PROTOCOL.md](PROTOCOL.md#24-how-long-you-actually-get) for how the number is resolved
and [OPERATIONS.md](OPERATIONS.md#pointing-a-pipeline-node-at-your-deployment) for where to
put it.

That step is not an inconvenience in the test, it is the test: raising the budget is
exactly the change an operator makes the first time a real job needs more than a quarter
of an hour, and it is the moment a node that reads its credentials once stops working.

---

## What testing cannot prove

**BEHAVIOUR — write order.** Nothing can establish, after the fact, that you wrote the
marker last. The platform does not try: it checks the **consequences**, by copying every object your
marker names into a place your credentials cannot reach and re-reading it there
(`pipelines/external_finalize.py:1103-1152`). A marker written too early shows up as a
size or hash mismatch, or as a missing object, and never as "you wrote things in the wrong
order". A local test can record the order your program wrote things in, which is useful,
but it is a statement about your test double and not about the contract. This is why
"write the marker last" is labelled a recommendation everywhere in these documents: it is
the most important thing in the set that nothing enforces.

**RECOMMENDATION — that a fake endpoint behaves like the real one.** A fake is either
stricter or laxer than the storage service, never identical. Stricter is the safer error,
and it is still an error: a suite that refuses something the platform permits will send you
refactoring correct code. Say which of the two yours is, in the harness, so a failure can
be read.

**RECOMMENDATION — that a green run means the contract is satisfied.** Every test has a
subject, and a suite that does not say which is which is unreadable. Three kinds:

1. tests whose subject is **your node**, which you can make pass by editing your node;
2. tests whose subject is **the platform**, the agent or the collector, which need an
   emulator or a fixture and can never be turned green by editing your node;
3. tests whose subject is **the harness itself**.

Without those labels, an implementer cannot tell a real defect from a gap in the harness,
and a green run proves nothing in particular.

**RECOMMENDATION — anything a check validates against itself.** Ask of every check: what
independent thing does this compare against? Five real examples from this project's own
history. Byte accounting measured with the encoder its own test hand-picked. A guard test
that set only the obsolete environment variable it existed to catch, so it encoded the bug.
A proposal to diff generated documentation against its own generator. A drift guard
comparing a snapshot with a version file sitting beside it. And the fifth is on this page:
the offline example above used to be **one** command that set both credentials variables
while claiming to prove the node read the right one, so a node reading only the obsolete
name passed the check that existed to catch it. All five are green forever and prove
nothing.

**RECOMMENDATION.** Distrust a conformance claim you cannot trace to a specific check in
the platform. A suite built from a prose description of this contract once reported 23
defects in the reference node, of which 6 were contract violations. The rest were the
suite's author's preferences, presented as rules. That is the failure this document set is
organised to avoid, and it is why every statement here carries a label.

---

## If a `conformance/` directory exists in this repository

This section is **background**, about a directory that may or may not exist beside these
documents. Read its own README first. It will say which of the three subjects above each
test has, and whether its fake endpoint is stricter or laxer than the storage service. A
test in that suite that fails your node is a reason to read the check, not automatically a
reason to change your node: check it against [PROTOCOL.md](PROTOCOL.md), and if the two
disagree, one of them is wrong and it is worth finding out which.
