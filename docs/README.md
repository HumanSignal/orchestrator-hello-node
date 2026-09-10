# Writing an external node

**BEHAVIOUR.** An **external node** is a pipeline step implemented by a container image.
In customer-run mode, you build the image and run an agent on your machine; the
orchestrator receives the image digest rather than your source. In hosted mode, the
orchestrator builds an approved Git repository and runs the image on a managed pool.
Both use the same job description, short-lived credentials and completion marker.
See [OPERATIONS.md](OPERATIONS.md#hosted-builds) for the hosted setup.

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
about it — should carry a label. The current review scope is recorded in
[Provenance](#provenance); the older mechanical labelling pass is historical evidence.

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

### Nothing bounds how long a stop takes

**BEHAVIOUR, and it is the other thing to internalise before reading further.** **No
interval on any stop path is guaranteed.** How long after a deadline your container is
signalled, how long it then has before it is killed, how soon a fence arrives, how long a
container that should be gone can go on writing — none of those is bounded by anything.
Each is a timer of ours plus a docker daemon that takes as long as it takes, an HTTP
request that may sit inside its own timeout and be retried, a periodic pass that may fail
and simply be tried again, and a kill that may fail outright.

The rest of this subsection is **background**, about these documents. Earlier drafts quoted
a figure for several of those intervals; every review round found another one that a
counterexample falsified, so the figures are gone rather than hedged. The full statement,
and what it means for how you write a node, is at the top of
[section 7 of PROTOCOL.md](PROTOCOL.md#7-cancellation), and the passages elsewhere inherit
it instead of repeating it. Where a duration does appear in these documents it is either a
setting of ours or a ceiling fixed by our own configuration — never a promise about how
long something takes.

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
| **How long I have once something asks my container to stop** (short answer: nothing guarantees you any interval at all) | [PROTOCOL.md](PROTOCOL.md#7-cancellation) |
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
| Which user my image should run as (any of them — here is why) | [OPERATIONS.md](OPERATIONS.md#which-user-your-image-runs-as) |
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
| Cancellation with exit code 20 | Yes | not applicable | A self-reported cancellation can salvage a valid marker as diagnostics. Operator cancellation of running work still does not collect its outputs; see [PROTOCOL.md](PROTOCOL.md#7-cancellation) |
| Runtime deadline stop window | Agent API | **Yes**, enabled by default: 180 seconds to stop plus 60 seconds to report | An accepted failure report can collect the marker and salvage verified objects as diagnostics |
| Hosted builds | Same container contract | Available when both external-node and hosted-build flags are enabled | Approved repository, managed pool, digest-pinned revision; same collection |
| Automatic retry of a failed attempt | Exit code 1 means "retry me" | not applicable | **No.** Nothing re-attempts an external job automatically. An operator retries by hand |

---

## Provenance

This section is **background**. The documentation was reviewed on **2026-09-10** against
the fetched default branches: this repository's `main` at
`c768c02f4ef734d1571133adae076f7c78900b4a`, and the orchestrator's **`master`** at
`30b0950a`. The orchestrator also has an older branch named `main`; it is not the
default branch and does not contain the external-node implementation.

All twelve tracked Markdown files were read. The current reference was checked against
`node.py`, its Dockerfile, the harness, tests and CI workflow, and against these platform
sources:

| Area | Sources in the orchestrator |
|---|---|
| Wire documents and validation | `external/contract.py`, `external/io.py`, `external/versioning.py`, `external/text.py` |
| Inputs and configuration | `handlers/steps/external.py`, `pipelines/config_schemas.py` |
| Credentials, runtime and stop window | `runners/credentials.py`, `runners/jobs.py`, `runners/reports.py`, `runners/claim.py` |
| Container environment, permissions and signals | `agent/runner.py`, `agent/creds.py`, `agent/config.py`, `agent/executors/docker_exec.py`, `agent/identity.py` |
| Logs and publication | `agent/logbuf.py`, `agent/redact.py`, `pipelines/log_stream.py`, `pipelines/external_finalize.py`, `pipelines/cancellation.py` |
| Registration and hosted builds | `noderegistry/services.py`, `noderegistry/serializers.py`, `noderegistry/building.py`, `noderegistry/hosted_views.py`, `noderegistry/models.py`, `noderegistry/management/commands/external_demo_setup.py`, `frontend/src/pages/ExternalNodesPage.tsx`, `lspo/settings/base.py` |

**Historical parser evidence (2026-08-08).** At orchestrator commit
`6b2ff82c70f26d0ceaa1a841137f1b3cfb08186b`, the original audit read the parser's model
definitions, listed every declared field and cross-field check, and fed about ninety
mutated markers to the parser one at a time, recording accepted and refused cases. Every
field and cross-field check had at least one refusal exercised. This is what **"exercised"**
means in the marker-refusal lists in PROTOCOL and CONFORMANCE. It is a record of that audit,
not a claim that the enumeration was rerun in September. The same audit measured the
input-count illustration by widening real manifests until the 8 MiB writer limit refused
one. Its old deadline experiment predates the stop-window implementation and is superseded
by the current source-based account in PROTOCOL section 7.

**Historical label coverage (2026-08-08, the same `6b2ff82c` platform snapshot).**
The original audit recorded a checker over all five guides for label coverage, labels
opening their blocks, and the distinction between rules, behavior and recommendations.
It reported zero uncovered blocks, mislabelled blocks and inherited bold ledes. A mutation
check caught four of five injected gaps; deleting a label from a legitimate continuation
remained undetectable. This historical checker was not rerun for the September update.

The frozen wire-document examples remain byte-identical to the platform's fixtures.
Current verification results are recorded in the opening note of
[CONFORMANCE-BASELINE.md](../CONFORMANCE-BASELINE.md). That file preserves the older
measurements, including claims about platform behavior that were true only at their
recorded commits. The archived README is also historical, not an operator guide.

This audit does not establish which flags, images or settings a live installation uses.
The current runtime-deadline account comes from the code and its existing tests; older
measurements of a deadline rejecting every terminal report do not describe the default
stop-window implementation now. No production run or real S3 transfer was performed for
this documentation update.

Source paths are provenance, not generated documentation. When these pages disagree
with the referenced source, the source is authoritative and the discrepancy is a
documentation defect. Keep enforced rules separate from recommendations, and distinguish
configured timers from a guarantee that a machine or network responds within them.

## The code in this repository

This section is **background**, about one file in this repository. `node.py` at the
repository root implements what these documents describe, and the conformance suite in
`conformance/` exercises it in real Docker containers. See the current verification note
before treating a past green run as evidence about this checkout. It was not always: it
shipped with twenty documented defects, every one of them a mistake a first version of a
node makes, and [CONFORMANCE-BASELINE.md](../CONFORMANCE-BASELINE.md) keeps the measurement
of what each one cost — which makes it the most useful thing here to read before writing
your own. Where `node.py` makes a choice rather than following a rule,
[AUTHORING.md](AUTHORING.md#nodepy-and-this-skeleton) says which choice and why. The platform source remains the authority for platform behavior. The local source is
the authority for what this example implements; report any disagreement with these pages.
