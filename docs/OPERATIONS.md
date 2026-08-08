# OPERATIONS: registering, running, and the limits of this release

An appendix. Nothing here changes how you write a node; all of it changes whether the node
you wrote ever runs.

Labels as elsewhere: **RULE**, **BEHAVIOUR**, **RECOMMENDATION**.

---

## Before anything

**BEHAVIOUR.** External nodes are behind a feature flag that is **off by default**
(`LSPO_EXTERNAL_NODES_ENABLED`, `lspo/settings/base.py:538`). On a deployment where it is
off, none of the registration endpoints answer and no external node runs.

**BEHAVIOUR.** This is a dev and staging capability today. There is no sandbox around your
container: it runs with the docker daemon's ordinary privileges on the machine hosting the
agent, with `no-new-privileges` set and the resource ceilings in
[PROTOCOL.md](PROTOCOL.md#32-what-your-container-may-reach), and the platform's own
position is that this is the customer's trusted code rather than hostile code.

---

## Registering a node

Two equivalent paths; both go through the same service layer, so they cannot drift.

**From the web interface.** Settings, then the **External Nodes** tab. It registers a
pool, a deployment and a runner agent, and shows the pool's registration token exactly
once.

**From a shell on the orchestrator.**

```bash
python manage.py external_demo_setup --image '<registry>/my-node@sha256:...'
```

It creates the three rows a node needs (pool, deployment, revision) and prints three
things: the pool's **registration token**, shown once because only its hash is stored; the
exact `docker run` line for the agent, filled in with your orchestrator URL; and the
**deployment id**, which is what a pipeline node points at.

**BEHAVIOUR.** The command is idempotent, and it deliberately does **not** re-mint the
pool token on a second run: that token is shared by every agent already enrolled, so
rotating it would lock all of them out. Pass `--rotate-token` when you actually mean it.

**BEHAVIOUR.** The command's own printed instructions still say there is no user interface
for external nodes. That sentence is out of date; the Settings tab exists.

### The image reference

**RULE.** Digest-pinned, in one of two spellings: `registry/name@sha256:<64 hex>` or a bare
`sha256:<64 hex>` image id. A tag is refused.

```bash
# with a registry
docker push <registry>/my-node:dev
docker inspect --format='{{index .RepoDigests 0}}' <registry>/my-node:dev

# one machine, no registry
docker image inspect --format='{{.Id}}' my-node:dev     # -> sha256:...
```

**BEHAVIOUR.** A bare image id produces a warning at registration and is accepted. Nothing
re-checks it afterwards, and nothing counts the runners in the pool, so a bare id on a
multi-machine pool simply fails on every host that does not already hold the image, with a
missing-image error rather than anything that names the real cause.

---

## Starting the agent

The **agent** is our process, running on your machine. It is the only thing that talks to
the orchestrator: it enrols once, polls for work, starts your container, streams its logs
back and reports the result. It listens on no port; every connection is outbound.

The setup command prints this line already filled in. It looks like:

```bash
mkdir -p "$HOME/lspo-agent" && chmod 1777 "$HOME/lspo-agent"

docker run -d --name lspo-agent \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --group-add $(getent group docker | cut -d: -f3) \
  -v "$HOME/lspo-agent:$HOME/lspo-agent" \
  -e LSPO_AGENT_WORKDIR="$HOME/lspo-agent/state" \
  -e LSPO_AGENT_API_URL=https://orchestrator.example.com \
  -e LSPO_AGENT_NAME=$(hostname) \
  -e LSPO_AGENT_POOL=self-hosted \
  -e LSPO_AGENT_REGISTRATION_TOKEN=<from the pool> \
  --stop-timeout 300 \
  lspo-agent:dev
```

Four parts of that are load-bearing (`agent/README.md`, "Run it"):

* **`--group-add`** with the host's docker group id. The agent image runs as a non-root
  user and has no access to the docker socket without it. The agent refuses to start
  rather than failing on its first job.
* **The state bind mount, with the host path and the container path spelled the same**,
  and the workdir pointing at a **child** of it. This is the one that silently destroys
  jobs when it is wrong. The agent asks the host's docker daemon to bind-mount each job's
  credentials directory into that job's container, and the daemon resolves those paths on
  the host. State kept in a docker **volume** is at a path the host has nothing at, and
  the daemon's answer to a bind source that does not exist is to **create an empty
  directory** rather than to fail. Your container then starts with an empty credentials
  directory and dies on a missing file while the agent's log says it wrote one. The agent
  warns about this at startup when it can detect it.
* The child directory matters too: the agent creates its own state directory because it
  tightens the permissions on it, and only an owner may do that. So you create the parent
  and the agent creates the state directory inside it.
* **`--stop-timeout 300`.** SIGTERM to the agent means "finish the jobs you are running".
  Docker's ten second default would kill it long before a real step finishes.

**RECOMMENDATION.** Delete `LSPO_AGENT_REGISTRATION_TOKEN` from the environment after the
first successful start. The agent boots from the identity saved in its state directory,
and the pool token is shared by every agent in the pool.

### Agent settings that change what your node sees

All are `LSPO_AGENT_*` (`agent/config.py:113-137`, `:333-346`).

| Setting | Default | Why a node author cares |
|---|---|---|
| `ALLOWED_ENV` | empty, which means **nothing is passed** | Exact names or glob patterns. A variable your deployment declares that is not on this list **fails the job** before your container starts |
| `MEMORY` | `2g` | Your container's memory ceiling, against a 1 GiB per-object allowance |
| `CPUS` | `2.0` | |
| `PIDS_LIMIT` | `512` | |
| `NETWORK` | empty, the daemon's default | Whether your container has ordinary outbound networking |
| `MAX_CONCURRENT_JOBS` | `1`, capped at 200 | How many of your containers this machine runs at once |
| `HEARTBEAT_INTERVAL_S` | `20` | How often your logs are shipped |
| `LOG_LINES_PER_HEARTBEAT` | `100` | Five lines per second, sustained, is the ceiling |
| `LOG_BUFFER_LINES` | `2000` | How much backlog survives a burst |
| `CREDS_REFRESH_MARGIN_S` | `60` | How early your credentials file is replaced |
| `KEEP_CONTAINERS` | false | Keeps exited containers for inspection; it never keeps a workload running |

---

## The uid coupling

**BEHAVIOUR.** Each job's credentials directory is created on the agent's disk with mode
`0700` and the file inside it `0600`, owned by the uid the agent process runs as
(`agent/creds.py:88-98`, `agent/identity.py:64-65`). In object-storage mode the agent does
**not** force your container's user, so your image runs as its own `USER`.

A `0700` directory owned by uid A is unreadable to a process running as uid B. So the two
uids must match, and nothing checks or warns.

* The shipped agent image runs as **uid 10001** (`Dockerfile.agent`).
* The example node image also uses **uid 10001**, independently.
* An agent started directly on a host instead of from that image runs as the invoking
  user, commonly uid 1000, and then 10001 is wrong.

**RECOMMENDATION.** Build your image with `USER` at uid 10001 and confirm with whoever
runs the agent. Do not "fix" a permission error by running as root: it does work, because
root bypasses the check, and it puts a root process on somebody's machine for nothing.

**BEHAVIOUR, local demo mode only.** With local-path staging the agent forces your
container to its own uid and gid with no supplementary groups, so your image's user is
ignored entirely. Anything that writes under that user's home directory works in
production and fails in the demo. Use `/tmp` or the staging directory.

---

## Pointing a pipeline node at your deployment

Add a script node whose configuration is:

```json
{"step_kind": "external", "external_deployment_id": 12, "params": {}}
```

Full configuration surface (`pipelines/config_schemas.py:475-513`):

| Key | Type | Required | Meaning | Reaches your container |
|---|---|---|---|---|
| `step_kind` | `"external"` | yes | discriminator | no |
| `external_deployment_id` | integer > 0 | **yes** | which registered deployment runs. Booleans are rejected explicitly | only through its effects |
| `params` | object | no, default `{}` | **the only node configuration your code ever sees**, copied verbatim | **yes** |
| `timeout_seconds` | integer > 0 or null | no | requested runtime budget | **yes** |
| `queue_timeout_seconds` | integer > 0 or null | no | how long the job stays claimable, default 3600 | no |
| `input_payload_kind` | string or null | no | require an upstream artifact of this kind; absent means every upstream artifact, including none | indirectly |

**BEHAVIOUR, and it costs people an afternoon.** Unknown configuration keys are **accepted
and ignored**. A typo such as `timeout_second` validates cleanly, is stored on the node,
and does nothing at all.

**BEHAVIOUR.** With `input_payload_kind` set and no matching upstream artifact, the step
fails after exhausting the engine's ordinary retry ladder rather than immediately. With it
unset and no upstream artifacts at all, the job runs with no input port, which is a
legitimate source-style step.

**BEHAVIOUR.** A folder (prefix layout) artifact upstream is refused immediately and
permanently, with a message naming the artifact. It is not retried.

---

## Revisions: what is immutable and what is actually read

A deployment points at a revision, and the revision is what pins the code.

| Field | Read at run time |
|---|---|
| `image_digest` | **Yes.** This is what executes |
| `declared_io` | **Partly.** Exactly two keys are read: `timeout_seconds` (a fallback budget) and `env_names` (variable **names**, never values) |
| `param_schema` | **No.** Nothing validates `params` against it anywhere |
| `contract_version` | **No.** The manifest is always stamped with the orchestrator's current contract version, which is `1` |
| `resource_class` | **No.** It does not size the container; the agent's own ceilings do |

**RECOMMENDATION.** Register a new revision whenever the image changes. The digest is the
node's identity, and it is what makes an old run reproducible.

---

## Pools, runners and quotas

**BEHAVIOUR.** A pool is either `managed` (operated by us, global) or `self_hosted`
(operated by a customer, owned by one organization). Relevant defaults
(`noderegistry/models.py:40-108`):

| Setting | Default | Effect |
|---|---|---|
| `max_concurrent` | **2** | Concurrently running launches in the pool |
| `max_runners` | **50** | Active agent identities that may enrol, which bounds who can mint one with the shared token |
| `registration_enabled` | true | Turning it off stops new agents joining without rotating the shared token |
| `is_active` | true | An inactive pool takes no new work |

**BEHAVIOUR.** Concurrency quotas nest, always taken in the order pool, then organization,
then pipeline. The pool row always exists; the other two are opt-in and absent means no
ceiling at that level.

**BEHAVIOUR.** A saturated quota is never a failure. The agent's poll simply returns
nothing and the job stays queued for someone else.

**BEHAVIOUR.** An agent may hold at most 200 assigned jobs, which is why
`LSPO_AGENT_MAX_CONCURRENT_JOBS` may not exceed 200.

---

## Residual limits, stated plainly

Each of these is a real gap at the commit these documents were verified against. None is a
rumour.

* **No watchdog.** Nothing reclaims a job that nobody ever claims, or one whose agent
  disappears after claiming it. The queue deadline only makes the claim scan skip the row.
  The execution stays parked at "Waiting for runner", holding its quota slot, until an
  operator cancels it. If you are testing "what if my machine dies mid-job", this is what
  you will see.
* **No automatic retry.** Every failed external attempt is recorded as transient
  regardless of your exit code, and nothing re-attempts. An operator retries by hand.
* **No aggregate upload quota.** One object is bounded at 1 GiB. Nothing caps the number of
  objects or the total bytes a container may upload before its credentials expire.
* **Presigned reads can outlive their stated expiry.** A presigned GET can remain valid
  past the envelope's `expires_at` by up to the budget left when it was signed. The upload
  policy cannot. Treat `expires_at` as exact for writes, and as a lower bound for reads.
* **Logs are tail-only while the run is live.** The last 1000 entries. The durable copy is
  written when the execution reaches a terminal state.
* **One input port, single files only.** The contract models multiple ports and folder
  inputs; the orchestrator emits neither.
* **`result.json` is never read.** Metrics come from what was published.
* **No sandbox.** Stated above, repeated here because it belongs on a limits list.

---

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| Execution sits at **Waiting for runner** | no agent is enrolled in that pool, the pool's concurrency ceiling is reached, or the agent cannot reach the orchestrator | `docker logs lspo-agent`; check the pool's `max_concurrent`; confirm the deployment id on the node |
| Agent logs 401 at startup | wrong pool token, or a stale `LSPO_AGENT_TOKEN` still in the environment, which wins over the saved identity | remove the stale variable; the saved identity is enough after the first start |
| Job fails immediately naming an environment variable | the deployment declares a variable the agent's `ALLOWED_ENV` does not permit | add the name or a pattern to the agent's allowlist, or stop declaring it |
| Container dies at once with a missing credentials file | the agent's state is in a docker volume rather than a host path, so the credentials directory the daemon mounted was an empty one it created | mount a real host directory at the same path inside and outside, with the workdir a child of it |
| Container dies with permission denied on its credentials | uid mismatch between your image and the agent process | rebuild with the agent's uid, usually 10001 |
| Node fails with "`LSPO_CREDENTIALS` is not set" | your code reads the wrong variable name | read `LSPO_CREDENTIALS_FILE` |
| Run fails with "no completion marker" | your container exited 0 without writing `__lspo_complete.json` | write the marker on every path, including failure |
| Run fails naming a hash or size mismatch | the object changed after you hashed it, or the marker was written before the upload finished | hash the bytes you actually wrote, and write the marker last |
| Uploads start failing partway through a long run | the credentials envelope expired, roughly fifteen minutes in | re-read the credentials file at or near `expires_at` |
| Upload refused with a policy error | the object key does not start with `staging.post.key_prefix`, or the object is over 1 GiB | prefix the key explicitly; split the object |
| A cancelled run keeps going, then dies | no SIGTERM handler, so PID 1 discarded the signal and the 30 second grace ran out | install a handler that sets a flag |
| Logs stop partway through | you exceeded the shipping rate, or a single line exceeded 64 KiB and its tail was discarded | fewer, shorter lines |
| Logs never appear at all | a logging library that defaults to WARNING and to stderr only, or a buffered stdout | configure the logger explicitly and set `PYTHONUNBUFFERED=1` |
| The node runs but downstream steps see nothing | the objects were inventoried but claimed under no output port | claim them under a port; `produced_ports` is what becomes downstream artifacts |
| A configuration key on the node seems to do nothing | unknown keys are accepted and ignored | check the spelling against the configuration table above |
