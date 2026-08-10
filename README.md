# orchestrator-hello-node

A template for an **external node**: a pipeline step that runs as your container, on your
machine, built from your own code. The orchestrator never sees the code. It sees an image
digest, hands the container a job description and short-lived credentials, and collects
whatever the container declares it produced.

## Read this before you copy anything

**`node.py` is safe to copy.** It passes the whole conformance suite: the harness in
`conformance/` builds this repository's image, runs it as a real container and judges it
from the outside only, and **all 128 of its tests are green**.

That was not true until recently. Twenty tests used to be red, and
[CONFORMANCE-BASELINE.md](CONFORMANCE-BASELINE.md) is the measured record of what each one
found and how it was repaired — worth reading, because every one of those defects is one a
first version of a node makes, and the document says what each cost.

What it now demonstrates, all of which the platform checks in no way whatsoever and every
one of which decides whether a real job survives:

* it reads the credentials path from `LSPO_CREDENTIALS_FILE`, the variable the agent
  actually sets, and never bakes a path into the image;
* it verifies every input against both its pinned hash and its pinned size;
* it **streams** in both directions and never holds an object in memory — which is why it
  has no HTTP-library dependency at all, since `requests` builds a multipart body in
  memory and would contradict the very rule this file exists to demonstrate;
* it re-reads its credentials, so a run longer than fifteen minutes can still upload;
* it keeps its inventory where the failure path can see it, and records an object before
  the upload starts;
* it handles a stop request, so a cancelled run stops taking on new work instead of
  running to completion for nobody;
* it writes the completion marker last, on every path, with the exit code the process
  really returns;
* it never prints a presigned URL.

Read the label on any test before treating it as a rule: they do not carry the same
weight. `basis_contract` means the platform's own sources say so and the citation quotes
them. `basis_our_policy` is a compatibility choice this repository makes. Most are
`basis_reference_quality` — what a reference implementation ought to demonstrate, which
the contract happily permits a real node to skip.

So: **start with [docs/](docs/), then copy `node.py` and edit it.**

If you would rather follow a numbered list — and if you are a coding agent working from
somebody's task description, this is the one to follow — the ordered path from that
description to a node the orchestrator can run is
[Building your own node from this template](CLAUDE.md#building-your-own-node-from-this-template)
in `CLAUDE.md`: read the documents, edit `node.py`, prove it with the harness, take the
digest, hand it over.

## What is here

| Path | What it is |
|---|---|
| `node.py` | A complete, correct example step. Copy it and edit |
| `Dockerfile` | The smallest image that is a real external step |
| `docs/` | **The documentation. Start here** |
| `conformance/` | A black-box harness: it builds this image, runs it, and judges it only from the outside |
| `tests/` | The tests that harness runs, and `pytest.ini` names the label on each one |
| `CONFORMANCE-BASELINE.md` | What the harness measured against `node.py`, before and after the repair |
| `CONTRIBUTING.md` | Notes on making this repository your own |
| `CLAUDE.md` | The ordered path from a task description to a connected node, and the invariants `node.py` exists to demonstrate |

## Build and run

```bash
docker build -t hello-node:dev .

# with a registry
docker push <registry>/hello-node:dev
docker inspect --format='{{index .RepoDigests 0}}' <registry>/hello-node:dev

# one machine, no registry
docker image inspect --format='{{.Id}}' hello-node:dev      # -> sha256:...
```

That digest is what you register. Registering the node, starting the agent that runs it,
and pointing a pipeline at it are all in
[docs/OPERATIONS.md](docs/OPERATIONS.md#registering-a-node).

To run the program with no orchestrator at all, against a hand-written credentials file,
see [docs/CONFORMANCE.md](docs/CONFORMANCE.md#level-1-run-it-with-a-hand-written-envelope).

To put the built image in front of the harness — this repository's node, or your own once
you have edited it:

```bash
pip install -r requirements-dev.txt
python -m pytest                                    # this is CI
python -m pytest --red-for-real                     # the true result, ignoring expected-red bookkeeping
python -m pytest --collect-only -q --print-labels   # what every test claims, and on whose authority
```

It needs a Docker daemon, and it refuses to run rather than simulate a container. What a
suite like this can and cannot prove is in
[docs/CONFORMANCE.md](docs/CONFORMANCE.md#if-a-conformance-directory-exists-in-this-repository).

## Documentation

| Document | Read it for |
|---|---|
| [docs/README.md](docs/README.md) | The index, a routing table, and what the platform does and does not support today |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | The contract: every field, every rule, in the order one container attempt happens |
| [docs/AUTHORING.md](docs/AUTHORING.md) | How to build one: the recipe, an annotated skeleton, and a checklist |
| [docs/CONFORMANCE.md](docs/CONFORMANCE.md) | How to test a node, and what a test can and cannot prove |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Registration, revisions, pools, limits, troubleshooting |

Every normative statement in those documents is labelled **RULE** (the platform refuses or
fails the run), **BEHAVIOUR** (what the platform does, which you must plan for) or
**RECOMMENDATION** (what a good node does; the platform permits otherwise). The labels are
load-bearing: some of the most important things a node must do — writing the completion
marker last, verifying its inputs against their hashes, re-reading its credentials — are
recommendations, because nothing in the platform checks them. They are no less important
for that. What the label tells you is what happens when you get it wrong: a refusal you
can see, or a failure somewhere else with no explanation attached.

## History

The previous version of this README, which described the contract at length, is kept at
[docs/_archive/README-2026-08-08.md](docs/_archive/README-2026-08-08.md). It contains
statements that are now known to be wrong; it is a historical record, not a reference.
