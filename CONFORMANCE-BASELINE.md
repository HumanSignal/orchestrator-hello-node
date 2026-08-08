# Conformance baseline — what this node does today

Measured, not reasoned about. Every line below is the observed behaviour of the image
built from this repository's own `Dockerfile`, run as a container against the harness in
`conformance/`, on Linux with Docker 28.4.

```
python -m pytest --red-for-real -q
→ 23 failed, 74 passed in 106s
```

The 23 failures are exactly the 23 tests marked `expected_red_until_fixed`. Nothing else
fails. In the default mode (`python -m pytest`) those 23 run as strict expected failures,
so CI is green today — and turns red the moment one of them is fixed, which is the prompt
to move that test into the `conforms_today` group.

| Group | Tests | Today |
|---|---|---|
| `expected_red_until_fixed` | 23 | fail |
| `conforms_today` | 16 | pass |
| `subject_is_platform` | 50 | pass |
| `harness_self_test` | 9 | pass |

---

## First, the thing that surprised me

**The node works today only by coincidence, and there are three coincidences holding it up.**

The orchestrator's agent injects nine variables, and the one naming the credentials file
is `LSPO_CREDENTIALS_FILE`. This node reads `LSPO_CREDENTIALS`, which the agent never
sets. It nevertheless runs, because:

1. its own `Dockerfile` contains `ENV LSPO_CREDENTIALS=/lspo/creds/creds.json` — the
   variable it reads is set by the image itself; and
2. the orchestrator currently always issues the *default* credentials path
   (`runners/credentials.py` hardcodes `DEFAULT_CREDENTIALS_FILE` into every envelope),
   so the value the image baked happens to be the right one.

Both sides of the contract already support a different path — the envelope and the
manifest each carry `credentials_file`, and the agent mounts the directory that path
lives in — so the first deployment that uses one, or the first rebuild of this image
without that `ENV` line, breaks the node completely. That is why the harness tests it as
a real defect rather than a latent one, and why one test in the group is a *regression
guard*: whoever makes the node read the new variable must keep honouring the old one,
because images in the field bake it.

### A third coincidence, on the platform's side

The agent writes each job's credentials directory **0700** and the file **0600**, owned by
its own user, and in S3 mode it deliberately does not override the workload image's user
(`agent/runner.py` sets `run_as` only in local demo mode). So a workload can read its own
credentials only if it runs as the same uid as the agent, or as root.

It works today because both are uid 10001 — the agent's `Dockerfile.agent` creates user
`lspo` with `--uid 10001`, and this node's `Dockerfile` creates user `step` with
`--uid 10001`. Nothing connects those two numbers; they are the same by accident.

Measured, using this repository's image and a 0700 directory owned by a different user:

```
PermissionError: [Errno 13] Permission denied: '/lspo/creds/creds.json'
```

The same directory read from a container forced to the owner's uid returns the file
normally. A customer who writes their own Dockerfile — which is exactly what this
repository tells them to do — and picks any other non-root user gets that
`PermissionError` on their own credentials, with nothing in the contract documentation
warning them, and the only obvious workaround being to run as root. This is a platform
finding, not a node defect, and it is written down as an executable rule in
`tests/test_platform_rules.py`.

The Dockerfile's own comment is also wrong about the mechanism: *"The agent mounts the
credentials file and sets LSPO_CREDENTIALS to its path."* The agent mounts the **directory**
(deliberately — the file is atomically replaced and a file bind-mount pins the old inode)
and sets `LSPO_CREDENTIALS_FILE`.

---

## The twelve defects, measured

### 1. It reads the wrong environment variable

`node.py:174` — `os.environ.get('LSPO_CREDENTIALS')`. Nothing on the orchestrator's side
sets that name.

| Test | Observed |
|---|---|
| `test_the_credentials_file_variable_is_honoured` | Credentials mounted at `/lspo/creds/envelope.json` and named by `LSPO_CREDENTIALS_FILE`. The step ignored it, opened the baked-in path and died: `FileNotFoundError: [Errno 2] No such file or directory: '/lspo/creds/creds.json'`. Exit 1, no marker. |
| `test_with_no_variable_at_all_the_default_path_is_used` | With both names genuinely absent: `StepError: LSPO_CREDENTIALS is not set; the agent did not mount a credentials file`. Exit 1. The contract's default path is never tried. |
| `test_when_both_variables_are_set_the_current_one_wins` | Legacy name → a superseded envelope, current name → the live one. The step used the superseded one: `403 Client Error: Forbidden for url: …/invocation.json?tok=gen-1`. Exit 1. |
| `test_a_disagreement_between_the_two_variables_is_reported` | Neither variable name appears anywhere in the output. An operator mid-migration gets a 403 and no way to connect it to the two variables that disagreed at startup. |
| `test_a_refused_request_does_not_print_the_presigned_url` | `credential material reached the log: ['?tok=']`. `requests` puts the full URL into the message of the error it raises for a bad status, and a presigned URL **is** the credential. The step lets that message through to stderr, where it is stored with the execution and searchable. |

### 2. Credentials are read once and never reloaded

`node.py:260` loads the envelope into a local variable and every later transfer uses that
copy. Envelopes live fifteen minutes by default, clamped to whatever remains of the job's
runtime budget, and the agent rewrites `creds.json` in place before expiry. So this is a
hard ceiling on the node's own runtime.

| Test | Observed |
|---|---|
| `test_credentials_that_rotate_before_the_first_upload_are_picked_up` | `upload of 'outputs/three.csv' was refused with HTTP 403 … the credential 'gen-1' … has been superseded`. Exit **10 (permanent)** — so the orchestrator does not even retry. |
| `test_credentials_are_reloaded_before_every_transfer_not_merely_once` | Same, and the failure marker could not be written either: `could not write the failure marker: … refused with HTTP 403`. The run leaves nothing behind at all. |
| `test_credentials_that_rotate_between_two_input_reads_are_picked_up` | A rotation between two reads is equally fatal. |
| `test_a_credential_that_expires_mid_operation_is_retried` | The store accepted the upload body, rotated, then answered 403. The step reported permanent failure instead of reloading and repeating. |

The second of those is the important one: it is the test a "reload once, after processing"
fix would still fail. The rule is *reload before every transfer*, because the file can be
replaced at any moment, including between two writes.

### 3. A failed run reports an empty inventory, so partial output is abandoned

`node.py:271` — `write_marker(envelope, manifest, [], status='failed', …)`. The literal
empty list is the whole defect.

`test_what_a_failed_run_already_produced_is_still_salvageable`:

> `['outputs/one.csv'] were uploaded and then abandoned: the failure marker inventories nothing, so salvage will publish none of them`

The collector's salvage path (`pipelines/external_finalize.py` → `_salvage_what_the_step_produced`)
copies exactly what the marker's `objects` list names. An empty inventory therefore does
not mean "nothing was produced" — it means "everything produced is stranded in a staging
directory nobody will look at again". On a wide batch that is hours of work.

### 4. SIGTERM does nothing at all — the node cannot be cancelled

This is not what I expected to measure, and the difference matters.

The step is **PID 1** in its own container. The kernel does not apply a signal's default
action to PID 1: it delivers the signal only if a handler is installed. `node.py` installs
none, so SIGTERM is dropped on the floor and the process carries on working. Verified
directly: the same image with a one-line handler exits immediately; without one it
survives SIGTERM and keeps running.

| Test | Observed |
|---|---|
| `test_sigterm_during_a_download_leaves_a_cancelled_marker` | `the step was asked to stop and its marker says 'succeeded' — the request changed nothing`. Exit 0. A run somebody cancelled is recorded as a success. |
| `test_sigterm_during_an_upload_leaves_a_cancelled_marker` | Same. |
| `test_a_cancelled_step_stops_taking_on_new_work` | `after being asked to stop, the step went on to fetch 3 of 3 inputs; it exited 0`. |
| `test_a_step_that_cannot_be_stopped_costs_the_whole_grace_period` | `docker had to wait the full 30s grace (30.3s measured) and then SIGKILL it. It exited 137, which classifies as 'transient'`. |

Two costs, neither visible from inside the step: a runner slot is held for thirty seconds
per cancelled job, and exit 137 is not one of the five codes the contract knows, so it
classifies as **transient** and the orchestrator schedules a retry of work a human
explicitly cancelled.

### 5. Any non-2xx is fatal; nothing transient is retried

`_http_get` calls `raise_for_status()` once; `_post_object` raises the step's own
permanent-failure type for every status outside 200/201/204.

| Test | Observed |
|---|---|
| `test_a_single_transient_refusal_is_retried` | One 503 on the first read ends the run: `503 Server Error: Service Unavailable for url: …`. Exit 1. |
| `test_a_transiently_refused_upload_is_retried` | One 503 on the first upload: `upload of 'outputs/data.csv' was refused with HTTP 503` — reported as **permanent** (exit 10), so a momentary refusal tells the orchestrator never to retry. |

Object storage answers 503 (`SlowDown`) under load and 500 on its own faults; both are
documented as retryable. The orchestrator's own retry re-does the *entire* step rather
than the one request that stumbled.

### 6. The marker's exit code contradicts the process's

`node.py:236` — `'exit_code': EXIT_OK if status == 'succeeded' else EXIT_PERMANENT`, while
`main()` returns 1 for anything that is not the step's own `StepError`.

`test_a_persistent_transient_fault_is_reported_as_transient_in_both_places`:

> `the marker says the step exited 10, the process exited 1`

The orchestrator reads the exit code for its retry decision and the marker for its report,
so the job is retried while the record attached to it says it never should be.

### 7. A failure before the manifest writes no marker at all

`load_envelope()` and `read_manifest()` are called *outside* the `try` that turns failures
into markers — despite `main()`'s docstring saying "Never raises: every failure becomes a
marker plus an exit code".

`test_a_failure_before_the_manifest_is_read_still_writes_a_marker`:

> `the step died before its manifest and wrote no marker at all; the store saw nothing`

The identity fields needed for that marker do not require the manifest: `LSPO_EXECUTION_ID`,
`LSPO_ATTEMPT`, `LSPO_GENERATION` and `LSPO_IDEMPOTENCY_KEY` are four of the nine injected
variables, and this is what they are for.

### 8. Two ports carrying the same filename collapse onto one object

`node.py:198` derives the output path from the input's `relpath` alone and ignores the
`port` the envelope carries beside it.

`test_two_ports_delivering_the_same_name_do_not_collapse`:

> `duplicate relpath(s) ['outputs/data.csv'] in the objects inventory`

Two things go wrong at once. The second upload silently overwrites the first, so one
input's bytes are gone; and the marker inventories one path twice with two different
hashes, which the collector refuses at parse time — so a run that looked successful
publishes **nothing at all**.

### 9. Whole objects are held in memory

`response.content` for the read, the same bytes again for the hash, and a third copy
inside the HTTP client's multipart body for the write.

`test_an_input_larger_than_the_container_is_not_fatal`:

> `the container was OOM-killed reading a 128 MiB input into 64m of memory; it exited 137, which classifies as 'transient' — so this is retried forever`

Input size is chosen by the pipeline; the memory ceiling is chosen by the operator running
the agent. A step that streams has a memory profile that does not depend on its input.

### 10. `result.json` is delivered as if it were data

`test_contract_documents_are_not_delivered_as_output`:

> `result.json is delivered on a port: {'output': ['outputs/data.csv', 'result.json']}`

`produced_ports` is what feeds the next step in the pipeline. An object claimed by no port
is explicitly normal in the contract, and that is what a contract document should be.

### 11. Nothing bounds `result.json`

`test_the_result_document_stays_under_its_ceiling`:

> `result.json is 3145907 bytes, over the 1048576-byte ceiling`

The step copies `params` into its summary without looking at their size, so a manifest the
orchestrator was happy to write (3 MiB — well inside the 8 MiB manifest ceiling) produces a
result document the orchestrator will refuse to read. The failure surfaces at collection,
on the far side of all the real work.

### 12. It reports no progress

`test_the_step_reports_progress`:

> `the step reported no progress at all; it said: INFO execution 4242 attempt 1 / INFO done — 4 file(s), 1600 bytes`

Progress is opt-in and out of band: a step writes `@lspo:progress {"fraction": 0.4, "phase": "copying"}`
and the agent consumes that line and reports it on the next heartbeat. The agent
deliberately does not invent a fraction from elapsed time, so a step that never writes one
has no progress at all — and a long batch is indistinguishable from a hung one for its
entire duration.

---

## What the node already gets right

Sixteen `conforms_today` tests. These are regression guards, asserted now so that fixing
the twelve above cannot quietly break them:

* the completion marker is the **last** object uploaded, and everything it inventories
  arrived strictly before it;
* the marker carries all four identity fields, and every inventoried hash and size matches
  the bytes the store actually received;
* inputs are verified against their pin — changed bytes, a wrong size and a missing
  `sha256` are all refused, permanently, with the object named;
* a permanent failure still writes a `failed` marker with a reason;
* zero inputs, several inputs across two ports, nested relpaths, and names containing `%`,
  spaces and non-Latin characters all round-trip byte for byte;
* `params` reach the step unchanged, including nesting, floats, booleans, nulls and
  non-ASCII text;
* unknown additive fields in the envelope, the manifest and each input entry are ignored,
  not rejected;
* an ordinary successful run prints no credential material;
* the legacy credentials variable, used alone, still works.

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
* **The agent's real credential-file permissions, end to end.** The interaction is
  measured and written down above and in `tests/test_platform_rules.py`, but with a
  synthetic directory rather than one a running agent produced. The harness itself
  deliberately uses 0755/0644 for every other test, so that a permission problem can never
  be mistaken for a node defect.
* **The environment allowlist end-to-end.** The agent refuses a manifest that names a
  variable outside `LSPO_AGENT_ALLOWED_ENV` *before the container starts*, so no
  black-box test of the node can observe it. The rule is written down and executable
  (`conformance/platform_rules.py`) and tested as a platform rule.
* **Generation fencing.** That a superseded runner physically cannot write into the live
  attempt's directory is a property of the staging prefix and the upload policy, not of
  the node. The harness proves the fence exists (a key outside the prefix is refused with
  403) but not the orchestrator's half.
* **Exit code 30 (contention).** Nothing in this node can produce it.
* **What the *right* output-port mapping is.** The node hardcodes a single port called
  `output`. The harness asserts only the unambiguous part — that two distinct inputs must
  not collapse onto one relpath, and that every delivered relpath is inventoried. Which
  port an object belongs on when the input arrived on a named port is a design decision
  for the fix slice, and the harness deliberately does not pre-empt it.
* **`logs.ndjsonl`.** The contract names the file and the collector's comments describe it
  as something a step is *invited* to write, not required to. The node never writes one.
  Recorded as an observation rather than tested as a defect, because "invited" is not a
  rule a conformance test can hold anyone to.
* **Real S3.** No AWS signature verification, no request-time skew, no chunked uploads, no
  versioning, no eventual consistency. None of them change what the node must do, and the
  fake endpoint is deliberately *stricter* than the real one in the three ways that matter
  (see `conformance/fakes3.py`).
