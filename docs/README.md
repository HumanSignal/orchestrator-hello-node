# Writing an external node

An **external node** is a pipeline step that runs as **your container, on your machine**.
The orchestrator never sees your code. It sees an image digest, writes a job description
and short-lived credentials where your container can read them, starts the container
through an agent process you run, and then collects whatever the container says it
produced.

These four documents are everything you need to write one correctly. They are written for
someone (or something) that has only this repository and cannot read the orchestrator's
source, so **every value that matters appears here literally**. Source paths are given as
provenance, after the value, never instead of it.

---

## How to read this: three kinds of statement

Every normative sentence in these documents is labelled. The labels are not decoration.
Treating a recommendation as a rule produces code that is wrong in a different direction,
which is exactly as expensive as breaking a rule.

| Label | What it means |
|---|---|
| **RULE** | Break it and the platform refuses the job, or fails the run. There is a **specific check** in the platform's code, and the text names it. |
| **BEHAVIOUR** | What the platform does. Not a duty on you, but your code has to survive it. |
| **RECOMMENDATION** | What a well-written node does, and why. The platform permits the alternative. |

If a statement carries no label, it is background, not a requirement.

Two of these needed a phrase of their own. A few statements are marked
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
| What happens when someone presses Cancel | [PROTOCOL.md](PROTOCOL.md#7-cancellation) |
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

The contract models more than the orchestrator currently uses. Writing against a modelled
but unshipped capability produces a node that is correct and never exercised, which is a
worse failure than an obvious one because nothing reports it.

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
| Cancellation with exit code 20 | Yes | not applicable | **Yes**, though the platform does not need your exit code to call a run cancelled |
| Automatic retry of a failed attempt | Exit code 1 means "retry me" | not applicable | **No.** Nothing re-attempts an external job automatically. An operator retries by hand |

---

## Provenance

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

**What was executed rather than read.** The two documents shown in
[CONFORMANCE.md](CONFORMANCE.md#level-1-run-it-with-a-hand-written-envelope) were parsed
with the orchestrator's own `InvocationManifest` and `CompletionMarker` at this commit, and
every refusal claimed in [PROTOCOL.md](PROTOCOL.md#5-the-completion-marker) was exercised
against them individually; the input file's size and hash were computed from the file. That
is all. **No container, agent, orchestrator, run or upload was executed while writing
this**, so everything about the agent's behaviour, the storage service and collection is
read from source and reasoned about, not measured. Where a statement rests on a
measurement somebody made earlier, it says so at the point it is made.

There is no generated reference bundle yet, so there is no `REFERENCE.md`. The tables in
[PROTOCOL.md](PROTOCOL.md) are hand-verified against the commit above and nothing checks
them automatically. Line numbers in particular go stale on any edit to the file they point
into; the surrounding sentence is the claim, and the line number is only where to look.
When these documents and the orchestrator disagree, the orchestrator is right. If you find
a disagreement, that is a bug in this document set, and it is worth reporting.

## A warning about the code in this repository

`node.py` at the repository root is a **demonstration, not a model of correctness**. It
has several known defects, listed in [AUTHORING.md](AUTHORING.md#known-gaps-in-nodepy),
and it is being repaired separately. Where these documents show a shape and `node.py`
differs, these documents are the correct one.
