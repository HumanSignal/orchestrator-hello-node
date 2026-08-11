# Conformance baseline — what this node does today

> ## Status: all twenty defects below are FIXED
>
> `node.py` was rewritten and the whole suite is green, in every mode:
>
> ```
> python -m pytest -q                  → 140 passed, 1 skipped
> python -m pytest -q --red-for-real   → 140 passed, 1 skipped
> LSPO_ORCHESTRATOR_SRC=… LSPO_ORCHESTRATOR_REF=origin/master python -m pytest -q
>                                      → 141 passed          (the skip is the citation check;
>                                                             with the sources present it runs)
> python verify_mutations.py           → 12/12 claims verified red against this suite
> ```
>
> **A skip other than that one now FAILS the run** (`tests/conftest.py`): a conditional test
> that skips has not run, and a guard that can disappear while the summary line stays green
> is the failure mode this repository keeps rediscovering.
>
> Those numbers were 128 for one release. Twelve tests were added since — eleven in
> `tests/test_cancellation.py` and one holding the harness's own stall instrument to
> account — and what they measure is in
> [the stop path was the right shape and inert](#the-round-after-the-stop-path-was-the-right-shape-and-inert)
> immediately below. Group counts today: `conforms_today` 50, `subject_is_platform` 67,
> `harness_self_test` 22, `expected_red_until_fixed` 0. Bases: `basis_contract` 82,
> `basis_reference_quality` 35, `basis_our_policy` 24.
>
> The first two lines are identical, which is the point: there is no `expected_red_until_fixed`
> test left, so CI mode and the true result cannot differ. All twenty tests that used to be
> red were moved into `conforms_today`, where a regression turns the run red immediately
> rather than being absorbed as an expected failure.
>
> **Everything below the next section — *The round after* — is the measurement that was taken
> BEFORE the repair, and it is kept deliberately.** It is the evidence for what each defect actually cost, and every one
> of them is a mistake a first version of a node makes — which makes it the most useful
> reading in this repository for somebody about to write one. Read it as history, not as a
> description of the file in this directory.
>
> Three changes were needed outside `node.py`, and each is a case of the harness having
> encoded the defect rather than the rule:
>
> * `test_a_refused_request_does_not_print_the_presigned_url` staged its expired credential
>   through the LEGACY variable. Once the node reads the variable the agent actually sets,
>   that setup hands it a perfectly good envelope and the run succeeds — so the test would
>   have asserted nothing. Staleness now arrives through the current variable.
> * `test_docker_really_drops_an_environment_variable_the_image_baked_in` proved its point
>   using `LSPO_CREDENTIALS`, which the `Dockerfile` only baked so that a node reading the
>   wrong variable would appear to work. That line is gone, so the test uses
>   `PYTHONUNBUFFERED`, which the image still sets for reasons of its own.
> * `test_several_inputs_are_all_copied` (green throughout) pinned the shape of output
>   names. It is why the fix for the two-ports-one-filename collision disambiguates only
>   the names that actually collide, instead of namespacing every output by its port.

## The round after: the stop path was the right shape, and inert

**The claim under test was that `node.py` does not handle a stop at all. It does** — and
had since the rewrite: a handler installed on SIGTERM and SIGINT, setting a flag and
nothing more; the flag checked between units of work; the inventory at module scope; a
marker written last, with `status: "cancelled"` and `exit_code: 20`, matching the code the
process returns. Every line of the shape `docs/AUTHORING.md` prescribes was there, and the
suite proved it. So the gap was somewhere else, and there were two of them.

### 1. The mechanism that made the flag readable did nothing

A flag cannot be read by a process parked in a socket call, and `node.py` knew that: it
kept a ledger of transfers in flight and its handler closed them. **Neither half worked**,
and both were measured directly rather than argued about:

| What the file did | What was measured |
|---|---|
| put the **response** on the ledger | a response exists only once the store has begun to answer, so during the wait that matters the ledger is empty |
| called **`close()`** on it | mid-read that raises `RuntimeError: reentrant call inside <_io.BufferedReader>` *inside the handler*, where `contextlib.suppress` swallows it, and the read then waits out its whole timeout |

So the real stop latency was the socket timeout, on both paths. Stopped during a held-open
download: **12.2 s**. During an upload: **10.2 s**. Against a store that never answers at
all — the case a stop actually has to survive — the read timeout is 25 s, and the container
took **25.2 s** to go, out of a nominal thirty that
[PROTOCOL.md](docs/PROTOCOL.md#7-cancellation) is explicit nobody is promised.

The repair is on the **connection**, registered when its socket is created — before a byte
of a request is sent — and the handler calls `shutdown(SHUT_RDWR)`, which makes the pending
call return at once. Both schemes are covered: plain HTTP is what the harness and a local
demo exercise, TLS is what every presigned URL in production uses, and a fix that covered
only the first would have been invisible where it matters. Measured after: **0.22 s** and
**0.24 s** on the same two scenarios. One thing is deliberately exempt — once the marker is
being written, nothing may abandon it: there is nothing left to rescue by cutting that
connection and a whole run's account of itself to lose.

That is one further act inside a signal handler, and `docs/AUTHORING.md` says a handler
sets a flag and does no work "and especially network work". **The ambiguity is real and it
is resolved rather than ignored**: a socket shutdown starts nothing, waits for nothing and
cannot block, so it is not work in the sense the rule is about — and the reason the rule
gives (that the cancellation path itself crashes) is why it is the last thing in the
handler and its failure is ignored. Nothing that *decides* anything moved into the handler.
The reasoning is in the code, at `_on_stop`.

### 2. The harness never asked for the receipt

Four cancellation tests, and not one of them required a marker to exist. The helper they
share returns early when there is none — deliberately, because the contract permits
silence — with the consequence that **a step which exits the instant it is signalled,
writing nothing at all, passed every one of them**. Prompt exit was measured; the account
of the run was not, and the account is the only thing salvage can publish from.

That was proved rather than asserted: a copy of `node.py` that writes no marker on the stop
path still passes `test_a_step_stopped_during_a_download_does_not_claim_it_succeeded`,
`test_a_cancelled_step_stops_taking_on_new_work` and
`test_a_step_that_cannot_be_stopped_costs_the_whole_grace_period`. Only the fourth notices,
and only because that scenario has an object already on the ledger — stopped before it
produces anything, the old suite had nothing to say.

Four tests close it, all `conforms_today`:

| Test | What it forbids |
|---|---|
| `test_a_stopped_step_leaves_a_receipt_and_says_it_was_stopped` | being stopped and leaving no marker, or one that calls the ending anything but `cancelled` |
| `test_the_receipt_of_a_stopped_step_carries_the_code_the_process_returned` | the marker and the process telling two different stories, and an exit code that reads as "broken" rather than "stopped" |
| `test_a_stopped_step_claims_no_object_the_store_never_received` | a receipt that inventories an object nobody can find |
| `test_a_stop_is_noticed_without_waiting_for_the_transfer_it_landed_in` | a stop latency equal to the socket timeout, measured against a store that never answers |

### All four are `basis_reference_quality`, and none of them may be anything else

The temptation here is `basis_contract`, and it has to be refused for the sixth time.
`external/contract.py` does say *"A cancelled run still writes a marker: partial logs and
partial outputs are exactly what someone will want to look at afterwards"* — but it says it
while explaining what a field means, and everything the platform actually **does** with an
absent marker on this path treats it as an ordinary outcome:
`_salvage_what_the_step_produced` publishes *"what a FAILED or cancelled step managed to
write"* and returns an empty list when there is none, and the collector reports *"No
completion marker was written, so the step gave no account of itself and nothing it
produced could be salvaged"* as a finding, not a refusal. A marker is **required** only
after a reported success. Read literally, no rule makes any of these four behaviours a
violation, so labelling them as conformance would teach a preference as law — which is the
one mistake this document exists to keep correcting.

The same goes for exit 20. `agent/runner.py` `_classify` asks
`if context.cancel_requested.is_set() or exit_code == EXIT_CANCELLED` and answers
`'cancelled'` either way, so nothing requires the code. Read the other way round, that line
is the whole argument for using it: **20 is the only signal that says "stopped" rather than
"broken" when the platform was not the party that asked.**

### Liveness: one table, and it is executable

Every row below was re-run against the suite **as it stands in this commit** by
`verify_mutations.py`, which patches the file, runs the named test, records whether it went
red and on which assertion, and restores. It exits non-zero if any claim is unsupported.

```
python verify_mutations.py     →  12/12 claims verified red against the current suite
```

| Mutation | The test that reds, and on what |
|---|---|
| the SIGTERM handler is never installed | the receipt test — *"its receipt says 'succeeded'"*; and the promptness test — *"took 25.4s to go"* |
| `shutdown()` put back to `close()` | promptness — *"took 25.4s to go after being asked to stop, with a store that was never going to answer"* |
| the marker claims `outputs/ghost.csv`, never written | the over-claim test names the ghost |
| no marker on the stop path | the receipt test — *"wrote no completion marker: the store holds []"*; the other two **skip**, naming the legal ending |
| the ledger forgets a `will_close` connection after its headers | the mid-body test — *"took 25.9s while reading a body that had stopped arriving"* |
| one elapsed deadline replaced by the standard library's per-address spend | the multi-address test — *"spent 31.4s failing to reach a host with 3 addresses"* |
| the name lookup left unbounded | the lookup test — *"spent 43.1s on a name lookup that was never going to answer"* |
| the deadline not recomputed after a proxy's `CONNECT` | the proxy test — *"spent 20.4s getting a connection through a proxy that took seven of them to answer"* |
| the receipt corrected by a second document | the one-receipt test — *"wrote 2 completion markers for one run"* |
| the exit code revised after the document was written | the same test — *"the one receipt says the step exited 0 and it returned 1"* |
| the receipt's elapsed deadline removed | the drip test — *"spent 42.9s on a receipt whose answer was dribbled out over forty"* |
| the stalling listener announcing on `accept()` | the harness self-test — *"announced a stall before the client had said anything"* |

### Why that script exists: this document once claimed three guards that were not here

The multi-address, name-lookup and proxy tests were written, measured red under their
mutations, and reported — and then an edit that replaced a **slice of the test file between
two anchors** deleted all three while adding two others. The claims stayed. For one commit
this document described a suite that did not exist, and the three helpers those tests used
sat in the harness with no callers at all: replacing the elapsed deadline with per-address
timeouts, removing the bounded lookup, or deleting the recomputation after `CONNECT` would
every one of them have stayed green.

Nothing caught it because **a claim about a test is prose, and prose is not executable**.
`verify_mutations.py` makes it executable, and two things it found on its own first runs are
worth keeping:

* a mutation must be patched onto **the path the behaviour would really take** — the
  "correct the receipt with a second document" patch first landed on the success path, where
  in that scenario the first write has already timed out, so it never executed and reported
  a green that said nothing;
* a patch target that appears **twice** is now refused rather than resolved to the first
  match, because the second version of that same mutation landed in the work-failure branch,
  where its condition is dead — again green, again meaningless.

The rule that follows, and it is a rule about evidence rather than about code: **never
report mutation evidence for a test that is not in the committed suite at the moment of
reporting**. The script is how that is checked rather than remembered.

**What is still not measured.** That any of this is *collected*. A local SIGTERM models the
node and nothing else: on the runtime-deadline path the terminal report is refused and the
marker is never read (the lease is clamped to the deadline, so `complete()` answers
`lease_lost`), and an operator's Cancel usually arrives as a SIGKILL. The receipt is
written for the runs where it is read, and for the day those gaps close. Nothing in this
repository can test that half.

### The review of that round: the same defect, twice more, in the same file

The repair above was shipped for review and came back with the finding that matters most
here — **it had the defect it was fixing, one layer up, twice.** "The object was not on the
ledger during the wait that matters" was fixed for exactly the wait that had been measured,
and there are four waits on this path, reached through different objects:

| The wait | What it was doing | What it does now |
|---|---|---|
| getting a connection: DNS, TCP, **TLS handshake** | governed by the 25-second transfer timeout, and unreachable — `ssl` detaches the plain socket while wrapping it, so shutting that down raises `OSError: [Errno 9] Bad file descriptor` and the handshake runs to the timeout regardless (measured) | **bounded** by its own three-second budget, with the flag re-checked the moment the call returns so a stop that arrived during it does not go on to start a request |
| waiting for the store to begin answering | fixed last round | unchanged |
| **reading the body** | the ledger was empty: `http.client` closes the connection as soon as it has parsed the headers of a `Connection: close` response — which urllib sets on every request — and the `close()` override took the entry off | the override is gone; one request is in flight at a time, so the entry is simply replaced by the next connection |
| waiting for an upload to be acknowledged | fixed last round | unchanged |

Measured on the shipped-and-reviewed version, both with a listener that accepts and then
says nothing: a stop during a stalled **TLS handshake** cost **25.2 s**, and a stop while a
**body had stopped arriving** cost **24.7 s** — the same 25-second timeout, twice, in the
release whose whole subject was not paying it. After: **3.2 s** (bounded, not cut) and
**0.3 s**.

**The TLS half also closed a hole in what this suite can see at all.** Every presigned URL
in production is https and this harness's store is plain http, so a stop mechanism proved
only over http was a claim about the wrong protocol. It needed no certificate authority and
no `openssl`: a handshake stalls before any certificate is offered, so a listener that
accepts and refuses to speak is enough. `conformance/stalling.py` is that listener, and it
is also what produces the mid-body case, which the store's hooks cannot — they fire while a
request is still being authorised, which is before a byte of the response exists.

### And the exception for a second stop was swallowing the first

The same round introduced "nothing may abandon the receipt", and applied it to **every**
marker. A stop landing while a marker claiming SUCCESS was in flight therefore changed
nothing at all: the flag was set, the upload was left alone, it landed, and the step
returned **0** — a document saying `succeeded` inside a launch the orchestrator records as
cancelled, which is what `_step_account` renders for a human. Deterministic, not a race,
and measured: exit `0`, receipts written `['succeeded']`.

The protection is now asymmetric, and the asymmetry is the argument that was made for it in
the first place. A receipt reporting a failure or a stop cannot be made worse by being cut
short, and it is the run's only account: protected. A receipt claiming success is the one
document a stop can turn into a lie: **not** protected — the handler cuts it, and the run
writes the cancellation it has become. The flag is checked again after that write, before
0 is returned, for the case where the receipt lands anyway.

### The round after that: a retraction, and the same lesson a third time

**The asymmetric protection argued for above is withdrawn.** It said: protect a receipt
reporting a failure or a stop, but leave one claiming SUCCESS abandonable, because that is
the document a stop can turn into a lie. The premise was right and the remedy was wrong.
Abandoning the upload does not prevent the lie — **it makes which document survives
unknowable.** Once the store has the whole body it may commit it, and cutting the socket
revokes nothing, so the cancellation written next is a second write to the same key that
can overlap the first. Neither this step nor object storage defines which of two
overlapping writes wins. The harness demonstrated it directly: its delay hook holds the
first commit back, the cancellation lands first, and the receipt the store serves is the
one saying `succeeded` — with the process exiting 20 beside it, which is worse than the
defect being fixed, because now the two accounts disagree as well.

The test written for it made the same mistake one level up: **it looked away from the value
the store serves**, and said so in its own docstring, blaming the harness's hook. That is
the tell. A test that must avert its eyes from the property that matters is reporting a
design problem, not a harness problem.

What replaces it: the receipt is protected whatever it says, the check happens **after**
that write, and the correction is a second write that begins only once the first has
finished. The writes are sequential, so the survivor is knowable, and the test now asserts
exactly it. This also makes the defence observable — the mutation that removes the check
was **green** last round, because the abandonment covered for it; it is red now, because
the check is the only thing standing there.

### And the wait that was "bounded" was not bounded

The three-second connect budget in that round was not an elapsed deadline, and could not
have been: `socket.create_connection` resolves the name **before there is a socket to apply
a timeout to**, and then applies the value **separately to each address it got back**.
Measured, in a container:

| Stimulus | Behaviour |
|---|---|
| one silently-dropped address, `timeout=4` | 4.0 s |
| the same name on **three** such addresses, `timeout=4` | **12.0 s** — the number the caller passed, spent three times |
| a name lookup against a resolver that receives every query and answers none | **40.6 s**, with the step's "budget" applying to none of it |

Both are now covered by one elapsed deadline (`_connect_within`, `_resolve_within`), and the
name lookup is bounded by handing it to a thread — the only thread in the file, and the
only way the standard library offers, since `getaddrinfo` takes no timeout at all. Measured
after: **10.4 s** and **10.5 s**.

**And the value moved from three seconds to ten, in the opposite direction from the fix.**
Three was chosen to make a test quick, and the cost of that is asymmetric in a way that is
easy to get backwards: a deadline firing on a healthy-but-slow connect **kills the whole
job**, because there is no automatic retry engine for external steps — every failed attempt
is recorded as transient whatever the process returns, and a person has to notice and retry
it by hand (`docs/PROTOCOL.md` section 6). A generous deadline costs, at worst, ten seconds
of a stop nobody was promised any of.

**Three stimuli were built and thrown away before one of these tests measured anything**,
and that is worth recording because each looked correct:

* an address in TEST-NET-3 — fails in **0.1 s**, nothing is routed there and the kernel
  says so;
* an unassigned address on the container's own bridge subnet — fails at **~3 s** whatever
  timeout is asked for, because the kernel gives up on the ARP on its own schedule;
* an unroutable *resolver* — the lookup fails in **0.4 s** for the same reason.

Each made its test pass against a node with no bound at all. What works is a peer that
receives and stays silent: a route that drops (`10.255.255.x`) and a resolver of our own in
a container (`docker.silent_resolver`), and the multi-address test now **checks that its
stimulus really hangs on this machine and skips if it does not**, rather than passing
quietly where the network refuses what it cannot route.

> Liveness for every round is in [one executable table](#liveness-one-table-and-it-is-executable)
> near the top of this document, re-run by `verify_mutations.py`. The per-round tables that
> used to sit in each section have been folded into it: they restated the same claims, and one
> of them described a mechanism that a later round removed.

### Round four: the correction could not be sequenced either, and the platform said so

The redesign above — let the receipt land, then correct it — was reviewed and **fails on
the same fact this file already documents about its own uploads**: a transport failure is
ambiguous, and a store may accept a body after the client that sent it has gone. So when
the success write times out and the cancellation is written next, the timed-out write can
commit **after** it. Reproduced with the harness, holding the receipt's upload for twelve
seconds against a step that gives up at ten: the cancellation lands first, the success
commits last, and the run ends with `succeeded` beside exit 20 — the exact state the
redesign existed to prevent. **Sequential calls are not sequential commits.**

So the design got smaller instead of gaining a third mechanism. **One receipt, or none:**
the flag is read once, before the document is composed; the write is protected; and if it
fails, nothing else is written to that name — the exit code and one log line are the whole
report, and the code is decided with the document so the two can never disagree.

### What made that safe was measured on the platform, not assumed

The question the design turns on is what the platform does with a `succeeded` marker beside
a launch it recorded as cancelled. It was read rather than guessed, at `origin/master`:

| Question | Answer, and where it is written |
|---|---|
| What decides the execution's outcome? | The orchestrator's own journal. `pipelines/external_finalize.py:254-260` maps `ExternalLaunchState` to the outcome and `:627-636` branches on it; the recovery path branches on `attempt.state` (`:819`, `:856`, `:863`). |
| Can a marker claim a success? | No — it can only **veto** one. `:1371-1375` refuses a runner-reported success when the marker disagrees. There is no inverse. |
| Are a stopped run's objects delivered? | No. Salvage attaches them with `role='logs'` and no `payload_kind` (`:3213-3216`, `:3242-3243`); even a cancellation racing a collection demotes what was already verified (`:2769-2770`). |
| Does anything cascade downstream? | No. `_run_the_cascade` sits behind the compare-and-set in `_complete_execution` (`:2645-2651`), and `_cancel_execution` never calls it. |
| What does the operator read? | Our sentence first, then the marker's **`exit_code` and `error`** — `status` is never rendered (`:2384-2394`). A stopped container that wrote `succeeded, 0` produces *"The external job was cancelled. The step exited with code 0."* |
| Does the agent's exit code override? | No. `agent/runner.py:2829-2843` tests fence, hard stop, `deadline_hit` and `cancel_requested` **before** `exit_code == 0`. |

**So the residual state is not a lie the platform can act on**, and a step that finished its
work and was interrupted while *reporting* it has genuinely succeeded — the stop arrived
late. That is what makes "write one document" sufficient rather than merely simpler.

### The receipt is the one transfer that needs a clock — and the clock is a guess

Nothing may abandon it, so nothing but elapsed time can end it — and `UPLOAD_TIMEOUT_S` is
not elapsed time. It bounds **silence**, so a peer that sends one byte per window holds the
transfer open indefinitely while never being idle. Measured with a store that dribbles the
receipt's answer out over forty seconds: **42.9 s** without a deadline. With one: **20 s**.

**The twenty is best-effort by construction, and an earlier version of this document
justified it from a grace that was never promised.** Read at the deployed commit: nothing
tells the container how long it has after a stop. Not the nine injected variables; not the
credentials envelope, whose `expires_at` is a signature's lifetime; not the job description,
whose `timeout_seconds` is a *requested* budget with no start time attached; and not the
stop object the orchestrator composes on its heartbeat, which reaches the agent and stops
there. Thirty seconds is what today's build passes to `docker stop` at every call site — an
observation, not a guarantee, and the value is the agent's to choose. Against a remaining
grace of zero no positive deadline can be honoured, and the alarm may be killed before it
can log that it fired. What the number buys is a shape: long enough for a receipt to land on
a store that is working, short enough that a step which will not land one stops trying while
there may still be time to say so. Exposing the real remaining deadline to the workload is
an open platform task, and it is the thing that would make this exact.

### The deadline still leaked through a proxy

`_connect_within` computes the remainder through the lookup and the TCP connect and stores
it once, as the socket's timeout — and `HTTPSConnection.connect` then spends it *again* on
the TLS handshake behind a proxy's `CONNECT`. Measured against a proxy that grants the
tunnel after seven seconds and then goes silent: **17.6 s** for a ten-second deadline. The
remainder is recomputed after the tunnel now: **10.5 s**.

### A guard that could vanish, and a premise that proved the wrong thing

The multi-address test's premise check probed **one** address for two seconds and accepted
anything over 1.5 — which an address failing at the kernel's own ~3 s ARP give-up passes,
while three of those cost ~9 s under the broken implementation, comfortably inside the
test's own threshold. The guard could therefore have gone green against exactly what it
exists to catch. It now probes the whole name and requires the timeout to have been spent
once per address, which is the property the test depends on.

And a skip no longer passes quietly: `tests/conftest.py` fails any run containing a skip
other than the verbatim-citation check, and names it. A conditional test that skips has not
run, and a guard that can disappear while the run stays green is the failure mode this
repository keeps rediscovering.

---

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

### The uid instruction that was false — the check earning itself a second time

The same check fired again, on the worst thing a public repository can be wrong about: an
instruction telling authors how to *build* their image, where following it is what hurts
them. This repository said, in nine places, that a job's credentials arrive as a `0600`
file in a `0700` directory owned by the agent's uid, and that a customer's image therefore
had to run as **uid 10001**. Every part of that had stopped being true.

* The credentials **file** is `0444` and its directory `0711`, re-applied on every write
  (orchestrator PR #250). Confidentiality comes from an ancestor directory nobody else can
  traverse, not from the leaf's mode — precisely so that an image running as any uid can
  open its own credentials.
* The **agent** now runs as the operator's own account, not as the account its image
  declares (orchestrator PR #268, `--user "$(id -u):$(id -g)"`). So "the agent is 10001"
  was not what a deployed agent gave you either.

The two errors compounded in the nastiest possible way. A half-correction that fixed only
the second — "the agent runs as the invoking user, so build as uid 1000" — would have been
*worse* than the original text, because it keeps the false `0700`/`0600` premise alive and
sends the author chasing a number that changes per machine. The premise had to go first.

What the citation check actually caught was one stale quotation: the old permissions test
cited *"The directory is created 0700 and the file 0600 — on a shared machine the credential
must not be readable by other users"*, and `agent/creds.py` no longer contains that
sentence. One red assertion, on one test, was the only thread that led to nine wrong
statements across the documents, the Dockerfile and the harness's own constants — none of
which any test would have contradicted, because they were prose.

The test it guarded has been replaced rather than deleted, and the replacement is stronger
than the original: instead of asserting a coupling, it now measures that the coupling is
**gone**, with real containers and four different users. See
`test_a_workload_running_as_any_uid_can_read_its_own_credentials`.

**Its liveness was measured, not assumed**, because a test that says "everything is
readable" is exactly the shape that passes when nothing is being checked. Four mutations,
each run against real containers, and each has to fail for the *right* reason. (These
mutate the harness's own permission constants rather than the node, so they are not in
`verify_mutations.py`; what has been checked is that the test they name is still in the
suite — `tests/test_platform_rules.py` — because a table of mutations against a test that
no longer exists is exactly the failure recorded at the top of this document.)

| Mutation | Result |
|---|---|
| the credential file narrowed back to `0600` | red — *"a 0600 file in a 0711 directory owned by uid 1000 was NOT readable as the image's own user"* |
| the directory narrowed back to `0700` | red — the same, naming `0700` |
| the directory widened to `0755`, so it can be listed | red — *"a 0755 directory was listable by a uid that does not own it"* |
| the mode re-applied at job start but **not** on the refreshed inode | red — *"the replaced credential file was not readable by an arbitrary uid"* |

And two negative controls, which must stay **green**, because the whole claim is that the
image's own uid is nobody's business: rebuilding this repository's image as uid `10001`
(the agent image's account) and as uid `1000` (a typical host operator) both pass.

The first draft of the test failed that battery in the most instructive way. It opened with
`assert CREDENTIALS_FILE_MODE == 0o444` — and narrowing the constant then failed on *that*
line, comparing a literal in this repository against a constant in this repository, with
the container never starting. A guard that marks its own homework. Removing it is what
turned the mutations into the four honest failures above. The lesson is the same one this
section already teaches, sharpened: a quotation is the only part of a document that can be
mechanically held to its source, so the rules worth quoting are the ones a reader will act
on — and a check must compare itself against something it does not also own.

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
  agent produced. The harness now applies the agent's real modes everywhere — `0711` on
  the directory, `0444` on the file — rather than the looser 0755/0644 it used while
  those modes were still 0700/0600 and would have locked the harness out of its own
  fixture. That change is worth more than tidiness: the directory belongs to whoever ran
  pytest and the container runs as somebody else, so every container test in the suite
  now reads its credentials through the same permission class a customer's image uses.
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
  containers: the bind-mounted-file test and
  `test_a_workload_running_as_any_uid_can_read_its_own_credentials`, which reads a job's
  credentials from inside the image as three users — the image's own, a uid in no passwd
  file, and the directory's owner — then once more as that stranger uid across a
  credential refresh, and finally confirms it still cannot LIST the directory. The citation check
  fired a second time on exactly this area: the platform had widened those modes so that a
  node image may run as any user, and this repository was still instructing authors to
  build as uid 10001 (see "The uid instruction that was false").
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
