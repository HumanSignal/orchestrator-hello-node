# orchestrator-hello-node — your first external node

This repository is a **template for a pipeline step that runs on your own machine**, in
your own container, built from your own code. The orchestrator never sees the code: it
sees an image digest, hands the container a job description and short-lived credentials,
and collects whatever the container declares it produced.

Copy this repository, edit `node.py`, and you have your own node.

---

## What you need

* Docker, on the machine that will run the work.
* An orchestrator that has external nodes switched on (`LSPO_EXTERNAL_NODES_ENABLED=1`),
  reachable from that machine over HTTPS.
* Access to run one management command on the orchestrator (or someone who can).

---

## 1. Build the image and get its digest

```bash
docker build -t hello-node:dev .
```

The orchestrator refuses anything that is not **pinned by digest** — a tag can point at
different bytes tomorrow, and a node whose meaning changes silently is not reproducible.

If you have a registry:

```bash
docker tag hello-node:dev <registry>/hello-node:dev
docker push <registry>/hello-node:dev
docker inspect --format='{{index .RepoDigests 0}}' <registry>/hello-node:dev
# → <registry>/hello-node@sha256:…   ← this is what you register
```

Running everything on one machine, with no registry? Use the local image id, which is
also a digest:

```bash
docker image inspect --format='{{.Id}}' hello-node:dev   # → sha256:…
```

Register that bare `sha256:…` id as-is. One honest caveat: an image id only *resolves* on
a machine that already holds the image — it is exactly as immutable as a registry digest,
but there is nothing to pull. Fine for this demo, where the runner and the image share one
machine; the moment your pool spans a second machine, push to a registry and register the
`…@sha256:…` repo digest instead. The setup command prints this same warning when you hand
it a bare id, and refuses nothing.

## 2. Register the node with the orchestrator

On the orchestrator (locally: from the repo root; in a deployment: inside the `web`
container):

```bash
python manage.py external_demo_setup --image '<registry>/hello-node@sha256:…'
```

It prints three things:

* **a pool registration token** — shown once, so save it now;
* **the exact `docker run` line for the runner**, filled in with your orchestrator URL;
* **the deployment id**, which is what a pipeline node points at.

Run it twice and nothing is duplicated; it will not silently re-mint the pool token
either, because that token is shared by every agent already enrolled and rotating it
would lock all of them out.

## 3. Start the runner on your machine

The **runner** is our process; it is the only thing that talks to the orchestrator. It
enrols once, then polls for work, starts your container, streams its logs back, and
reports the result. It listens on no port — all connections are outbound.

The setup command in step 2 prints this line already filled in, together with the one
that has to run before it. It looks like:

```bash
mkdir -p /srv/lspo-agent && chmod 1777 /srv/lspo-agent

docker run -d --name lspo-agent \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --group-add $(getent group docker | cut -d: -f3) \
  -v /srv/lspo-agent:/srv/lspo-agent \
  -e LSPO_AGENT_WORKDIR=/srv/lspo-agent/state \
  -e LSPO_AGENT_API_URL='https://<your-orchestrator>' \
  -e LSPO_AGENT_POOL='<pool name from step 2>' \
  -e LSPO_AGENT_REGISTRATION_TOKEN='<the token printed in step 2>' \
  -e LSPO_AGENT_NAME=$(hostname) \
  -e LSPO_AGENT_MAX_CONCURRENT_JOBS=1 \
  --stop-timeout 300 \
  lspo-agent:dev
```

Every setting is `LSPO_AGENT_*`; the runner's own README is the authoritative list. Two
details in that command are not decoration. `--group-add` is how a non-root process
reaches the Docker socket (the runner refuses to start without it, rather than failing on
its first job). And the state directory is a **plain folder on this machine, mounted at
the same path inside the runner** — not a Docker volume, and that is not a style choice:
the runner hands paths under it to your Docker service when it attaches each job's
credentials into your container, and your Docker service looks those paths up on the
machine. A volume has no location there, and Docker's answer to a path it cannot find is
to create an empty folder rather than to complain — so your step would start with an empty
credentials directory and fail on a missing file, while every log said the credentials had
been written. The runner creates its own `state` folder inside the one you made, because
it tightens that folder's permissions and only an owner may do that.

After the first successful start you can **delete `LSPO_AGENT_REGISTRATION_TOKEN`**: the
runner boots from the identity in that folder. Keeping the shared pool secret on the
machine longer than necessary buys you nothing.

Check it enrolled:

```bash
docker logs lspo-agent | head
```

## 4. Point a pipeline node at it

In the orchestrator, add a script node whose config is:

```json
{"step_kind": "external", "external_deployment_id": <the id printed in step 2>, "params": {}}
```

Wire an `UPSTREAM_COMPLETE` edge from a step that produces an artifact into this node.

## 5. Run it

Trigger the upstream step and watch the Runs panel:

| What you see | What is happening |
|---|---|
| **Waiting for runner** | the job is queued; no worker of ours is holding anything |
| **Running**, with logs appearing | your container is running on your machine; its stdout is streamed back through the runner |
| **Completed**, with artifacts | the container's output was verified and published under the execution |

---

## What the container gets, and must give back

The runner mounts a credentials file read-only and sets `LSPO_CREDENTIALS` to its path:

```json
{
  "schema_version": 1,
  "scheme": "s3",
  "expires_at": "2026-08-07T12:00:00+00:00",
  "manifest_get": "https://…",
  "inputs": [{"name": "…", "relpath": "…", "sha256": "…", "size": 123, "get_url": "https://…"}],
  "staging": {"mode": "presigned_post",
              "post": {"url": "https://…", "fields": {…}, "key_prefix": "…/"}}
}
```

`manifest_get` fetches `invocation.json` — the job description: your `params`, the pinned
inputs, the attempt and generation numbers, the runtime budget.

Write whatever you like under your staging prefix, then **last of all** write
`__lspo_complete.json`:

```json
{
  "schema_version": 1,
  "execution_id": 42, "attempt": 1, "generation": 1,
  "status": "succeeded", "exit_code": 0,
  "objects":        [{"relpath": "outputs/data.csv", "sha256": "…", "size": 1234}],
  "produced_ports": {"output": ["outputs/data.csv"]}
}
```

Three rules, and why each exists:

1. **The marker is written last.** Its existence is taken as proof that everything it
   lists is already readable. Write it early and a half-finished run is indistinguishable
   from a complete one.
2. **Every path in `produced_ports` must appear in `objects`.** The inventory is what
   carries the hash and the size; a port entry outside it names a file nobody can verify.
3. **Hashes and sizes must match the bytes you wrote.** Collection re-reads every object
   and publishes **nothing at all** if one disagrees. A partly published delivery is worse
   than a failed one, because nothing downstream can tell which it got.

Exit codes: `0` succeeded · `1` transient, retry me · `10` permanent, do not retry ·
`20` cancelled.

## Where your logs go

Everything your container writes to stdout or stderr is captured by the runner, sent to the
orchestrator, and shown in the run's log — the same pane an operator watches while the step
runs. It is also kept in a log file stored with the execution, so it survives after the run.

**`logging` needs one line before it prints anything.** A bare `log.info(...)` in a fresh
Python program writes nothing at all: the default emits WARNING and above, and only to
stderr. This is the most common reason a node author says their logs disappeared. `node.py`
shows the working shape:

```python
logging.basicConfig(level=logging.INFO, stream=sys.stdout, format='%(levelname)s %(message)s')
log = logging.getLogger('hello-node')
```

`print()` works without any of that — just pass `flush=True` if you want a line to appear
while the step is still running rather than when it ends.

**Do not print secrets.** What your container writes is stored with the execution, shown to
everyone who can see that run, and searchable. No tokens, no credentials, no customer data
you would not put in a ticket. The credentials this step is handed are short-lived, but a
leaked one is still a leak — and note that an exception message can carry a presigned URL
into the log without you meaning it to.

**Only the tail survives.** The run keeps roughly the last thousand lines. A step that
prints a line per record will lose its beginning; if something matters, say it once, at the
end.

Your credentials expire (about 15 minutes) and are refreshed by the runner while your
container runs, so a long job never has to hold a long-lived secret. Uploads are bounded
to your own job's prefix by the storage service itself, not by our politeness.

---

## Troubleshooting

| Symptom | Cause | What to do |
|---|---|---|
| Runner logs `401` at startup | the pool token is wrong, or an old `LSPO_AGENT_TOKEN` is still set in the environment (it wins over the saved identity) | remove the stale variable; the saved identity in the volume is enough |
| Runner keeps logging "nothing to do" | no queued work for this pool, the pool is at its concurrency ceiling, or the node points at a different deployment | check the deployment id on the node |
| Execution sits at **Waiting for runner** | no runner is enrolled in that pool, or it cannot reach the orchestrator | `docker logs lspo-agent` |
| Run fails with "no completion marker" | your container exited without writing `__lspo_complete.json` | write the marker on the failure path too, with `"status": "failed"` |
| Run fails naming a hash mismatch | the file changed after you hashed it | hash the bytes you actually wrote, and write the marker afterwards |
| Container cannot reach the upload URL | credentials expired, or egress is blocked | check the clock on the machine and outbound HTTPS |

## Honest limits of this release

Dev and staging only. There is **no sandbox** — the container runs with your Docker
daemon's normal privileges, so run only code you trust. One input port and one output
port; single-file inputs. If the machine running the work disappears mid-job, the job
stays parked until an operator cancels it (the watchdog that reclaims it automatically is
a later slice).

---

## Conformance harness

`conformance/` holds a black-box test rig for this node: it builds the image from this
repository's own `Dockerfile`, runs it against a fake presigned-storage endpoint, and
judges it from the outside only — the environment it was given, the objects it uploaded,
**the order it uploaded them in**, and the exit code it died with.

```bash
pip install -r requirements-dev.txt
python -m pytest                            # known gaps run as expected failures; this is CI
python -m pytest --red-for-real             # the true result
python -m pytest --collect-only -q --print-labels   # what every test claims, and on whose authority
```

**Read `CONFORMANCE-BASELINE.md` before copying this repository as a template.** Twenty-one
tests are red today and they are the parts of `node.py` you should not copy verbatim yet —
but read the label on each one before treating it as a rule. Ten are contract violations
with the rule quoted; three are compatibility policy this repository chose for itself; and
eight are things a *reference* implementation ought to demonstrate that the contract
happily permits a real node to skip.
