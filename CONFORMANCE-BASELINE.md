# Conformance baseline — what this node does today

Measured, not reasoned about. Every line below is the observed behaviour of the image
built from this repository's own `Dockerfile`, run as a container against the harness in
`conformance/`, on Linux with Docker 28.4.

```
python -m pytest --red-for-real -q
→ 21 failed, 94 passed in 138s

python -m pytest -q                     # CI mode
→ 94 passed, 21 xfailed in 137s
```

The 21 failures are exactly the 21 tests marked `expected_red_until_fixed`. Nothing else
fails.

| Group | Tests | Today |
|---|---|---|
| `expected_red_until_fixed` | 21 | fail |
| `conforms_today` | 17 | pass |
| `subject_is_platform` | 59 | pass |
| `harness_self_test` | 18 | pass |

**These numbers were re-measured from zero after the harness was corrected.** They are
not an edit of the previous run's numbers, and the previous table is not comparable to
this one: tests were withdrawn, split, renamed and relabelled, and the store the node is
measured against had its credential model rewritten.

---

## Read this first: what changed, and why the list got shorter

The first version of this harness asserted **23 defects**. A review found that several of
them were not defects at all — they were preferences of ours, or inferences about the
contract that the contract does not make. A harness that demands more than the contract
sends the next author to write wrong code, and the documentation written from that code
teaches it to every node author afterwards. So every remaining assertion now has to trace
to a rule somebody can quote, and each test says which kind of authority it rests on:

| Basis | Meaning | Expected-red today |
|---|---|---|
| `basis_contract` | The rule exists in the platform's sources and is quoted in the test. | **10** |
| `basis_our_policy` | A compatibility or hygiene choice *this repository* makes. | 3 |
| `basis_reference_quality` | What a reference implementation should demonstrate; the contract permits otherwise. | 8 |

**So the claim "this node violates the contract" went from 23 to 10.** The other eleven
are still worth fixing and are still red — but they are labelled honestly, and none of
them may be written down as conformance requirements.

`tests/conftest.py` enforces this: a test declaring `basis_contract` whose citation does
not name one of the authoritative source files fails at collection time. Run
`python -m pytest --collect-only -q --print-labels` for the whole table.

### The five defects that were WITHDRAWN, and why

**1. "`result.json` must not be delivered on an output port."** Withdrawn — it is legal,
and the orchestrator's own test for this very file expects it. The collector turns every
relpath in `produced_ports` into a downstream artifact and does not care which files they
are (`pipelines/external_finalize.py`, the loop over `marker.produced_ports`), and
`tests/test_hello_node_example.py` asserts `produced_ports['output'] == ['outputs/rows.csv',
'result.json']`. **Kept as a recommendation only:** handing every downstream step the
step's own metrics document as though it were data is a design smell, and a pipeline
author may reasonably want it off a port. It is not a conformance failure and this harness
no longer treats it as one.

**2. "The step must report progress."** Withdrawn as a requirement. `agent/logbuf.py`:
*"Progress is opt-in and out of band… A step that never writes one simply has no
progress."* A node that emits nothing is fully conformant. **Relabelled** to
reference-quality: the example is where an author learns the protocol exists, so the
reference node should demonstrate it. The test was also tightened, because it could be
satisfied by one constant line at startup — it now requires at least two distinct
fractions.

**3. "A single 503 on a read must be retried in-process."** Withdrawn. The contract's
mechanism for a transient fault is to exit `1` and let the **orchestrator** re-run the
attempt; retrying inside the step is an optimisation, not a rule. The node already exits
1 here, so this is now a `conforms_today` regression guard instead. The old test also
never proved a retry happened: silently skipping the refused input and succeeding would
have passed it.

**4. "Credentials must be re-read before every transfer."** Withdrawn — the platform does
not ask for it, and the harness could only "prove" it by modelling something that does not
happen. Issuing a fresh envelope does **not** revoke the URLs already in the container's
hands: a presigned URL is a signature over a deadline and nothing takes it back. The old
store killed every earlier credential the instant a new one was minted, which failed a
perfectly correct expiry-aware step. **Reframed** (see defect 2 below) around what really
happens: envelopes **expire**, and the file on disk is refreshed before they do.

**5. "A cancelled step must exit 20 and its marker must say `exit_code: 20`."** Withdrawn.
`agent/runner.py` `_classify` checks whether cancellation was requested **before** it looks
at the exit code, so a SIGKILLed cancelled container is still recorded as *cancelled*, not
retried. **Reframed** on the costs that are real: thirty seconds of a runner slot per
cancelled job, a receipt saying `succeeded` inside a launch recorded as cancelled, and work
that was explicitly cancelled being done anyway.

Two further claims were withdrawn as *wrong*, not merely over-strong:

* **"The agent injects exactly nine variables."** It injects nine **plus** whatever the
  manifest's `env_names` asks for and the operator's allowlist permits, with the nine
  applied last so a manifest cannot shadow them.
* **"A marker must carry `idempotency_key`, `exit_code` and `error`."** All three are
  optional (`… | None = None`), collection cross-checks only execution/attempt/generation,
  and the orchestrator's own test deletes `idempotency_key` and still parses the marker.
  What survives is the conditional form: *if* the marker states an exit code, it must be
  the one the process returned.

---

## The ten contract defects, measured

Each one names the rule it breaks, quoted. The full citation lives on the test itself.

### 1. It reads a credentials variable that nothing sets

> `agent/runner.py` `_workload_env` sets `'LSPO_CREDENTIALS_FILE': context.creds.mount_file`
> for every job; `agent/creds.py`: *"The FIRST envelope decides where the file lives inside
> the container… the issuer's own `credentials_file` is the authority on the mount path."*

`node.py:174` reads `LSPO_CREDENTIALS`. Nothing on the orchestrator's side sets that name.

| Test | Observed |
|---|---|
| `test_the_credentials_file_variable_is_honoured` | Credentials mounted at `/lspo/creds/envelope.json` and named by `LSPO_CREDENTIALS_FILE`. The step ignored it, opened the baked-in path and died: `FileNotFoundError: … '/lspo/creds/creds.json'`. Exit 1, no marker. |

It works today only because the image's own `Dockerfile` bakes
`ENV LSPO_CREDENTIALS=/lspo/creds/creds.json`, **and** the orchestrator's issuer currently
always names the default path. Either can change without warning; the first customer
deployment handed a different path breaks the node completely.

### 2. Credentials are read once, so the node cannot outlive one envelope

> `runners/credentials.py`: *"The envelope expires in `LSPO_RUNNER_CREDS_TTL_S` (default 15
> minutes), clamped to whatever remains of the job's own runtime budget… A long job calls
> `POST /jobs/<id>/sign` for a fresh set."*
> `agent/creds.py`: *"**Refresh before expiry, not after.** … the agent asks for a new
> envelope once the remaining validity drops under a margin, **so the workload never has to
> handle an expired file**."*

The file on disk is kept fresh, atomically, in a directory the container has mounted. A
step that read it once into a variable is not holding that file — it is holding a
fifteen-minute-old copy of it, and no refresh on the agent's side reaches it.

| Test | Observed |
|---|---|
| `test_a_step_that_outlives_its_envelope_reads_the_fresh_one` | `upload of 'outputs/data.csv' was refused with HTTP 403 … had already expired when the request arrived`. Exit **10 (permanent)** — the orchestrator is told never to retry. |
| `test_a_transfer_refused_on_an_expired_credential_is_repeated_after_re_reading` | The store took the body, the credential died, the refusal followed. Same 403, and `could not write the failure marker: … 403` — the run leaves nothing behind at all. |
| `test_the_work_remaining_after_an_expiry_is_still_done` | Three inputs; after the expiry the step fetched `['input/one.csv']` and stopped. Everything past the fifteen-minute mark simply does not happen. |

**What these tests no longer demand:** that the step re-read the file before every single
transfer. Checking the stated `expires_at`, or re-reading on a refusal, or both, are all
correct. A 403 that the step recovers from is not a failure.

### 3. A failed run reports an empty inventory, so partial output is abandoned

> `external/contract.py` `CompletionMarker.objects`: *"Every object produced, with hash and
> size, relative to the staging prefix."*
> `pipelines/external_finalize.py` `_salvage_what_the_step_produced` iterates
> `for obj in marker.objects` — *"Publish what a FAILED or cancelled step managed to write."*

`node.py:271` — `write_marker(envelope, manifest, [], status='failed', …)`. The literal
empty list is the whole defect.

> `['outputs/one.csv'] were uploaded and then abandoned: the failure marker inventories
> nothing, so salvage will publish none of them`

An empty inventory does not mean "nothing was produced". It means "everything produced is
stranded in a staging directory nobody will look at again". On a wide batch that is hours
of work.

### 4. A transient store refusal on an upload is reported as permanent

> `external/contract.py` `classify_exit`: *"a step that means 'do not retry me' must say so
> with `EXIT_PERMANENT`"* — and `EXIT_PERMANENT = 10`.

`_post_object` raises the step's own permanent-failure type for **every** status outside
200/201/204. One 503 (`SlowDown`, which object storage answers under load) therefore tells
the platform never to run this work again.

> `a single 503 on an upload ended the run with exit 10, which classifies as 'permanent' —
> the orchestrator will not retry it`

The read path already gets this right (exit 1), and that is now asserted as a regression
guard. Note what is **not** required: the step does not have to retry the upload itself.

### 5. The marker's exit code contradicts the process's

> `external/contract.py` `CompletionMarker.exit_code`: *"Process exit code, when the runner
> observed one."*
> `pipelines/external_finalize.py` `_step_account` renders it verbatim into the run's
> failure reason: *"The step exited with code {marker.exit_code} and reported: …"*

`node.py:236` writes 10 for every unsuccessful run whatever the process returned, while
`main()` returns 1 for anything that is not the step's own `StepError`.

> `the marker says the step exited 10, the process exited 1`

The orchestrator retries the attempt (correct, from the exit code) while the sentence
attached to the run says the step failed permanently. The test is now conditional: a marker
that omits `exit_code` entirely is conformant and passes.

### 6. Two ports carrying the same filename produce a marker the collector refuses

> `external/contract.py` `CompletionMarker._check_inventory_is_unique`: *"one file, one
> entry (two entries could otherwise carry two different hashes for the same path)."*
> `pipelines/external_finalize.py`: *"The step wrote an invalid completion marker."*

`node.py:198` derives the output path from the input's `relpath` alone and ignores the
`port` the envelope carries beside it — `runners/credentials.py` puts one there:
`entry = {key: obj[key] for key in ('port', 'relpath', 'sha256', 'size')}`.

> `duplicate relpath(s) ['outputs/data.csv'] in the objects inventory`

The run exits 0 and publishes **nothing**, because the whole document is refused at parse
time. That is the worst possible shape for a failure: everything looked fine from outside.

The second consequence — one input's bytes silently overwritten by the other's — is
tracked separately as a reference-quality expectation, because *which* output path an
object belongs on is the step's own business.

### 7. It writes a `result.json` larger than the contract's reader will accept

> `external/contract.py`: `MAX_RESULT_BYTES = 1024 * 1024`, and *"anything approaching a
> megabyte there is payload in the wrong place."*
> `external/io.py` `_read_json` reads `limit + 1` bytes and raises `ContractDocumentTooLarge`.

> `result.json is 3145907 bytes, over the 1048576-byte ceiling`

The step copies `params` into its summary without looking at their size, so a manifest the
orchestrator was happy to write (3 MiB, well inside the 8 MiB manifest ceiling) produces a
document the contract's own reader is forbidden to read. **Honest caveat:** nothing in the
current collection path calls `read_result`, so today the oversized document is written and
never read. The ceiling bounds writes as well as reads, and the day anything reads it, it
becomes an outage on the far side of all the real work.

### 8. A refused request prints the presigned URL

> `agent/redact.py`: *"A presigned URL is not an address with a password attached: **the
> query string IS the credential**… `requests` in particular puts the full URL into the text
> of an HTTP error."*
> `agent/runner.py` pumps the container's output into the log buffer **unredacted**
> (`stream_logs(context.handle, context.buffer.add)`) — the agent redacts its own messages,
> not the workload's.

> `credential material reached the log: ['?tok=']`

Whatever the step prints is stored with the execution, shown to anyone who can see the run,
and searchable. The credentials are short-lived; a leaked one is still a leak.

---

## The three defects that are OUR policy, not the contract's

All three are about the credentials variable, and none of them is a rule anybody can be
held to. `LSPO_CREDENTIALS` appears nowhere in the platform: this repository's own
`Dockerfile` is what sets it, so how it interacts with the real variable is our decision
and we should say so rather than dress it up.

| Test | Our policy | Observed |
|---|---|---|
| `test_with_no_variable_at_all_the_default_path_is_used` | Fall back to `DEFAULT_CREDENTIALS_FILE` when neither variable is set. The agent always sets its variable, so this is defence, not conformance. | `StepError: LSPO_CREDENTIALS is not set`. Exit 1. |
| `test_when_both_variables_are_set_the_current_one_wins` | The name the agent actually sets wins, because it is the only one whose value the orchestrator chose. | The step used the superseded envelope: `403 … had already expired`. Exit 1. |
| `test_a_disagreement_between_the_two_variables_is_reported_and_agreement_is_not` | Say something when the two disagree — by name, never by value — and stay quiet when they agree. | Neither variable name appears anywhere in the output. An operator mid-migration gets a 403 and nothing connecting it to the two variables that disagreed at startup. |

The compatibility half is asserted *now*, before any fix: `test_the_legacy_variable_on_its_own_is_still_honoured`
passes today and must keep passing, because images in the field bake the old name.

---

## The eight reference-quality gaps

The contract permits every one of these. They are red because this repository is the file
customers copy, and a reference implementation that demonstrates the wrong habit teaches it
to everyone who starts from it. **None of them may be documented as a requirement.**

### Cancellation — four tests

The step is **PID 1** in its own container. The kernel does not apply a signal's default
action to PID 1: it delivers the signal only if a handler is installed. `node.py` installs
none, so SIGTERM is dropped on the floor and the process carries on working. Verified
directly: the same image with a one-line handler exits immediately; without one it survives
SIGTERM.

What that costs, measured:

| Test | Observed |
|---|---|
| `test_a_step_that_cannot_be_stopped_costs_the_whole_grace_period` | `docker had to wait the full 30s grace (30.3s measured) and then SIGKILL it` — half a minute of a runner slot per cancelled job. |
| `test_a_step_stopped_during_a_download_does_not_claim_it_succeeded` | The marker says `succeeded`, inside a launch the orchestrator has recorded as cancelled. |
| `test_a_step_stopped_during_an_upload_inventories_what_it_left_behind` | Same, plus: whatever landed is only salvageable if the marker names it. |
| `test_a_cancelled_step_stops_taking_on_new_work` | `after being asked to stop, the step went on to fetch 3 of 3 inputs` — the request changes nothing. |

**What is NOT at stake:** the run is recorded as cancelled either way. `agent/runner.py`
`_classify` asks `if context.cancel_requested.is_set() or exit_code == EXIT_CANCELLED`
*before* it looks at the exit code, so a SIGKILLed cancelled container is cancelled, not
retried. Exit 20 is available and is the clearest thing to return; it is not required, and
these tests no longer assert it.

### The other four

| Test | What it expects, and why it is not a rule |
|---|---|
| `test_a_failure_before_the_manifest_is_read_still_writes_a_marker` | The contract *invites* a failure marker rather than requiring one, and the collector treats an absent one as an ordinary outcome. This is a promise **this file** makes and breaks: `main()`'s docstring says *"Never raises: every failure becomes a marker plus an exit code"*, while the two calls that load the credentials and fetch the manifest sit outside the block that would make it true. The identity a marker needs is in the injected environment, so the promise is keepable. Observed: `the step died before its manifest and wrote no marker at all`. |
| `test_an_input_larger_than_the_container_is_not_fatal` | Memory is the operator's ceiling and input size is the pipeline's; the contract says nothing. Observed: `the container was OOM-killed reading a 128 MiB input into 64m of memory; it exited 137, which classifies as 'transient' — so this is retried forever`. |
| `test_neither_input_survives_at_the_others_expense` | Which output path an object belongs on is the step's business. But the collapse silently destroys one of two inputs' bytes, and a step that loses data is not one to copy. How to keep them apart is the fix slice's decision; the harness does not pre-empt it. |
| `test_the_reference_node_demonstrates_the_progress_protocol` | Progress is opt-in. Observed: `the step reported 0 progress sample(s)`. A long batch is indistinguishable from a hung one for its whole duration, and the example is where an author would learn the protocol exists. |

---

## What the node already gets right

Seventeen `conforms_today` tests, asserted now so that fixing the above cannot quietly
break them. Ten of them are contract rules; the rest are the reference behaviour this file
is supposed to teach:

* the completion marker is the **last** object uploaded, is uploaded **exactly once**, and
  everything it inventories arrived strictly before it;
* the marker carries the three identity fields collection cross-checks, and every
  inventoried hash and size matches the bytes the store actually received;
* everything the step wrote is inventoried, and every inventoried relpath names a key the
  store really holds — including names with `%`, spaces, non-Latin characters and nested
  directories;
* a persistent transient fault exits with a code the contract classifies as transient;
* a permanent failure still writes a `failed` marker with a reason;
* inputs are verified against their pin — changed bytes, a wrong size and a missing
  `sha256` are all refused, permanently, with the object named;
* zero inputs, several inputs across two ports, and every input copied through byte for
  byte;
* `params` reach the step unchanged, including nesting, floats, booleans, nulls and
  non-ASCII text;
* unknown additive fields in the envelope, the manifest and each input entry are ignored,
  not rejected;
* an ordinary successful run prints no credential material;
* the legacy credentials variable, used alone, still works.

---

## The harness's own bugs, found and fixed this round

The instrument was wrong in five ways. Four of them made it too strict; one of them could
have made it report a hung container as a success.

**A hung container was reported as exit 0.** `Container.wait` returned `None` on timeout,
`Job.run` ignored that, and `collect()` then reported docker's `State.ExitCode` — which is
`0` for a container that is still running. Any test inspecting only the exit code and the
logs could have passed vacuously, in the direction of "the node is fine". `wait` now raises
`ContainerDidNotExit`, and a running container yields `exit_code=None`, never a number.
Proven by `test_a_container_that_never_exits_is_never_reported_as_a_success`.

**Credentials were revoked rather than expired.** Described above; the store now expires a
credential at the moment it says, judged when the request arrives, and issuing a new one
revokes nothing. Proven by `test_a_credential_expires_but_is_not_revoked_by_a_newer_one`.

**The store answered with the FIRST write of an object.** Real S3 exposes the last one. A
hash test could have blessed bytes that were replaced, or rejected a step that legitimately
re-sent an object after a refused transfer. Proven by
`test_upload_order_is_arrival_order_and_a_re_upload_wins`.

**Uploads were accepted with most of the presigned POST form missing.** The endpoint
checked one custom token, the key prefix and the size, and ignored `policy`,
`x-amz-signature`, `x-amz-algorithm` and `x-amz-credential`. A node that forwarded the URL
and dropped the rest would have passed here and been refused by S3. Now every signed field
is checked, with a test per field.

**The local contract validator disagreed with the real parser.** It rejected a document
that omits `schema_version` (the real one defaults it to 1), accepted `"schema_version":
true` (the real one refuses booleans explicitly, because `True == 1` in Python), and
checked neither the prefix-layout rules nor half the manifest's field types. All fixed, and
the orchestrator's own three frozen golden documents are now checked in under
`conformance/fixtures/` and validated by the harness — so "my re-implementation agrees with
the real one" is a measurement rather than a claim.

Four smaller ones: the marker-order test never asserted the marker was written *once*; the
input-coverage assertions counted requests rather than distinct objects (three retries of
one input looked like three inputs); the cancellation-during-upload test never checked that
the interrupted output was inventoried; and the "disagreement is reported" test could be
satisfied by printing both variable names unconditionally, which is noise rather than a
warning — it now requires silence when the two agree.

---

## What I could not test, and why

* **The 8 MiB marker ceiling.** Reaching it needs roughly forty thousand inventoried
  objects; the run time is not worth the coverage. The 1 MiB result ceiling is tested and
  is the same class of defect.
* **The local (demo) credential scheme.** `node.py` has a whole branch for
  `scheme: "local"` — `local_path` inputs and a `local_path` staging directory. This
  harness exercises only the S3 branch. Covering it means bind-mounting the staging
  directory read-write and running the container as the harness's own uid, which is what
  the agent does in demo mode; it is a straightforward extension, not a blocked one.
  (The orchestrator's own `tests/test_hello_node_example.py` does exercise that branch.)
* **The agent's real credential-file permissions, end to end.** The interaction is measured
  and written down in `tests/test_platform_rules.py`, but with a synthetic directory rather
  than one a running agent produced. The harness itself deliberately uses 0755/0644 for
  every other test, so that a permission problem can never be mistaken for a node defect.
* **The environment allowlist end-to-end.** The agent refuses a manifest that names a
  variable outside `LSPO_AGENT_ALLOWED_ENV` *before the container starts*, so no black-box
  test of the node can observe it. The rule is written down and executable in
  `conformance/platform_rules.py`.
* **Whether the platform's own behaviour drifts.** The `subject_is_platform` group is
  mostly *restatements* — this harness's copy of a rule, checked against itself. Nobody
  runs this suite against the orchestrator, so a change there cannot turn these red by
  itself. Two of them are exceptions and really do exercise the mechanism: the
  bind-mounted-file test and the 0700-permissions test both run real containers. The
  citations on the rest are what make a restatement checkable by a human in one step, and
  that is how the "exactly nine variables" error was caught.
* **Generation fencing.** That a superseded runner physically cannot write into the live
  attempt's directory is a property of the staging prefix and the upload policy, not of the
  node. The harness proves the fence exists (a key outside the prefix is refused with 403)
  but not the orchestrator's half.
* **Exit code 30 (contention).** Nothing in this node can produce it.
* **`logs.ndjsonl`.** The contract names the file and the collector's comments describe it
  as something a step is *invited* to write, not required to. The node never writes one.
  Recorded as an observation rather than tested, because "invited" is not a rule a
  conformance test can hold anyone to.
* **Real S3.** No AWS signature verification, no request-time skew, no chunked uploads, no
  versioning, no eventual consistency. The fake endpoint is stricter than the real one
  about ordering and about proving a fence was exercised, and now matches it on credential
  expiry, last-write-wins and the presigned POST form.
