# orchestrator-hello-node

A template for an **external node**: a pipeline step that runs as your container, on your
machine, built from your own code. The orchestrator never sees the code. It sees an image
digest, hands the container a job description and short-lived credentials, and collects
whatever the container declares it produced.

Copy this repository, edit `node.py`, and you have your own node.

## What is here

| Path | What it is |
|---|---|
| `node.py` | A working example step. See the warning below |
| `Dockerfile` | The smallest image that is a real external step |
| `docs/` | **The documentation. Start here** |
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
**RECOMMENDATION** (what a good node does; the platform permits otherwise).

## A warning about `node.py`

`node.py` demonstrates the happy path and has known defects: it reads the wrong
environment variable for its credentials, reads them only once so a run longer than about
fifteen minutes cannot upload anything, buffers whole objects in memory, loses the
inventory of what it already produced when it fails, and ignores cancellation entirely.
The full list, with line numbers, is in
[docs/AUTHORING.md](docs/AUTHORING.md#known-gaps-in-nodepy). Read the shape of a correct
node in [docs/AUTHORING.md](docs/AUTHORING.md#the-skeleton), not from this file.

The previous version of this README, which described the contract at length, is kept at
[docs/_archive/README-2026-08-08.md](docs/_archive/README-2026-08-08.md). It contains
statements that are now known to be wrong; it is a historical record, not a reference.
