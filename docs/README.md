# Writing an external node

**BEHAVIOUR, and it is the whole shape of the thing.** An **external node** is a pipeline
step that runs as **your container, on your machine**. The orchestrator never sees your
code. It sees an image digest, writes a job description and short-lived credentials where
your container can read them, starts the container through an agent process you run, and
then collects whatever the container says it produced.

These four documents are everything you need to write one correctly. They are written for
someone (or something) that has only this repository and cannot read the orchestrator's
source, so **every value that matters appears here literally**. Source paths are given as
provenance, after the value, never instead of it.

---

## How to read this: three kinds of statement

This whole section is **background**: it is about how to read these documents, and imposes
nothing on your node. Every normative sentence in them is labelled, and the labels are not
decoration. Treating a recommendation as a rule produces code that is wrong in a different
direction, which is exactly as expensive as breaking a rule.

| Label | What it means |
|---|---|
| **RULE** | Break it and the platform refuses the job, or fails the run. There is a **specific check** in the platform's code, and the text names it. |
| **BEHAVIOUR** | What the platform does. Not a duty on you, but your code has to survive it. |
| **RECOMMENDATION** | What a well-written node does, and why. The platform permits the alternative. |

**How far a label reaches.** A label opens a **statement**, and it governs everything that
belongs to that statement — its own paragraph, and any list or table that continues it —
until the next label or the next heading. So a table introduced by "**RULE.** …" is a table
of rules, and its rows do not each repeat the word. This is what makes the next sentence
checkable rather than aspirational.

**If a statement carries no label, it is background, not a requirement.** In this set that
is only ever true of two things: prose about *these documents* (what was verified, how to
read them, where to go next), and prose about *this repository's example files*. Every
statement about the **platform** — what it does, what it refuses, what a good node does
about it — carries a label. That claim was checked mechanically over all five documents
rather than by eye; see [Provenance](#provenance).

One kind of statement needed a phrase of its own. A few are marked
"**RECOMMENDATION**, with no working alternative": no check refuses you, and the
alternative still cannot work, most often because you would be holding no credentials for
what you were trying to reach. They are kept out of the RULE column deliberately, because a
check you can name and a consequence you can reason about are different things and mixing
them is how a preference gets enforced as a rule.

### A recommendation is not a lesser thing

The single most important instruction in this document set — **write the completion marker
last** — is a RECOMMENDATION. So is verifying your inputs against their hashes, and so is
re-reading your credentials on a long run. Each of them decides whether a real production
run works, and not one of them is checked anywhere.

That is the whole reason for labelling. The label does not rank importance; it answers
"what will tell me I got this wrong, and when?" A broken RULE produces a refusal that
names itself. A broken RECOMMENDATION produces a run that fails somewhere else, later,
saying something unrelated — or, worse, a run that quietly succeeds with the wrong data.

**A related trap, since it cost this document set a correction.** A normative-sounding
instruction in a source file is not an enforced rule. The orchestrator's own contract
module states, in bold, that the marker is written "strictly last" — and there is no check
behind that sentence, because no component ever observes the order in which a container
wrote things. Reading it as a rule and writing it down as one made this document set
contradict itself. When you are deciding what your node must do, "the code says to" and
"the platform will stop me" are two different facts.

---

## Routing table

Pointers only — this table is **background**, and every statement it points at carries its
own label.

| I want to know | Go to |
|---|---|
| Which environment variables my container gets | [PROTOCOL.md](PROTOCOL.md#1-bootstrap-what-the-container-starts-with) |
| Where my credentials are and what is in them | [PROTOCOL.md](PROTOCOL.md#12-the-credentials-envelope) |
| How to read the job description (`invocation.json`) | [PROTOCOL.md](PROTOCOL.md#2-inputs) |
| Every field of the job description, with its type | [PROTOCOL.md](PROTOCOL.md#22-invocationjson-field-by-field) |
| How to read my input files, and what I must check | [PROTOCOL.md](PROTOCOL.md#23-reading-the-input-objects) |
| How long my container is actually allowed to run | [PROTOCOL.md](PROTOCOL.md#24-how-long-you-actually-get) |
| Where to write my output files | [PROTOCOL.md](PROTOCOL.md#4-outputs) |
| Every field of the completion marker | [PROTOCOL.md](PROTOCOL.md#5-the-completion-marker) |
| What happens if my credentials expire mid-run | [PROTOCOL.md](PROTOCOL.md#43-credentials-expire-during-your-run) |
| Which exit code to return | [PROTOCOL.md](PROTOCOL.md#6-exit-codes) |
| What happens when someone presses Cancel, and why it is usually not a polite stop | [PROTOCOL.md](PROTOCOL.md#7-cancellation) |
| What happens when my step runs out of time, and why nothing it wrote is collected | [PROTOCOL.md](PROTOCOL.md#7-cancellation) |
| Why my container was killed with no warning at all | [PROTOCOL.md](PROTOCOL.md#71-fencing-the-stop-with-no-grace-period-at-all) |
| How my logs reach the run view, and what is lost | [PROTOCOL.md](PROTOCOL.md#33-logging) |
| How to report progress | [PROTOCOL.md](PROTOCOL.md#34-progress) |
| What the platform does after my container exits | [PROTOCOL.md](PROTOCOL.md#8-what-happens-after-you-exit) |
| A skeleton to copy, in the right shape | [AUTHORING.md](AUTHORING.md#the-skeleton) |
| The order to build things in | [AUTHORING.md](AUTHORING.md#the-recipe) |
| A checklist before I ship | [AUTHORING.md](AUTHORING.md#the-checklist) |
| How to test my node without an orchestrator | [CONFORMANCE.md](CONFORMANCE.md#level-1-run-it-with-a-hand-written-envelope) |
| What a conformance suite can and cannot prove | [CONFORMANCE.md](CONFORMANCE.md#what-testing-cannot-prove) |
| How to register my node and start the agent | [OPERATIONS.md](OPERATIONS.md#registering-a-node) |
| Which uid my image must use, and why | [OPERATIONS.md](OPERATIONS.md#the-uid-coupling) |
| Limits, quotas, and things that do not exist yet | [OPERATIONS.md](OPERATIONS.md#residual-limits-stated-plainly) |
| Why my run is stuck at "Waiting for runner" | [OPERATIONS.md](OPERATIONS.md#troubleshooting) |

---

## Shipped-capability matrix

**BEHAVIOUR, for the whole table below.** The contract models more than the orchestrator
currently uses. Writing against a modelled but unshipped capability produces a node that is
correct and never exercised, which is a worse failure than an obvious one because nothing
reports it.

| Capability | In the contract | Sent by the orchestrator today | Collected today |
|---|---|---|---|
| Multiple input ports | Yes, `inputs` is a list with unique names | **No.** Exactly one port, always named `input` | not applicable |
| Prefix (folder) input ports | Yes, `layout: "prefix"` with a tree digest | **No.** A folder artifact upstream is refused with a configuration error before the job is created | not applicable |
| Empty input set | Yes, `inputs` defaults to `[]` | **Yes.** A step with no upstream artifacts gets `"inputs": []` | not applicable |
| Multiple output ports | Yes, `produced_ports` is a name to relpath map | not applicable | **Yes.** Every port becomes a downstream artifact kind |
| Objects claimed by no port | Yes | not applicable | Copied and verified, but **not offered downstream** on a successful run |
| `result.json` as metrics | Yes, `ResultDoc` with `metrics` and `summary` | not applicable | **No. Nothing ever reads it.** Run metrics come from the marker inventory |
| `logs.ndjsonl` written by your step | Invited by the contract | not applicable | Published only if you also list it in the marker's `objects` |
| Progress reporting | Yes, one stdout line per sample | not applicable | **Yes**, shown live in the run view |
| Cancellation with exit code 20 | Yes | not applicable | **Only sometimes.** A `cancelled` marker is read and salvaged when your own report is what ends the job. When an **operator** cancels a step that is still running, nothing you wrote is collected — see [PROTOCOL.md](PROTOCOL.md#7-cancellation) |
| Automatic retry of a failed attempt | Exit code 1 means "retry me" | not applicable | **No.** Nothing re-attempts an external job automatically. An operator retries by hand |

---

## Provenance

Everything in this section is **background**: it is about these documents — where their
values came from and what was checked — and imposes nothing on your node.

Every value in these documents was read from the orchestrator's source at commit
**`6b2ff82c70f26d0ceaa1a841137f1b3cfb08186b`** (2026-08-08) and checked by hand. The files
read were `external/contract.py`, `external/io.py`, `external/versioning.py`,
`external/text.py`, `external/README.md`, `runners/credentials.py`,
`runners/serializers.py`, `runners/reports.py`, `runners/auth.py`, `runners/enrollment.py`,
`runners/jobs.py`, `runners/views.py`, `agent/creds.py`, `agent/runner.py`,
`agent/config.py`, `agent/logbuf.py`, `agent/redact.py`, `agent/client.py`,
`agent/executors/docker_exec.py`, `agent/identity.py`, `handlers/steps/external.py`,
`pipelines/external_finalize.py`, `pipelines/external_state.py`, `pipelines/log_stream.py`,
`pipelines/config_schemas.py`, `noderegistry/models.py`, `noderegistry/services.py`,
`noderegistry/serializers.py`, `noderegistry/views.py`,
`noderegistry/management/commands/external_demo_setup.py`,
`frontend/src/pages/ExternalNodesPage.tsx`, `lspo/settings/base.py`, and the two
Dockerfiles that fix the container user.

**What was executed rather than read.** Five things, all at the commit above:

1. **The offline example was run, both ways.** The fixture in
   [CONFORMANCE.md](CONFORMANCE.md#level-1-run-it-with-a-hand-written-envelope) was built,
   and the two commands there were each run against this repository's `node.py`. Run A,
   which sets the obsolete credentials variable alongside the real one, printed the two
   lines shown and left the three files shown, exiting 0. Run B, which sets only the
   variable the platform really sets, failed exactly as shown — an uncaught traceback and
   exit 1, with the staging directory left empty. Both transcripts in that document are
   verbatim.
2. **Every marker refusal was enumerated by exercising the parser**, not written from
   memory. A harness read the parser's own model definition, listed every field it declares
   and every cross-field check it runs, then fed it about ninety mutated markers one at a
   time and recorded each verdict. Every field and every check came back with at least one
   refusal. That is what "exercised" means wherever these documents use the word, and it is
   what the completeness claim in
   [CONFORMANCE.md](CONFORMANCE.md#validating-your-own-marker) rests on.
3. **The manifest, the envelope and the markers shown here were parsed** with the
   orchestrator's own `InvocationManifest` and `CompletionMarker`. The input file's size and
   hash were computed from the file.
4. **The input-count ceiling was measured**, by building manifests of increasing width with
   the real models until the writer's 8 MiB limit refused one
   ([PROTOCOL.md](PROTOCOL.md#23-reading-the-input-objects)).
5. **The runtime-deadline path was run on both sides**, because the account of it in an
   earlier draft was wrong in a way no amount of re-reading had caught. The agent's own
   loop was driven against a stand-in docker daemon and a stand-in orchestrator, and the
   orchestrator's runner API was driven through its own test client. Four things were
   observed rather than argued: the deadline reaches the container as a **polite** docker
   stop with a 30 second timeout, after which the agent classifies the run `failed`; a
   heartbeat refused with the orchestrator's own "past your deadline" code arrives inside
   the agent as its ordinary **lost-lease** error, indistinguishable from having lost the
   job to somebody else; on that answer the agent **kills the container outright and sends
   no terminal report at all**; and on the orchestrator's side the last heartbeat before
   the deadline shortens the lease to end exactly at the deadline, after which the terminal
   report is refused, the job's record is left in a live state, and the field that arms
   collection is never stamped. The platform's own test file for deadlines was also run at
   this commit and passes. This is what [PROTOCOL.md](PROTOCOL.md#7-cancellation) rests on.

**Which path those runs actually covered, because it is narrower than the section they
support.** Every one of them exercised a job whose **container was already running** when
its deadline passed. Nothing was exercised about a job still being prepared when its budget
runs out, about any of the six fences, or about collection — and the first two of those do
not behave like the path that was run. Where section 7 states an outcome that the runs did
not cover, it says so in the sentence itself and cites the source it was read from instead.
That distinction is the correction this round exists for: an execution proves the path it
took, and writing its result up as a universal is the same class of mistake as writing up a
guess.

That is all. **No real container, no deployed orchestrator, no object storage and no
collection run were exercised** — the runs in item 5 used stand-ins for the docker daemon
and, on the agent's side, for the orchestrator. So the arithmetic in section 7 about *how
much* of the thirty second grace survives is read from the shipped intervals and reasoned
about, not timed; what was observed is which of the two stops happens and whether a report
is sent. Everything about the storage service and about collection remains read from source.
Where a statement rests on a measurement somebody made earlier, it says so at the point it
is made.

**The labelling was checked mechanically, not by eye — and the checker itself had to be
rewritten first.** The earlier version reported zero while four real gaps sat in the
documents, because it only looked for a label when a paragraph contained a modal verb
("must", "never", "should"). That exempted every imperative ("Build in this order") and
every plain statement of fact about the platform ("Your node receives a list of input
objects on a single port"), which are the two commonest shapes a normative sentence takes
here. The replacement runs three checks over all five documents:

* **coverage** — every paragraph, list and table must either open with a label or sit under
  one, with a heading resetting the scope. The only escape is saying, in the text, that the
  block or its section is background. Nothing is exempted for lacking a modal verb;
* **the label must open the block**, so that prose merely mentioning the word
  "RECOMMENDATION" no longer opens a scope over everything after it;
* **the label must be the right one.** A **RULE** has to name a check — a source citation or
  an explicit refusal — somewhere in the statement it governs. What the platform *accepts*
  is BEHAVIOUR, not RULE, and what your *test* has to do is a RECOMMENDATION. Both were
  being written as rules.

A fourth report is advisory: a paragraph that inherits a label from the one before it while
opening with its own bold lede, which is how a stale label came to govern a run of
checklist section headings.

**What it still cannot see, stated plainly.** A label carries until the next label or the
next heading, so a paragraph that directly continues a labelled statement is legitimately
covered — and a label deleted from such a paragraph produces a document that still passes.
Narrowing the carry to lists and tables was tried and rejected: it flagged 56 blocks, nearly
all of them genuine continuations, and a check nobody reads is not a check.

**The checker was itself mutation-tested**, because a labelling checker that cannot fail is
the same defect it exists to find. Five defects were injected into the corrected documents,
which otherwise score zero on every count: four were caught, and the fifth is the residual
named above. Run against the documents *before* this round's corrections, it reproduces all
four of the gaps that round found by hand.

The finished set reports **zero** uncovered blocks, **zero** mislabelled ones and **zero**
inherited ledes.

There is no generated reference bundle yet, so there is no `REFERENCE.md`. The tables in
[PROTOCOL.md](PROTOCOL.md) are hand-verified against the commit above and nothing checks
them automatically. Line numbers in particular go stale on any edit to the file they point
into; the surrounding sentence is the claim, and the line number is only where to look.
When these documents and the orchestrator disagree, the orchestrator is right. If you find
a disagreement, that is a bug in this document set, and it is worth reporting.

## A warning about the code in this repository

This section is **background**, about one file in this repository. `node.py` at the
repository root is a **demonstration, not a model of correctness**. It has several known
defects, listed in [AUTHORING.md](AUTHORING.md#known-gaps-in-nodepy), and it is being
repaired separately. Where these documents show a shape and `node.py` differs, these
documents are the correct one.
