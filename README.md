# orchestrator-hello-node

A template for an **external node**: a pipeline step that runs as your container, on your
machine, built from your own code. The orchestrator never sees the code. It sees an image
digest, hands the container a job description and short-lived credentials, and collects
whatever the container declares it produced.

## Read this before you copy anything

**`node.py` is a demonstration of the happy path and it has known defects.** It reads the
wrong environment variable for its credentials, reads them only once (so a run longer than
about fifteen minutes can upload nothing at all), buffers whole objects in memory, loses
the inventory of what it already produced when it fails, and ignores stop signals
entirely. Copying it and editing it gives you a node with all of those problems.

The correct shape is in [docs/AUTHORING.md](docs/AUTHORING.md#the-skeleton), and the full
list of what is wrong with this file, line by line, is in
[docs/AUTHORING.md](docs/AUTHORING.md#known-gaps-in-nodepy). Where these documents and
`node.py` disagree, the documents are right. `node.py` is being repaired separately.

None of that is an opinion about the file. The harness in `conformance/` builds this
repository's image, runs it as a container and judges it from the outside only, and
**twenty of its tests are red against `node.py` today**;
[CONFORMANCE-BASELINE.md](CONFORMANCE-BASELINE.md) is the measured record of what each one
found. Read the label on a red test before treating it as a rule, because they do not all
carry the same weight: **six are contract violations**, with the platform's own rule
quoted; **three are a compatibility policy this repository chose for itself**; and
**eleven are things a *reference* implementation ought to demonstrate that the contract
happily permits a real node to skip**.

So: **start with [docs/](docs/), build from the skeleton, and use `node.py` only to see a
complete program end to end.**

## What is here

| Path | What it is |
|---|---|
| `node.py` | A working example step, with known defects. See the warning above |
| `Dockerfile` | The smallest image that is a real external step |
| `docs/` | **The documentation. Start here** |
| `conformance/` | A black-box harness: it builds this image, runs it, and judges it only from the outside |
| `tests/` | The tests that harness runs, and `pytest.ini` names the label on each one |
| `CONFORMANCE-BASELINE.md` | What the harness measured against `node.py` — the evidence behind the warning above |
| `CONTRIBUTING.md` | Notes on making this repository your own |

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
python -m pytest                                    # known gaps as expected failures; this is CI
python -m pytest --red-for-real                     # the true result
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
