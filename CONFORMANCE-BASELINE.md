# Conformance baseline — what this node does today

Measured, not reasoned about. Every line below is the observed behaviour of the image
built from this repository's own `Dockerfile`, run as a container against the harness in
`conformance/`, on Linux with Docker 28.4.

```
LSPO_ORCHESTRATOR_SRC=… LSPO_ORCHESTRATOR_REF=origin/master python -m pytest --red-for-real -q
→ 20 failed, 100 passed in 144s

python -m pytest -q                     # CI mode, no orchestrator sources present
→ 99 passed, 1 skipped, 20 xfailed in 141s
```

The 20 failures are exactly the 20 tests marked `expected_red_until_fixed`. Nothing else
fails. The one skip is the verbatim citation check, which needs a checkout of the
orchestrator; with `LSPO_ORCHESTRATOR_SRC` set it runs and passes, which is how the second
line above reaches 100.

| Group | Tests | Today |
|---|---|---|
| `expected_red_until_fixed` | 20 | fail |
| `conforms_today` | 21 | pass |
| `subject_is_platform` | 59 | pass |
| `harness_self_test` | 20 | pass |

**Re-measured from zero.** These are not an edit of the previous run's numbers. Tests were
removed, split, relabelled and rewritten between the two, and the fake store the node is
measured against had a whole behaviour taken out of it.

---

## Read this first: the claim "this node violates the contract" is now 6, down from 23

The first version of this harness asserted **23 defects**. The second cut that to 10 by
withdrawing five claims outright and relabelling the rest. A third review found the
remaining ten still over-claimed, and named the cause: the harness kept calling something
a CONTRACT rule because the rule was *about that area*, not because the rule, read
literally, made the node's behaviour a violation.

The test applied this round is that one sentence. *Does the quoted rule, read literally,
make the observed behaviour a violation?* Not "is there a rule nearby". Not "would a good
node do this". If the rule says "a marker, if written, must …", then a test requiring a
marker to exist is not resting on it. Where the answer was arguable, the test was demoted.

| Basis | Meaning | Expected-red today |
|---|---|---|
| `basis_contract` | The rule exists in the platform's sources, is quoted in the test, and read literally it makes this behaviour a violation. | **6** |
| `basis_our_policy` | A compatibility or hygiene choice *this repository* makes. | 3 |
| `basis_reference_quality` | What a reference implementation should demonstrate; the contract permits otherwise. | 11 |

**Every one of the twenty is still red and still worth fixing.** What changed is what may
be written down as a requirement, and the answer is now six things.

### What moved this round, and why

**Removed — the harness was modelling something that cannot happen.** One rotation test
required the step to retry an upload that the store had *accepted* and then refused. To
produce that, the fake store killed a credential after the request body had arrived and
re-authorized the request on the way out. That is revocation, not expiry: a presigned URL
is a signature over a deadline and an issuer cannot take one back mid-transfer, so real
S3 has no way to produce this and no platform source asks a step to survive it. The test,
the hook that created it, and the store's ability to express it are all gone.

**Demoted — credential expiry (2 tests).** That this node cannot outlive one credential
lifetime is real, measured, and severe. It is not a rule anybody wrote down. What the
sources state is what the PLATFORM does: the envelope expires, and the agent refreshes
`creds.json` in place "so the workload never has to handle an expired file"
(`agent/creds.py`). Read literally that is a guarantee offered to the step, not a duty
placed on it; "so re-read the file rather than caching it at startup" is the obvious
inference and this harness no longer labels inferences as contract. **This is the
demotion I am least comfortable with** — see "One demotion I would argue about" below.

**Demoted — not printing your own credential (2 tests).** `agent/redact.py` governs how
the AGENT cleans URLs out of the messages *it* writes. `agent/runner.py` shows a
workload's output going into the log buffer verbatim — which is a description of what
happens to what the step prints, not a prohibition on printing it. Nothing makes a leaky
step non-conformant. It stays as the strongest reference-quality expectation in the suite.

**Split — five tests were asserting two things under one authority.** A conjunction is
only as strong as its weakest half, and each of these had a contract half and a weaker
one sharing a single citation:

| Was one test | Contract half | The other half, now separate |
|---|---|---|
| "a permanent failure writes a marker and exits ten" | the exit code is 10 | that a failed run writes a marker with a reason at all — *invited*, not required |
| "unknown additive fields are ignored" | the manifest — a contract model | the credential envelope and its input entries — not a model, no stated rule |
| "the inventory names the keys really written under" | inventoried relpaths name real objects | that every input reappears at `outputs/<relpath>` — this node's own layout |
| "the marker is written last and exactly once" | written last, after everything it names | written *once* — a second identical write at the end breaks no stated rule |
| "params reach the step unchanged" (already reference-quality) | — | unchanged |

**Made genuinely conditional — three tests.** They were described as conditional and were
not: each fetched the completion marker unconditionally, so a step that wrote none failed
on a missing file rather than on the rule being tested. They now skip, saying which legal
choice the step made.

### Withdrawn in earlier rounds, still withdrawn

`result.json` on an output port is legal; progress is opt-in; in-process retry of a 503 is
not required; credentials are never revoked; a cancelled step need not exit 20. The agent
injects nine variables **plus** what the manifest asked for and the operator allowed. A
marker's `idempotency_key`, `exit_code` and `error` are all optional.

---

## The six contract defects, measured

Each names the rule it breaks, quoted. The full citation lives on the test itself, and
every quotation in this repository has been checked against the platform's sources
verbatim — see "How citations are checked" below.

### 1. It reads a credentials variable that nothing sets

> `external/contract.py`: credentials are *"delivered out of band as a file at
> `credentials_file`"*, whose definition is *"In-container path where credentials are
> mounted."* `agent/runner.py` `_workload_env` puts exactly that path in the environment:
> `'LSPO_CREDENTIALS_FILE': context.creds.mount_file`, for every job.

The path is the platform's to choose and it is communicated in one place. `node.py:174`
reads `LSPO_CREDENTIALS`, which nothing on the orchestrator's side sets.

> `the step failed instead of reading /lspo/creds/envelope.json: … File "/app/node.py",
> line 177, in load_envelope: with open(path, 'rb') as handle` → `FileNotFoundError`

It works today only because the image's own `Dockerfile` bakes
`ENV LSPO_CREDENTIALS=/lspo/creds/creds.json` **and** the orchestrator's issuer currently
always names the default path. Either can change without warning; the first deployment
handed a different path breaks the node completely.

### 2. A failed run reports an empty inventory, so partial output is abandoned

> `external/contract.py` `CompletionMarker.objects`: *"Every object produced, with hash and
> size, relative to the staging prefix."*
> `pipelines/external_finalize.py` `_salvage_what_the_step_produced`: *"Publish what a
> FAILED or cancelled step managed to write."*

`node.py:271` — `write_marker(envelope, manifest, [], status='failed', …)`. The literal
empty list is the whole defect.

> `['outputs/one.csv'] were uploaded and then abandoned: the failure marker inventories
> nothing, so salvage will publish none of them`

An empty inventory does not mean "nothing was produced". It means "everything produced is
stranded in a staging directory nobody will look at again". On a wide batch that is hours
of work. The test now asks first whether a marker exists at all, because writing one is
not required — what is required is that a marker which *does* exist inventories every
object the step produced.

### 3. A transient store refusal on an upload is reported as permanent

> `external/contract.py`: *"The process exit code is the ONLY signal available when a job
> dies before it can write a marker, so the numbers carry meaning"* — and `classify_exit`:
> *"a step that means 'do not retry me' must say so with `EXIT_PERMANENT`"*, which is 10.

`_post_object` raises the step's own permanent-failure type for **every** status outside
200/201/204. One 503 (`SlowDown`, which object storage answers under load) therefore tells
the platform never to run this work again.

> `a single 503 on an upload ended the run with exit 10, which classifies as 'permanent' —
> the orchestrator will not retry it`

The read path already gets this right (exit 1), asserted now as a regression guard. Note
what is **not** required: the step need not retry the upload itself.

### 4. The marker's exit code contradicts the process's

> `external/contract.py` `CompletionMarker.exit_code`: *"Process exit code, when the runner
> observed one."*
> `pipelines/external_finalize.py` `_step_account` renders it verbatim into the run's
> failure reason: *"The step exited with code {marker.exit_code} and reported: …"*

`node.py:236` writes 10 for every unsuccessful run whatever the process returned, while
`main()` returns 1 for anything that is not the step's own `StepError`.

> `the marker says the step exited 10, the process exited 1`

The orchestrator retries the attempt (correct, from the exit code) while the sentence
attached to the run says the step failed permanently. Doubly conditional now: a step that
writes no marker, or a marker that omits `exit_code`, is conformant and the test skips.

### 5. Two ports carrying the same filename produce a marker the collector refuses

> `external/contract.py` `CompletionMarker._check_inventory_is_unique`: *"one file, one
> entry (two entries could otherwise carry two different hashes for the same path)."*

`node.py:198` derives the output path from the input's `relpath` alone and ignores the
`port` the envelope carries beside it — `runners/credentials.py` puts one there:
`entry = {key: obj[key] for key in ('port', 'relpath', 'sha256', 'size')}`.

> `duplicate relpath(s) ['outputs/data.csv'] in the objects inventory`

The run exits 0 and publishes **nothing**, because the whole document is refused at parse
time. That is the worst possible shape for a failure: everything looked fine from outside.

The second consequence — one input's bytes silently overwritten by the other's — is
tracked separately as reference quality, because *which* output path an object belongs on
is the step's own business.

### 6. It writes a `result.json` larger than the contract's reader will accept

> `external/contract.py`: *"Contract documents are control data, not payload, so every read
> AND every write is bounded"*, `MAX_RESULT_BYTES = 1024 * 1024`.
> `external/io.py`: *"bounding the write makes a producer fail loudly at the point of the
> mistake, instead of publishing a document that only turns out to be unreadable later, in
> somebody else's process."*

That second sentence is why this is a rule about the writer and not only about the reader.

> `result.json is 3145907 bytes, over the 1048576-byte ceiling`

The step copies `params` into its summary without looking at their size, so a manifest the
orchestrator was happy to write (3 MiB, well inside the 8 MiB manifest ceiling) produces a
document the contract's own reader is forbidden to read. **Honest caveat:** nothing in the
current collection path calls `read_result`, so today the oversized document is written and
never read.

---

## The three defects that are OUR policy, not the contract's

All three are about the credentials variable. `LSPO_CREDENTIALS` appears nowhere in the
platform: this repository's own `Dockerfile` is what sets it, so how it interacts with the
real variable is our decision.

| Test | Our policy | Observed |
|---|---|---|
| `test_with_no_variable_at_all_the_default_path_is_used` | Fall back to `DEFAULT_CREDENTIALS_FILE` when neither variable is set. The agent always sets its variable, so this is defence, not conformance. | `StepError: LSPO_CREDENTIALS is not set`. Exit 1. |
| `test_when_both_variables_are_set_the_current_one_wins` | The name the agent actually sets wins, because it is the only one whose value the orchestrator chose. | The step used the superseded envelope: `403 … had already expired`. Exit 1. |
| `test_a_disagreement_between_the_two_variables_is_reported_and_agreement_is_not` | Say something when the two disagree — by name, never by value — and stay quiet when they agree. | Neither variable name appears anywhere in the output. |

The compatibility half is asserted *now*, before any fix:
`test_the_legacy_variable_on_its_own_is_still_honoured` passes today and must keep passing,
because images in the field bake the old name.

---

## The eleven reference-quality gaps

The contract permits every one of these. They are red because this repository is the file
customers copy. **None of them may be documented as a requirement.**

### Credential expiry — 2 tests

The node reads `creds.json` once into a local variable in `main()`, so every later
transfer uses a copy that ages out.

| Test | Observed |
|---|---|
| `test_a_step_that_outlives_its_envelope_reads_the_fresh_one` | The upload is refused — `403 … had already expired when the request arrived` — and the run ends with exit 10, telling the orchestrator never to retry. |
| `test_the_work_remaining_after_an_expiry_is_still_done` | Three inputs; after the expiry the step had fetched `['input/one.csv']` and stopped. |

The consequence is a hard ceiling on runtime: past one credential lifetime the node can
upload neither its outputs nor the failure marker that would explain why. Two things are
worth separating from that. The exit code it uses to report the refusal **is** a contract
violation, and it is defect 3 above under its own citation. And what these tests do *not*
require is re-reading the file before every transfer — checking the stated `expires_at`,
or re-reading on a refusal, or both, are all correct answers.

### Not leaking credentials — 1 test red, 1 green

| Test | Observed |
|---|---|
| `test_a_refused_request_does_not_print_the_presigned_url` | **Red.** `credential material reached the log: ['?tok=']` — the traceback carries `403 Client Error: Forbidden for url: http://…/invocation.json?tok=gen-2`. |
| `test_an_ordinary_run_prints_no_credential_material` | **Green**, and asserted so that a fix for the one above cannot introduce the leak on the happy path. Counted under `conforms_today`, not here. |

`requests` builds the text of an HTTP error out of the URL, so letting an exception's
message through is exactly what publishes the credential. Whatever the step prints is
stored with the execution, shown to anyone who can see the run, and searchable.

### Cancellation — 4 tests

The step is **PID 1** in its own container. The kernel does not apply a signal's default
action to PID 1: it delivers the signal only if a handler is installed. `node.py` installs
none, so SIGTERM is dropped on the floor and the process carries on working.

| Test | Observed |
|---|---|
| `test_a_step_that_cannot_be_stopped_costs_the_whole_grace_period` | `docker had to wait the full 30s grace (30.2s measured) and then SIGKILL it` — half a minute of a runner slot per cancelled job. |
| `test_a_step_stopped_during_a_download_does_not_claim_it_succeeded` | The marker says `succeeded`, inside a launch the orchestrator has recorded as cancelled. |
| `test_a_step_stopped_during_an_upload_inventories_what_it_left_behind` | Same. Rebuilt this round around TWO inputs so that one output has genuinely landed when the signal arrives — with one input the inventory check was vacuous. |
| `test_a_cancelled_step_stops_taking_on_new_work` | `after being asked to stop, the step went on to fetch 3 of 3 inputs`. |

**What is NOT at stake:** the run is recorded as cancelled either way. `agent/runner.py`
`_classify` asks `if context.cancel_requested.is_set() or exit_code == EXIT_CANCELLED`
*before* it looks at the exit code.

### The other four

| Test | What it expects, and why it is not a rule |
|---|---|
| `test_a_failure_before_the_manifest_is_read_still_writes_a_marker` | The contract *invites* a failure marker; the collector treats an absent one as ordinary. This is a promise **this file** makes and breaks: `main()`'s docstring says *"Never raises: every failure becomes a marker plus an exit code"*, while the two calls that load the credentials and fetch the manifest sit outside the block that would make it true. Observed: `the step died before its manifest and wrote no marker at all; the store saw nothing`. |
| `test_an_input_larger_than_the_container_is_not_fatal` | Memory is the operator's ceiling and input size is the pipeline's. Observed: OOM-killed reading a 128 MiB input into 64 MiB; exit 137, which classifies as *transient* — so it is retried forever. |
| `test_neither_input_survives_at_the_others_expense` | Which output path an object belongs on is the step's business. But the collapse silently destroys one of two inputs' bytes. |
| `test_the_reference_node_demonstrates_the_progress_protocol` | Progress is opt-in. Observed: `the step reported 0 progress sample(s)`. The example is where an author would learn the protocol exists. |

---

## What the node already gets right

Twenty-one `conforms_today` tests, asserted now so that fixing the above cannot quietly
break them. Nine of them are contract rules; the rest are the reference behaviour this file
is supposed to teach:

* the completion marker is the **last** object uploaded, and everything it inventories
  arrived strictly before it — and it is written exactly once;
* the marker carries the three identity fields collection cross-checks, and every
  inventoried hash and size matches the bytes the store actually received;
* everything the step wrote is inventoried, and every inventoried relpath names a key the
  store really holds — including names with `%`, spaces, non-Latin characters and nested
  directories, which also survive the copy unchanged;
* a persistent transient fault exits with a code the contract classifies as transient;
* a permanent failure exits 10, and explains itself in a marker;
* inputs are verified against their pin — changed bytes, a wrong size and a missing
  `sha256` are all refused, permanently, with the object named. The hash-mismatch message
  is held to the exact words the orchestrator's own test for this file expects;
* zero inputs, several inputs across two ports, and every input copied through byte for
  byte;
* `params` reach the step unchanged, including nesting, floats, booleans, nulls and
  non-ASCII text;
* unknown additive fields are ignored, not rejected — in the manifest (a contract rule) and
  in the credential envelope and its input entries (our own expectation);
* an ordinary successful run prints no credential material;
* the legacy credentials variable, used alone, still works.

---

## How citations are checked, and what the check cannot do

A `basis_contract` label is a claim that the platform wrote a rule down. Until this round
the only thing enforced was that the citation *mentioned* one of the authoritative files —
which a paraphrase, a stale quotation, or no quotation at all passes just as easily.

Two checks now sit under `conformance/citations.py`:

**Structural, always on.** A `basis_contract` citation must name an authoritative file AND
contain a quoted fragment at least twelve characters long. Naming the area a rule lives in
is not citing the rule. This runs at collection time and needs nothing but the harness — it
caught one citation immediately, whose only "quotation" was the single word *transient*.

**Verbatim, opt-in.** Every quoted fragment must actually occur in one of the files the
citation names. Point `LSPO_ORCHESTRATOR_SRC` at an orchestrator checkout (optionally with
`LSPO_ORCHESTRATOR_REF` to read a git ref rather than the working tree) and the harness
checks all of them; without it the check skips and says so, because this repository is
standalone and does not vendor the platform. Matching ignores line wrapping, indentation,
Sphinx markup, comment markers, the seams between adjacent Python string literals, and
which quote character was used — the things that legitimately differ between a rule in a
source file and the same rule inside a citation.

Run against `origin/master` it found **eighteen** citations that did not survive the check.
Most were formatting artefacts on the harness's side, fixed in the matcher. Three were real:

* one cited a function that does exist but rendered three separate lines of its body as a
  single semicolon-joined line, which is a paraphrase presented as a quotation;
* one expanded `{ENV_PREFIX}ALLOWED_ENV` into `LSPO_AGENT_ALLOWED_ENV` and quoted the
  result as source text;
* one silently dropped an interpolated value out of the middle of a quoted error message.

All 38 citations in the suite now match `origin/master` verbatim.

**What neither check can do — and it is the important half.** They establish that the
sentence exists, not that it *supports* the assertion. Every over-claim corrected in this
harness so far cited a real file and quoted a real sentence; what was wrong was the step
from the sentence to the conclusion. That is a judgement about meaning, no string search
answers it, and it stays with whoever reviews the test. The structural check makes the
judgement *possible* by forcing the sentence into the open where a reader can weigh it.

---

## One demotion I would argue about

Calling the credential-expiry tests "reference quality" is the weakest label in this
document, and I want it on the record rather than buried.

A node that stops working after fifteen minutes is not merely unexemplary. The platform is
entitled to run a job for hours; it is entitled to refresh `creds.json` under a running
container; and the entire design of the credentials mount — a directory rather than a
file, an atomic replace rather than a rewrite — exists so a container can pick up the new
document. A step that cannot is unusable for anything but short work, and "the contract
permits it" is a thin thing to say about that.

But the rule genuinely is not written. Every sentence in the platform's sources describes
what the *agent* does; none places a duty on the workload. Under the test applied this
round — *does the quoted rule, read literally, make this a violation?* — the honest answer
is no, and inventing an obligation because it is obviously implied is the exact habit this
round was called to break.

The clean resolution is not a label. It is one sentence in `external/contract.py` saying
what a workload owes: *read the credentials file when you need credentials; do not cache
it past its stated `expires_at`.* Then the test moves back to `basis_contract` with a
citation, and no reader has to reconstruct the argument. **That is a request to the
platform, not a change this repository can make.**

---

## What I could not test, and why

* **The 8 MiB marker ceiling.** Reaching it needs roughly forty thousand inventoried
  objects; the run time is not worth the coverage. The 1 MiB result ceiling is tested and
  is the same class of defect.
* **The local (demo) credential scheme.** `node.py` has a whole branch for
  `scheme: "local"`. This harness exercises only the S3 branch. (The orchestrator's own
  `tests/test_hello_node_example.py` does exercise that branch.)
* **The agent's real credential-file permissions, end to end.** Measured in
  `tests/test_platform_rules.py` with a synthetic directory rather than one a running
  agent produced. The harness itself deliberately uses 0755/0644 everywhere else, so that
  a permission problem can never be mistaken for a node defect.
* **The environment allowlist end-to-end.** The agent refuses a manifest naming a variable
  outside `LSPO_AGENT_ALLOWED_ENV` *before the container starts*, so no black-box test of
  the node can observe it.
* **Whether the platform's own behaviour drifts.** The `subject_is_platform` group is
  mostly *restatements* — this harness's copy of a rule, checked against itself. Nobody
  runs this suite against the orchestrator, so a change there cannot turn these red by
  itself. The verbatim citation check is the closest thing to a tripwire, and it only runs
  where the sources are present. Two tests really do exercise the mechanism with real
  containers: the bind-mounted-file test and the 0700-permissions test.
* **Generation fencing.** That a superseded runner physically cannot write into the live
  attempt's directory is a property of the staging prefix and the upload policy, not of the
  node. The harness proves the fence exists (a key outside the prefix is refused with 403)
  but not the orchestrator's half.
* **Exit code 30 (contention).** Nothing in this node can produce it.
* **`logs.ndjsonl`.** The contract names the file and describes it as something a step is
  *invited* to write. The node never writes one. Recorded as an observation rather than
  tested.
* **Real S3.** No AWS signature verification, no request-time skew, no chunked uploads, no
  versioning, no eventual consistency.

---

## The harness's own bugs, found and fixed

### This round

**`docker stop`'s return code was discarded.** A failed stop returns in a fraction of a
second having done nothing — which reads to a cancellation test as a step that shut down
instantly and did no further work: short elapsed time, no marker, one input seen. Three
cancellation tests could have passed on a broken daemon. `stop` now raises, and every
cancellation test additionally asserts that the container really exited rather than
trusting that the stop call returned. Those tests bypass `wait()`, so the existing
hung-container self-test did not cover them.

**`docker inspect` mapped every failure onto "no exit code".** A daemon that had fallen
over, a permission problem and an unparseable answer all arrived at the tests as
`exit_code=None` — indistinguishable from an ordinary expected-red failure. Only a
confirmed "no such container" is now treated as an answer at all, and it raises rather
than returning. This is the same class of bug as the one fixed last round (`wait` used to
return `None` on a timeout); it is now finished, and proven by a self-test that removes a
running container and checks that `inspect`, `wait`, `collect` and `stop` all refuse to
answer.

**The store could un-authorize a request it had already accepted.** Described above; the
hook and the second authorization are gone, and a self-test now proves the opposite
property — a credential that expires *during* an upload does not retroactively refuse it.

**Two rotation tests had a race that failed a CORRECT implementation.** They published the
replacement `creds.json` after the response had been written, so the step could receive
the body and reopen the file before the swap landed — reading the expired document and
failing through no fault of its own. The refresh now happens while the request is still in
flight, which makes the fresh file strictly older than anything the step can do with the
response. A test that randomly fails the right answer is worse than no test.

**One cancellation test was vacuous for the property in its name.** "Everything that
landed is inventoried" compared an empty set against the marker: the store records an
upload when it answers, not when the bytes arrive, so with a single input nothing had
landed at the moment the step was signalled. It now uses two inputs and holds the *second*
upload, so one acknowledged object is on the ledger — and it asserts that there is one.

**A citation could name a file and quote nothing.** Described above.

### Earlier rounds, for the record

A hung container was reported as exit 0; credentials were revoked rather than expired; the
store answered with the FIRST write of an object rather than the last; uploads were
accepted with most of the presigned POST form missing; and the local contract validator
disagreed with the real parser in four ways. All fixed, and the orchestrator's three
frozen golden documents are checked in under `conformance/fixtures/` so that "my
re-implementation agrees with the real one" is a measurement rather than a claim.
