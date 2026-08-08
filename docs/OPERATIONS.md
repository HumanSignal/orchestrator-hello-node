# OPERATIONS: registering, running, and the limits of this release

**Background**, about this document: it is an appendix. Nothing here changes how you write
a node; all of it changes whether the node you wrote ever runs.

Labels as elsewhere — **RULE**, **BEHAVIOUR**, **RECOMMENDATION** — and this line is
**background** about them.

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

**BEHAVIOUR.** Two equivalent paths; both go through the same service layer, so they cannot
drift.

### From the web interface

**BEHAVIOUR.** Settings, then the **External Nodes** tab, then **Connect node**. It creates
the same three database rows the command below does — a **pool**, a **deployment** and a
**revision** — and shows the pool's registration token exactly once, together with the
`docker run` line for an agent.

It does **not** create the agent. Nothing does: an agent comes into existence when
somebody runs that `docker run` line on a machine, and it enrols itself using the
registration token. Until then the deployment is registered and has nowhere to run, which
the screen shows as "no agent yet".

### From a shell on the orchestrator

**BEHAVIOUR.** The same three rows, from the command line:

```bash
python manage.py external_demo_setup --image '<registry>/my-node@sha256:...'
```

It creates the same three rows (pool, deployment, revision) and prints three things: the
pool's **registration token**, shown once because only its hash is stored; the exact
`docker run` line for the agent, filled in with your orchestrator URL; and the
**deployment id**, which is what a pipeline node points at.

**BEHAVIOUR.** Both paths give the new revision a runtime budget of **900 seconds** when
you do not name one (`--timeout-seconds` on the command,
`noderegistry/management/commands/external_demo_setup.py:38`; the `timeout_seconds` field
on the registration API, `noderegistry/serializers.py:26`). That number, not the
platform's 3600-second fallback, is what your container actually gets — see
[PROTOCOL.md](PROTOCOL.md#24-how-long-you-actually-get). A revision is immutable, so
changing it later means either publishing a new revision or setting `timeout_seconds` on
the pipeline node, which takes effect immediately and wins.

**BEHAVIOUR.** The command is idempotent, and it deliberately does **not** re-mint the
pool token on a second run. Pass `--rotate-token` when you actually mean it.

**BEHAVIOUR, and three places in the platform say this wrongly.** Rotating the pool's
registration token does **not** lock out the agents already enrolled. That token buys
exactly one thing — permission to create a new agent identity in that pool — and each
agent, once enrolled, authenticates with its own bearer token minted at enrolment
(`runners/auth.py:21-33`, `noderegistry/views.py:262-270`). Rotating stops the old secret
enrolling anything further and nothing else: every running agent keeps claiming,
heartbeating and completing exactly as before. (The setup command's own module docstring
and `noderegistry/services.py:12-15` both still say rotation "would lock all of them out".
They are wrong; the Settings screen, which says the opposite, is right.)

**BEHAVIOUR, and this is the third wrong sentence, so read it carefully.** Switching the
pool's `registration_enabled` off does **not** stop an enrolled agent either. That flag is
consulted in exactly one place — the enrolment endpoint, where it decides whether a *new*
agent may join (`runners/enrollment.py:138`). Nothing on a live request looks at it. What a
live request checks, on every claim, heartbeat, credential re-issue and completion, is
three other things: that the runner is still active, that its **pool** is still active, and
that the organization owning that pool is still active (`runners/auth.py:487-500`,
`:353-360`). So the ways to actually stop an enrolled agent are: **retire the runner**,
**deactivate the pool**, or deactivate the owning organization — and of those, retiring the
runner is the one that stops a single machine rather than everything on the pool.
(`noderegistry/views.py:262-270`, the rotate-token endpoint, tells the operator to "switch
the pool's `registration_enabled` off" to stop an enrolled agent. That sentence is wrong in
the same way the two above are.)

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

**BEHAVIOUR.** The **agent** is our process, running on your machine. It is the only thing
that talks to the orchestrator: it enrols once, polls for work, starts your container,
streams its logs back and reports the result. It listens on no port; every connection is
outbound.

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

**BEHAVIOUR, for all four bullets below.** Four parts of that command are load-bearing, and
each one fails in its own way when it is wrong (`agent/README.md`, "Run it"). None of them
is a duty on your node — they are the operator's, and they are here because your node is
what visibly breaks:

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
and the pool token is shared by every agent in the pool — leaving it on a machine leaves
the ability to enrol more agents lying around on that machine
(`agent/identity.py:14-27`).

### Agent settings that change what your node sees

**BEHAVIOUR, for the table below.** All are `LSPO_AGENT_*` and all are the agent operator's
to set, not yours (`agent/config.py:113-137`, `:333-346`). The defaults are what your node
gets unless somebody changed them.

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
| `LEASE_EXPIRY_GRACE_S` | `60` | How long past its expired lease a job keeps running while the orchestrator is unreachable. Past it the container is **killed outright**, with no grace period — see [PROTOCOL.md](PROTOCOL.md#71-fencing-the-stop-with-no-grace-period-at-all) |
| `KEEP_CONTAINERS` | false | Keeps exited containers for inspection; it never keeps a workload running |

There is **no** disk or storage setting here, and that is not an omission in the table: the
agent applies a CPU, a memory and a process ceiling to your container and no storage
ceiling at all (`agent/executors/docker_exec.py:248-274`).

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

**RULE for the "Required" column, BEHAVIOUR for the rest of the table below.** The
configuration validator refuses a node whose `external_deployment_id` is missing or is not
a positive integer; everything else in the table is what the platform does with a key you
did set (`pipelines/config_schemas.py:475-513`).

| Key | Type | Required | Meaning | Reaches your container |
|---|---|---|---|---|
| `step_kind` | `"external"` | yes | discriminator | no |
| `external_deployment_id` | integer > 0 | **yes** | which registered deployment runs. Booleans are rejected explicitly | only through its effects |
| `params` | object | no, default `{}` | **the only node configuration your code ever sees**, copied verbatim | **yes** |
| `timeout_seconds` | integer > 0 or null | no | requested runtime budget. **Set this to give a node more than the 900 seconds its revision declares** — it wins over the revision and takes effect at once | **yes** |
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

**BEHAVIOUR, for the table below.** A deployment points at a revision, and the revision is
what pins the code. The table says which of the revision's fields are actually read when a
job runs — the others are stored and ignored.

| Field | Read at run time |
|---|---|
| `image_digest` | **Yes.** This is what executes |
| `declared_io` | **Partly.** Exactly two keys are read: `timeout_seconds` (the budget, unless the pipeline node overrides it — **900** on a default registration) and `env_names` (variable **names**, never values) |
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

**BEHAVIOUR, for every bullet below.** Each is a real gap at the commit these documents were
verified against, and none is a rumour: they are things the platform does, or does not do,
which you cannot change from a node.

* **No watchdog.** Nothing reclaims a job that nobody ever claims, or one whose agent
  disappears after claiming it. The queue deadline only makes the claim scan skip the row.
  The execution stays parked at "Waiting for runner", holding its quota slot, until an
  operator cancels it. If you are testing "what if my machine dies mid-job", this is what
  you will see — and it is also where a job that simply **ran out of time** ends up, see
  the next bullet.
* **Hitting the runtime budget leaves a running container's run parked, and collects
  nothing.** The agent does stop the container when the budget runs out, but the
  orchestrator refuses the terminal report that would normally finish the job: the last
  heartbeat before the deadline shortened the lease to end at the deadline, and a report
  on an expired lease is turned away. Nothing arms collection, so no marker is read and
  nothing the step produced is published, and the execution stays at "Waiting for
  runner" until somebody cancels it. The 30 second grace the agent asks docker for is
  also cut short — the platform's own next heartbeat comes back refused and the agent
  kills the container, which with the shipped intervals happens somewhere in the first
  20 seconds. The one job that ends differently is one still being **prepared** when its
  budget runs out: the deadline is not being watched yet, the claim's own five-minute
  lease has not been shortened yet, and a failure there is reported and accepted, so the
  run finishes as failed rather than parking. Whether that one collects anything depends
  on where in preparation it died, and "it failed during preparation" does not answer
  that: the container is created and started **before** the agent's own log and
  heartbeat threads are. A failure in the earlier steps — an unreadable job description,
  a failed image pull — leaves no container and nothing to collect; a failure starting
  one of those threads, which a busy host really does produce, leaves the container
  running and still sends the report that arms collection. Full derivation, and which
  half of it was executed rather than read, in
  [PROTOCOL.md](PROTOCOL.md#7-cancellation). The practical consequence for an operator
  is the same in both cases: give a step a budget it will comfortably finish inside; a
  budget that is merely "close enough" does not degrade gracefully, it loses the run.
* **No automatic retry.** Every failed external attempt is recorded as transient
  regardless of your exit code, and nothing re-attempts. An operator retries by hand.
* **No aggregate upload quota.** One object is bounded at 1 GiB. Nothing caps the number of
  objects or the total bytes a container may upload before its credentials expire.
* **No limit on the SIZE of inputs, and an indirect one on their number.** Nothing caps one
  input object or their total bytes. The count, however, is capped after all: every input
  and its metadata are written into the job description, and the writer refuses to publish
  one over 8 MiB (`external/io.py:148-157`). Measured against the real models, that is
  roughly **25,000 inputs** with 95-character URIs, at about 330 bytes each. A step wired
  downstream of something that produced ten thousand files runs fine; one wired downstream
  of something that produced fifty thousand fails **before any container starts**, while
  the orchestrator is still writing the job description. See
  [PROTOCOL.md](PROTOCOL.md#23-reading-the-input-objects).
* **No disk ceiling on the container.** The agent sets a CPU limit, a memory limit and a
  process limit, and no storage limit (`agent/executors/docker_exec.py:248-274`). A node
  that downloads its inputs to `/tmp` and never deletes them can fill the disk of the
  machine hosting the agent — which is a customer's machine, running their other work.
  Nothing warns, and the first symptom is unrelated things failing on that host.
* **Presigned reads can outlive their stated expiry.** A presigned GET can remain valid
  past the envelope's `expires_at` by up to the budget left when it was signed. The upload
  policy cannot. Treat `expires_at` as exact for writes, and as a lower bound for reads.
* **Logs are tail-only, and so is the durable copy.** The last 1000 entries, live and in
  the file written when the execution reaches a terminal state — the file is built from
  the same buffer. There is no complete record of a chatty container's output anywhere.
* **A fenced container is killed outright.** The runtime deadline at least begins with a
  SIGTERM; the fencing path gives the container a SIGKILL and nothing else. It therefore
  writes no further marker, and what is collected afterwards is only what a valid marker
  for that attempt had already recorded — under the recommended write-the-marker-last
  discipline, usually nothing. Usually and not always: the marker is written last, but
  it does not vanish at the instant the process does, and a fence landing between its
  upload and the agent seeing the container go collects what it names. There are six
  fencing triggers and they differ in whether the agent may even report the outcome; the
  table in [PROTOCOL.md](PROTOCOL.md#71-fencing-the-stop-with-no-grace-period-at-all)
  gives each one with its own consequence. Note that an ordinary agent shutdown is
  **not** one of them: it waits for your job, and a forced shutdown leaves your
  container running for the next agent to adopt.
* **Pressing Cancel is usually a kill, not a polite stop, and it collects nothing.**
  Cancelling a step that is running on a runner ends the job on the orchestrator's side in
  one transaction, which means the agent's next heartbeat is refused and the agent responds
  by SIGKILLing the container. Only a heartbeat already in flight can come back carrying the
  cancellation and produce a SIGTERM instead. Either way **no marker is read and nothing the
  step produced is collected** — the log is written down, the run is finished, and whatever
  was in the staging area expires there. Full derivation in
  [PROTOCOL.md](PROTOCOL.md#7-cancellation). The practical consequence for an operator: use
  Cancel to stop work, never to harvest a partial result.
* **One input port, single files only.** The contract models multiple ports and folder
  inputs; the orchestrator emits neither.
* **`result.json` is never read.** Metrics come from what was published.
* **No sandbox.** Stated above, repeated here because it belongs on a limits list.

---

## Troubleshooting

**The table below is labelled row by row, in its Kind column**, rather than by one label
governing the whole of it; this paragraph and the next are **background** explaining how to
read that column. **RULE** means a specific check refused you and the fix is not optional.
**BEHAVIOUR** means the platform did something by design and your node has to accommodate
it. **Unchecked** means nothing in the platform looks at this at all — it is a coupling or
a convention that fails as some unrelated-looking symptom, and no amount of correct
behaviour elsewhere will produce a warning about it.

**The Kind column labels the CAUSE, not the cure**, and the two are not always the same
kind of thing — still **background** about how to read the table. Where the Kind is RULE,
the first clause of "What to do" is what that check demands. Everything else in that column
— including advice attached to a RULE row — is a **RECOMMENDATION**: it is what a
well-built node does, nothing verifies it, and the platform permits the alternative. The
two rows where that distinction bites are marked inline.

| Symptom | Likely cause | Kind | What to do |
|---|---|---|---|
| Execution sits at **Waiting for runner** | no agent is enrolled in that pool, the pool's concurrency ceiling is reached, or the agent cannot reach the orchestrator | BEHAVIOUR — a saturated quota is never an error, the job simply waits | `docker logs lspo-agent`; check the pool's `max_concurrent`; confirm the deployment id on the node |
| Agent logs 401 at startup | wrong pool token, or a stale `LSPO_AGENT_TOKEN` still in the environment, which wins over the saved identity | RULE — the token is authenticated on every request | remove the stale variable; the saved identity is enough after the first start |
| Job fails immediately naming an environment variable | the deployment declares a variable the agent's `ALLOWED_ENV` does not permit | RULE — checked before your container starts | add the name or a pattern to the agent's allowlist, or stop declaring it |
| Container dies at once with a missing credentials file | the agent's state is in a docker volume rather than a host path, so the credentials directory the daemon mounted was an empty one it created | Unchecked — the docker daemon creates an empty directory rather than failing | mount a real host directory at the same path inside and outside, with the workdir a child of it |
| Container dies with permission denied on its credentials | uid mismatch between your image and the agent process | Unchecked — nothing compares the two uids or warns | rebuild with the agent's uid, usually 10001 |
| Node fails with "`LSPO_CREDENTIALS` is not set" | your code reads the wrong variable name | Unchecked — the platform sets `LSPO_CREDENTIALS_FILE` and cannot police how you read it | read `LSPO_CREDENTIALS_FILE` |
| Run fails with "no completion marker" | your container exited 0 without writing `__lspo_complete.json` | RULE — a marker is required on a successful run | write one before exiting 0. (*RECOMMENDATION, not part of the rule*: write one on your own failure path too, so your `error` and your part-finished inventory survive. It will not save a run an operator stopped while it was still running, nor one whose running container was stopped by the runtime budget — neither of those collects anything today) |
| Run fails naming a hash or size mismatch | the object changed after you hashed it, or the marker was written before the upload finished | RULE — every published object is re-read and held to the marker | hash the bytes you actually wrote. (*RECOMMENDATION, not part of the rule*: write the marker last — nothing observes write order, so this failure is the only symptom you will ever see of getting it wrong) |
| Uploads start failing partway through a long run | the credentials envelope expired, roughly fifteen minutes in | BEHAVIOUR — normally visible only once somebody raises the node's `timeout_seconds` past 900, because on a default registration the credentials expire at the same moment the container is asked to stop. Not exclusively, though: the stop has a grace period, so a node that handles the stop and writes its marker on the way out hits the expiry inside it even on the default | re-read the credentials file at or near `expires_at` |
| Upload refused with a policy error | the object key does not start with `staging.post.key_prefix`, or the object is over 1 GiB | RULE — enforced by the storage service, so the refusal is an HTTP error | prefix the key explicitly; split the object |
| The container is stopped at almost exactly fifteen minutes and the run then sits at "Waiting for runner" forever | the runtime budget the revision declared (900 seconds by default) ran out | BEHAVIOUR — a SIGTERM, then a SIGKILL that arrives well inside the thirty seconds it advertises, and a terminal report the orchestrator refuses because the lease was shortened to end at the deadline. Nothing is collected, and a run stopped this way is not marked failed either ([PROTOCOL.md](PROTOCOL.md#7-cancellation)) | set `timeout_seconds` on the pipeline node, or publish a revision declaring more, so the step finishes on its own; then cancel the parked run to release its quota slot. A SIGTERM handler makes the container exit cleanly, but it does not make this case recoverable |
| The container is killed with no warning and nothing is collected | the job was fenced. Six triggers, listed in [PROTOCOL.md](PROTOCOL.md#71-fencing-the-stop-with-no-grace-period-at-all); the common two are an orchestrator unreachable for about six minutes, and a job revoked while it ran. **An operator pressing Cancel produces this same symptom**, and is the commonest cause of it; a step running out of its runtime budget produces it too, from a second or so into the SIGTERM grace | BEHAVIOUR — a SIGKILL with no grace period, so no further marker can be written. Three of the six leave the agent able to report, and where that report is accepted, collection runs and reads any valid marker that was already in staging — which under the write-the-marker-last discipline is usually, not certainly, none; on the other three the execution stays parked until an operator cancels it, and cancelling collects nothing either | check the agent's connectivity and the agent's own log for the fence reason; nothing in the node can prevent this |
| A stopped run keeps going, then dies | no SIGTERM handler, so PID 1 discarded the signal and the grace ran out — or was cut short by the platform's next heartbeat | BEHAVIOUR (discarding the signal is a property of Linux, not of the platform) | install a handler that sets a flag |
| A cancelled run's partial output does not reach anything downstream | nothing was collected, because cancelling a step whose container is **still running** does not read its marker | BEHAVIOUR — by design, on both branches of the cancellation race. One exception, and it is not about a running container: a cancellation arriving while collection is **already under way** lets that collection finish, and what it verified is attached to the cancelled run as diagnostics under no output port — visible in the artifact browser, readable by nobody downstream ([PROTOCOL.md](PROTOCOL.md#7-cancellation)) | nothing to fix in the node. If partial output matters, let the step finish or fail on its own rather than cancelling it |
| Logs stop partway through | you exceeded the shipping rate, or a single line exceeded 64 KiB and its tail was discarded | BEHAVIOUR — the excess is dropped silently | fewer, shorter lines |
| Logs never appear at all | a logging library that defaults to WARNING and to stderr only, or a buffered stdout | Unchecked | configure the logger explicitly and set `PYTHONUNBUFFERED=1` |
| Log lines arrive mangled, with stray `[31m` in them | you printed ANSI colour; the escape byte is stripped as a control character and the rest survives | BEHAVIOUR | print plain text |
| The node runs but downstream steps see nothing | the objects were inventoried but claimed under no output port | BEHAVIOUR — an unclaimed object is verified and kept, but not offered downstream | claim them under a port; `produced_ports` is what becomes downstream artifacts |
| A configuration key on the node seems to do nothing | unknown keys are accepted and ignored | BEHAVIOUR — validation ignores what it does not recognise | check the spelling against the configuration table above |
| The disk on the agent's machine fills up | inputs downloaded and never deleted; no storage limit exists | Unchecked | delete each input when done; see the residual limits above |
