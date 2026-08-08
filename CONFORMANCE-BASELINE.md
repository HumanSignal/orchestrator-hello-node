# Conformance baseline — what this node does today

Measured, not reasoned about. Every line below is the observed behaviour of the image
built from this repository's own `Dockerfile`, run as a container against the harness in
`conformance/`, on Linux with Docker 28.4, with the citations checked against orchestrator
commit `6b2ff82c`.

(`origin/master` moved again between rounds — `e4c25192` → `6b2ff82c` — but all twelve
files this harness is allowed to cite are **byte-identical** across that move, so the three
drift mirrors described further down were re-checked and needed no change.)

```
LSPO_ORCHESTRATOR_SRC=… LSPO_ORCHESTRATOR_REF=origin/master python -m pytest --red-for-real -q
→ 20 failed, 109 passed in 149s      (and nothing skipped — measured with -rs)

python -m pytest -q                     # CI mode, no orchestrator sources present
→ 108 passed, 1 skipped, 20 xfailed in 148s
```

The 20 failures are exactly the 20 tests marked `expected_red_until_fixed`. Nothing else
fails, and nothing skips for a reason that depends on the node. The one skip is the
verbatim citation check, which needs a checkout of the orchestrator; with
`LSPO_ORCHESTRATOR_SRC` set it runs and passes, which is how the first line above reaches
109.

| Group | Tests | Today |
|---|---|---|
| `expected_red_until_fixed` | 20 | fail |
| `conforms_today` | 21 | pass |
| `subject_is_platform` | 67 | pass |
| `harness_self_test` | 21 | pass |

**Re-measured from zero, again.** These are not an edit of the previous run's numbers. Four
assertions were sharpened this round and nothing was added: no new test, no new harness
capability, no new claim. **The same twenty tests are red, for the same reasons**, and the
contract-defect count is unchanged at six. One test changed its basis label, so the suite's
`basis_contract` total moves from 83 to 82 and `basis_reference_quality` from 23 to 24.

**Several tests can now SKIP, and a skip is a finding of a different kind.** Where a rule
constrains something optional — a marker, a `result.json`, an object that may or may not
have been produced yet — a run in which that thing does not exist is an *inapplicable*
condition, not a pass and not a failure. Those tests say so, naming which legal choice the
step made. None of them skips against the node as it stands; if one starts to, the run
summary is where it shows up.

---

## Read this first: the claim "this node violates the contract" is 6, down from 23

The first version of this harness asserted **23 defects**. The second cut that to 10 by
withdrawing five claims outright and relabelling the rest. A third review found the
remaining ten still over-claimed, and named the cause: the harness kept calling something
a CONTRACT rule because the rule was *about that area*, not because the rule, read
literally, made the node's behaviour a violation. Six survived that test and all six still
stand: the fourth review changed no label and withdrew no claim, and the fifth withdrew no
claim either — it moved ONE test out of `basis_contract`, and that test was never one of the
six (it passes today, and it is a regression guard rather than a defect).

The test is that one sentence. *Does the quoted rule, read literally, make the observed
behaviour a violation?* Not "is there a rule nearby". Not "would a good node do this". If
the rule says "a marker, if written, must …", then a test requiring a marker to exist is
not resting on it. Where the answer was arguable, the test was demoted.

What the fourth review found was the same habit one level down, inside tests whose LABELS
were right: a test that names the prohibited outcome correctly and then also insists on one
particular way of avoiding it. The fifth found the last few places where an assertion and
the prohibition it names were still not quite the same sentence — including two where the
gap ran the other way and let the prohibited outcome through. Both are below.

| Basis | Meaning | Expected-red today |
|---|---|---|
| `basis_contract` | The rule exists in the platform's sources, is quoted in the test, and read literally it makes this behaviour a violation. | **6** |
| `basis_our_policy` | A compatibility or hygiene choice *this repository* makes. | 3 |
| `basis_reference_quality` | What a reference implementation should demonstrate; the contract permits otherwise. | 11 |

**Every one of the twenty is still red and still worth fixing.** What changed is what may
be written down as a requirement, and the answer is now six things.

### What moved this round: an assertion must match the prohibition it names, exactly

Four fixes, all of one kind — an assertion that was slightly wider or slightly narrower
than the sentence it claimed to be enforcing. **Two of them were false passes**, which for
a gate is the dangerous direction: the harness would have gone green over a node doing the
very thing the test exists to forbid. Each was proved closed by building the evading
behaviour into a throwaway copy of `node.py` and running the old and new assertions against
it, and none of that touched the real `node.py`, which is unchanged.

| # | What was wrong | Proof it is closed |
|---|---|---|
| 1 | **The result-document ceiling looked only at the surviving write.** It read the document back with `body_of`, which returns the LAST accepted upload of a name — deliberately, because that is what an object store serves. But the rule bounds *every* write, so a step could upload an oversized document, overwrite it with a small one, and pass. The oracle is now the size of every accepted upload of that name. | A copy of the node that uploads its real 3,145,907-byte document and then overwrites it with a 59-byte one: the old assertion **passed**; the new one fails, naming both sizes. |
| 2 | **The collision test accepted exit 0 with no marker.** It discarded the run's outcome and skipped whenever the marker was absent. But absence is permitted only after a failure or a cancellation: the agent reports exit 0 as a success, and the collector refuses a reported success with no marker — the run fails at collection having done all the work. So deleting the offending document while still exiting 0 evaded the test entirely. "Not applicable" is now conditional on the run having actually failed. | A copy of the node that suppresses a marker it knows is unparseable and still exits 0: the old test **skipped** (green); the new one fails on the exit code. |
| 3 | **Refusing an unverified input cannot carry a contract label**, and it affected two tests. Nothing anywhere obliges a workload to check its inputs against their pins, so "exit 10 because the bytes differ from the pin" rests on this node's own behaviour, not the platform's. `test_a_permanent_failure_exits_ten` is now reference quality — the exit *numbers* are contract, the premise is not. The salvage test keeps its contract-labelled inventory assertion, and "the node did not fail" became another not-applicable condition. | A copy of the node with pin verification removed — conformant, breaking no rule: both tests were **red** before, one of them under a contract label. Now the exit-code test is red under an honest reference-quality label and the salvage test skips. |
| 4 | **The collision's sibling forbade additional outputs.** It compared the set of output bodies for equality, while its stated expectation is only that neither input was lost. A correct fix that also wrote an index or a report would have gone red. The expected bodies are now a subset of what landed. | A copy of the node that namespaces outputs by port *and* writes an auxiliary index: the old assertion **failed** on the extra object; the new one passes. |

The shape common to all four: a test may assert exactly the outcome it forbids, and must
not quietly import a second requirement — neither a stronger one that fails a correct fix
(3 and 4) nor a weaker one that lets the forbidden outcome through by another door (1 and 2).

### What moved in the round before: assert the prohibition, never prescribe the remedy

A contract test says *"this run must not exhibit the prohibited outcome"*, and every other
outcome passes. The moment it also says "and it must exit 0", or "and this optional
document must exist", it has stopped describing the contract and started describing one
particular conforming implementation — so a **correct fix goes red for the wrong reason**.
That is worse than having no test at all, and it was the single cause of three of the five
findings that round. The other two were bugs in the harness itself, and they are at the
bottom of this document.

Six assertions were rewritten from *must do X* into *must not do Y*:

| Test | Was | Is now |
|---|---|---|
| the `result.json` ceiling | the run exits 0, **and** a result document exists, **and** it is under 1 MiB | no result document over 1 MiB reaches the store. Failing loudly instead of writing one is what the cited rule says it wants; writing none is legal, and skips |
| two ports carrying one filename | the run exits 0 **and** the marker parses | if a marker was written, it parses. Noticing the collision and refusing the job — with a valid failure marker or with none — is conformant |
| salvage after a failure | something landed, **and** the marker names it | *if* something landed, the marker names it. A step that verifies every input before producing anything has nothing to inventory, and that is an inapplicable condition, not a failure |
| neither input survives the other | the run exits 0 **and** both bodies are in the store | the same, but a run that refused the job skips: nothing was silently overwritten, and refusing is one of the fixes this test promises not to pre-empt |
| the inventory names real keys | the run exits 0, **and** the inventory is non-empty, **and** every relpath in it exists | every relpath the marker inventories names an object the store holds. No marker, or an empty inventory, skips |
| unknown fields are ignored (the manifest, and its envelope twin) | the run *with* the extra fields exits 0 | the same job runs twice and the two outcomes must **match**. "Adding a field is not a breaking change" is a claim about a difference; "this node succeeds" was never part of it |

The last one is the shape worth copying. When a rule is about a difference, measure the
difference — an absolute assertion in its place quietly imports a second requirement that
nobody wrote down.

Four of those six rows were sharpened again in the round after, for the reasons in the
section above this one: three of them were still not quite the sentence they claimed to
enforce, and two of those three were passing runs they should have failed.

**Where the line was drawn, because it can be drawn absurdly.** Read literally enough, the
contract requires a step to produce nothing at all, so *every* test here is inapplicable to
a node that does nothing — which would leave a suite that proves nothing. The bar used is:
**could a plausible fix of this node legitimately make this premise false?** Refusing a
job with colliding filenames, verifying every input before writing anything, failing rather
than writing an oversized document — yes, all three, and all three are now accommodated. An
ordinary two-input job completing at all: no fix under discussion changes that, so those
premises stay assertions. The one place this line was uncomfortable is that several tests
would fail a node which stopped verifying its input hashes; verifying is not required by
the contract, but it is this node's own advertised behaviour, asserted in its own right, so
those tests were treated as entitled to assume it.

**That last sentence was too generous, and the round after this one corrected it.** A test
may assume the node's own behaviour freely — but not while carrying a CONTRACT label,
because the label is a claim about the platform's rules and the premise is not one of them.
So the two tests that turned "the bytes do not match the pin" into a required failure were
separated: the one whose whole subject is the exit code became reference quality, and the
one whose subject is a marker's inventory kept its contract label by treating "the step did
not fail" as another inapplicable condition. The general rule is worth stating plainly: **a
conjunction is only as strong as its weakest half, and that applies to a test's premise
exactly as much as to its assertion.**

### What moved two rounds before, and why

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
of work. The test is doubly conditional, because two entirely different things are optional
here: writing a marker at all, and having produced anything by the time the failure lands.
A step that verifies every input *before* it writes anything fails this job with an empty
store — nothing to inventory, nothing omitted, so the test skips rather than demanding this
node keep its present order of work. What is required is that a marker which *does* exist
inventories every object that *does* exist.

A third condition was added in the last round, and it is what keeps the contract label
honest: **the run must actually have failed.** Salvage is the path the collector takes for a
step that failed or was cancelled; a successful attempt is verified and published by an
entirely different one. Since nothing obliges a workload to verify its input pins, a
perfectly conformant implementation may simply complete this job — and the test used to
assert that it had failed, which is a demand the contract never makes. It skips instead.

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

The rule forbids exactly one thing — writing a document the collector cannot parse — so
that is all the test asserts. Refusing the job on the collision is conformant, with a valid
failure marker or with none at all, and the test does not require any particular exit code.

It does *read* the exit code, for one purpose: to decide what an absent marker means. After
a failure or a cancellation, silence is permitted and the test skips. After exit 0 it is not
— the agent reports that as a success and the collector then refuses the run outright,
*"Nothing can be published: the marker is the inventory of what the step produced"* — so a
"fix" that simply stopped writing the offending document while still exiting 0 would trade
an unpublishable run for an unpublishable run, and the test fails it.

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

The test forbids the oversized document and nothing else. `external/io.py` explicitly wants
a producer to *"fail loudly at the point of the mistake"*, so ending the run non-zero is a
conforming fix and the exit code is not asserted; `result.json` is optional, so writing none
skips. An earlier version required both, which would have failed either correct fix.

**Every accepted upload of that name is measured, not the one that survived.** The rule
bounds the write, so each write answers for itself. Reading the document back the way an
object store serves it — the most recent write — would have let a step publish the oversized
document and then overwrite it with a small one, and a ceiling a producer may step over and
tidy up after is not a ceiling.

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
or re-reading on a refusal, or both, are all correct answers. The one sentence that would
turn this pair into a contract claim, and the scoping it needs to be true, is at the bottom
of this document under "One demotion I would argue about".

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
| `test_a_step_stopped_during_an_upload_inventories_what_it_left_behind` | Same. Rebuilt around TWO inputs so that one output has genuinely landed when the signal arrives (with one, the inventory check was vacuous), and it now waits for the store to go quiet before reading it — see the harness bugs below for why that mattered. |
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
* a permanent failure exits 10, and explains itself in a marker. **Both halves of that line
  are reference quality, not conformance.** The exit NUMBERS and their meanings are the
  contract's, but nothing obliges a workload to treat an input that fails its pin as a
  failure at all — so the premise is this node's own behaviour, and the label follows the
  weaker half. It is still a regression guard worth keeping: a step that checks its pins
  and then reports the mismatch as *transient* asks the platform to re-run a job that can
  never succeed, and unrecognised exit codes classify as transient, so getting this wrong
  is the default;
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

All 37 citations in the suite — 84 quoted fragments — match `6b2ff82c` verbatim. (The count
was 38 a round ago: one more citation was withdrawn when the exit-code test became reference
quality, and none was added. A round before that one was withdrawn and one added, so the
count was unchanged by coincidence. The earlier withdrawn
one quoted a real sentence about credentials expiring and used it to support a claim about
*when a store authorizes a request* — which that sentence, and no other in the platform,
says anything about. The check could not have caught that; only a reader can.)

### The check earned itself a round ago: it caught the platform moving

Everything it had found before was a formatting artefact on this side or a
quotation-that-was-really-a-paraphrase. This round it caught what it was built for — the
platform reworded a rule and a restatement here went stale — and it did so within minutes
of the change landing. `origin/master` moved from `d8a78355` to `e4c25192` (*"the
container's own output, and the eight other holes in the log/artifact path"*) while this
round was being measured, and that commit touches three of the eleven files this harness is
allowed to cite.

One citation stopped matching: the progress-line rule quoted
`str(payload.get('phase') or '')[:64]`, and the agent now writes
`phase = without_control_characters(str(payload.get('phase') or ''))` before truncating.
That is not a cosmetic rewording. The phase travels in the **heartbeat, which is the
request that renews the lease**; the server's field refuses control characters and a
refusal fails the whole heartbeat — so a step that puts one in its phase every interval
never renews its lease, and its execution is parked for ever.

Three platform rules had drifted away from this harness's re-implementation, and all three
are mirrored now:

| The platform now | This harness had |
|---|---|
| refuses a relpath containing **any** control character — U+0000 above all, because no filesystem can hold it in a name | refused only a line break |
| refuses an output-port name that is empty, blank, or carries a control character (`check_port_name`, new) | did not look at port names |
| drops control characters from a progress phase before truncating it | truncated the raw string |

Names are **refused**; prose (an `error`, a phase) is **cleaned**. The asymmetry is the
platform's and it is deliberate: a cleaned identifier would quietly name a different file
or match a different port, while a cleaned message is still the run's account of itself.

The lesson for anyone reading the numbers in this document: **they are measured against a
named commit**, and the citation check is the only thing that notices when that commit
stops being the one the citations describe. Run it with `LSPO_ORCHESTRATOR_REF` set to the
commit the node is deployed against.

**What neither check can do — and it is the important half.** They establish that the
sentence exists, not that it *supports* the assertion. Every over-claim corrected in this
harness so far cited a real file and quoted a real sentence; what was wrong was the step
from the sentence to the conclusion. That is a judgement about meaning, no string search
answers it, and it stays with whoever reviews the test. The structural check makes the
judgement *possible* by forcing the sentence into the open where a reader can weigh it.

---

## One demotion I would argue about — and the sentence that would settle it

Calling the credential-expiry tests "reference quality" is the weakest label in this
document, and I want it on the record rather than buried.

A node that stops working after fifteen minutes is not merely unexemplary. The platform is
entitled to run a job for hours; it is entitled to refresh `creds.json` under a running
container; and the entire design of the credentials mount — a directory rather than a
file, an atomic replace rather than a rewrite — exists so a container can pick up the new
document. A step that cannot is unusable for anything but short work, and "the contract
permits it" is a thin thing to say about that.

But the rule genuinely is not written. Every sentence in the platform's sources describes
what the *agent* does; none places a duty on the workload. Under the test applied here —
*does the quoted rule, read literally, make this a violation?* — the honest answer is no,
and inventing an obligation because it is obviously implied is the exact habit these
rounds were called to break.

### The proposed platform change

The clean resolution is not a label. It is one sentence in the platform's own sources —
`external/contract.py` is where a workload author would look for it — saying what a
workload owes:

> A workload may cache an envelope until its `expires_at`; before initiating a credentialed
> request at or after that instant it must reopen the file named by `LSPO_CREDENTIALS_FILE`
> and use the current envelope, while a request initiated before that instant may complete
> without re-reading or retrying solely because that instant passes.

**That is a request to the orchestrator repository, not a change this one can make.**

The scoping is the whole content of it, and an earlier draft — *"read the credentials file
when you need credentials; do not cache it past its stated `expires_at`"* — was rejected in
review for being broader than anybody wants in two directions. "Read it when you need
credentials" reads as *before every transfer*, which would make a step that holds a live
envelope across ten uploads non-conformant for no benefit; a re-read per object is pure
cost, and the agent refreshes precisely "so the workload never has to handle an expired
file". And "do not cache past expiry" reads as *abort what you have already started*, which
asks for the one thing nothing can deliver: a presigned request already in flight cannot be
re-signed, and the store will not refuse it either — it authorized the request when it
arrived. The sentence above says only what a workload can actually act on: **look again
before you begin; never abandon something already begun.**

Until that sentence exists, nothing in this repository may carry `basis_contract` on it —
including the obvious candidate, "beginning a request with an envelope that has already
expired". That is a self-inflicted 403, not a violation of any written rule, and labelling
it as one would be this harness making the same mistake a fourth time. When the sentence
lands, two things follow mechanically: the two rotation tests move from
`basis_reference_quality` to `basis_contract` with it as their citation, and the
begin-after-expiry case becomes worth splitting out as a test of its own.

---

## What I could not test, and why

* **The 8 MiB marker ceiling.** Reaching it needs roughly forty thousand inventoried
  objects; the run time is not worth the coverage. The 1 MiB result ceiling is tested and
  is the same class of defect.
* **An oversized document the store REFUSED.** The result-ceiling oracle reads every
  *accepted* upload, not every attempt. A step that tried to publish an oversized document
  and was turned away by the store would not be caught. That is deliberate — nothing a
  reader can ever see was produced, so there is no document in violation — but it is a
  scoping choice, and it is written down here rather than left to be rediscovered.
* **A cancellation that ends in exit 0.** The collision test now treats "no marker" as
  permitted only when the run did not exit 0, because the agent classifies exit 0 as a
  success. That equivalence holds *in this test*, which never asks for a cancellation. It
  is not a general rule: `agent/runner.py` `_classify` reports a job as cancelled whenever
  cancellation was requested, **whatever the exit code**, so the same condition copied into
  a cancellation test would be wrong there. Anyone reusing this shape must ask whether a
  cancellation could have been requested.
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
  where the sources are present — **it fired for the first time this round**, on a rule
  the platform reworded while the baseline was being measured (see "The check earned itself
  this round"). It catches a rule whose WORDS changed; a rule whose words stayed and whose
  behaviour changed would still pass. Two tests really do exercise the mechanism with real
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

### The round before

**The cancellation ledger was read before the interrupted upload had settled.** The test
that asks whether a stopped step accounts for what it left behind held the *second* upload
open for twelve seconds, signalled the container, and then read the store immediately. But
this store — like a real one — has the whole body in hand before it answers: the object is
committed and the acknowledgement goes to a socket nobody is reading any more. So the
reading taken at the instant the container died was **not** the state collection would
eventually see. Measured directly, with a container killed the moment its second upload
arrived:

```
ledger read the instant the container died : ['outputs/one.csv']
ledger once the store went quiet           : ['outputs/one.csv', 'outputs/two.csv']
objects that landed AFTER the old snapshot : ['outputs/two.csv']
```

A step that wrote a marker naming only the first object would have **passed** that test and
left the second one stranded — precisely the outcome the test exists to catch, because
salvage publishes exactly what a marker names and nothing else. Every request is now on an
in-flight ledger, `Endpoint.settle()` blocks until it is empty, and the cancellation test
reads the store only afterwards. Neither remedy is prescribed: a step may drain what it
started and inventory it, or ensure nothing it did not account for is left behind. (Naming
an object that turns out to be absent is safe on the platform's side — salvage is
"best-effort about OBJECTS" and drops one it cannot verify. An object nobody named is never
looked at at all.)

**An upload was authorized against the clock at the END of its body, not the start.** The
store stamped a POST's arrival after reading and parsing the whole multipart body, so a
credential that expired *while the bytes were still on the wire* refused an upload that had
begun inside its lifetime. That is the revocation model this harness spent the previous
round removing, surviving in the one place nobody looked. The self-test that was supposed
to cover it did not: it delayed the request with a hook, and hooks run *after* the body has
been read — it proved that a delay after arrival changes nothing, which was never in
question, and it carried a `basis_contract` citation about credentials expiring, which says
nothing about when a store authorizes. Arrival is now stamped before the first byte of the
body is read; the self-test sends a body in two halves across the expiry instant over a raw
socket; and it is labelled `basis_our_policy`, because **when a store authorizes is a fact
about S3 and no orchestrator source states it**. Reverting the stamp turns that test red, on
the assertion that the credential was still live when the request arrived.

### Two rounds before

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
landed is inventoried" compared an empty set against the marker: with a single input,
nothing had been recorded at the moment the step was signalled. It now uses two inputs and
holds the *second* upload, so one object is genuinely on the ledger.

The explanation written down at the time was **wrong**, and it is worth leaving the
correction here rather than quietly editing it: the note said the store "records an upload
when it answers, not when the bytes arrive". It does not. It records the upload once the
body has arrived and the policy allows it, and answers *afterwards* — which is what a real
store does, and which is exactly why the object still lands when the client has already
been killed. Believing the tidier version is what left the next round's bug in place for a
round.

**A citation could name a file and quote nothing.** Described above.

### Earlier rounds, for the record

A hung container was reported as exit 0; credentials were revoked rather than expired; the
store answered with the FIRST write of an object rather than the last; uploads were
accepted with most of the presigned POST form missing; and the local contract validator
disagreed with the real parser in four ways. All fixed, and the orchestrator's three
frozen golden documents are checked in under `conformance/fixtures/` so that "my
re-implementation agrees with the real one" is a measurement rather than a claim.
