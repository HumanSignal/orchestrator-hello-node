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

**BEHAVIOUR.** Settings, then the **External Nodes** tab, then **Connect node**. Give the
node a name, paste the digest, and it creates the same three database rows the command
below does — a **pool**, a **deployment** and a **revision** — together with the
`docker run` line for an agent.

**BEHAVIOUR, and it is the ordinary case rather than the exception.** Registering again
does not duplicate anything: a pool of that name, a deployment of that name and a revision
already pinning that exact digest are each **matched and reused**. So re-registering a
rebuilt image is a normal act, not a mess to clean up.

**BEHAVIOUR, and it is the first place somebody following this page gets stuck.** The
reply shows the pool's registration token **only when that registration minted one**,
which normally means only when it just created the pool. It is shown once, because only
its hash is stored. Register a second node into a pool that already has a token and the
reply comes back **with no token at all**, and a command carrying the literal placeholder
`PASTE_THE_POOL_TOKEN_HERE` where the secret belongs. Nothing is broken. Either fill in
the token saved when that pool was created, or rotate the pool's token from the node list
and use the new one — rotating is safe, and what it does and does not affect is set out
below.

It does **not** create the agent. Nothing does: an agent comes into existence when
somebody runs that `docker run` line on a machine, and it enrols itself using the
registration token. Until then the deployment is registered and has nowhere to run, which
the screen shows as "no agent yet".

**BEHAVIOUR.** Once an agent has enrolled it appears against the node in the **Runners**
column of the External Nodes page. **A node that will not run is answered by that column
first**: no runner online means there is nothing to run it, and no amount of looking at
the pipeline will show you that.

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

**BEHAVIOUR.** The agent image is published at **`ghcr.io/humansignal/lspo-agent:latest`**,
and the package is **public**: an anonymous pull works and no `docker login` is needed.
`docker run` pulls it for you; `docker pull ghcr.io/humansignal/lspo-agent:latest` fetches
it on its own if you would rather do that first.

Both the Connect reply and the setup command print the start line already filled in —
prefer either of those, because they carry the real pool name and, when that registration
minted one, the real token. The general form:

```bash
mkdir -p "$HOME/lspo-agent" && chmod 700 "$HOME/lspo-agent"

docker run -d --name lspo-agent \
  --user "$(id -u):$(id -g)" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --group-add $(getent group docker | cut -d: -f3) \
  -v "$HOME/lspo-agent:$HOME/lspo-agent" \
  -e LSPO_AGENT_WORKDIR="$HOME/lspo-agent/state" \
  -e LSPO_AGENT_API_URL=https://orchestrator.example.com \
  -e LSPO_AGENT_NAME=$(hostname) \
  -e LSPO_AGENT_POOL=self-hosted \
  -e LSPO_AGENT_REGISTRATION_TOKEN=PASTE_THE_POOL_TOKEN_HERE \
  --stop-timeout 300 \
  ghcr.io/humansignal/lspo-agent:latest
```

Three of those values are placeholders and the printed command has them filled in: the
orchestrator's address, the pool name, and the token — see the registration section above
for when the reply carries a token and when it does not.

**BEHAVIOUR.** The first line is a prerequisite rather than decoration, and skipping it does
not fail where the mistake is. `docker run` needs the mount source to exist, and when it does
not, the daemon creates it — **as root**. The agent then cannot create its own state directory
inside it, which it has to do itself because it makes that directory owner-only and a `chmod`
needs ownership rather than write permission, and the container exits at startup with
"Operation not permitted", which names nothing anybody can act on.

**BEHAVIOUR.** `--user "$(id -u):$(id -g)"` runs the agent as the account that pasted the
command rather than as the service account its image declares, and that flag is what makes
`chmod 700` enough. Everything the agent keeps on the machine — its identity file, every
running job's credentials — then belongs to that account, and nothing here is shared with any
other account on the host. Earlier releases had no such flag: the agent ran as a uid that
belongs to nobody on the host, so the parent directory had to be world-writable (`chmod 1777`)
before the agent could create anything inside it, and a world-writable directory then had to be
policed, which is what the long host-side audit that used to be printed here existed for.
Running as the operator removes the world-writable directory, and the audit went with it.

**BEHAVIOUR, and it is what `--user` does not fix.** `0700` protects the directory; it does
not protect its **name**. If any directory above `$HOME/lspo-agent` can be written by other
accounts — a home directory at `0775` with a shared group, which some sites ship — a member of
that group can rename `lspo-agent` away and leave their own directory under the same name. The
running agent never notices: its bind mount stays attached to the original inode, so its
startup checks and every recheck go on passing, while the docker daemon resolves the
replacement **on the host and as root** every time it mounts a job's credentials. Nothing
checks that, and nothing prints a checker for you to run — not the block above, not the agent's
own startup log. One shipped for several releases and was withdrawn: every version of it was
wrong in the same family of ways, because a short check cannot soundly resolve a chain of
symbolic links — resolving a path to its canonical form is exactly what discards the directory
that *holds* each link — and the platform's position is that a checker it cannot vouch for is
worse than none (`agent/README.md`, "Run it").

**BEHAVIOUR for the first sentence below, RECOMMENDATION for the second.** They are the two
the orchestrator prints with the block, word for word:

> On a machine other accounts use, nothing here checks who can replace the agent's state
> directory or anything on the path to it — including the directories holding any symbolic
> links in that path. Satisfy yourself of that before you start, and prefer a machine you do
> not share.

**BEHAVIOUR, for all four bullets below.** Four parts of that command are load-bearing, and
each one fails in its own way when it is wrong (`agent/README.md`, "Run it"). None of them
is a duty on your node — they are the operator's, and they are here because your node is
what visibly breaks:

* **`--group-add`** with the host's docker group id. The agent image runs as a non-root
  user and has no access to the docker socket without it. The agent refuses to start
  rather than failing on its first job. It grants a supplementary **group**, and a group is
  independent of the uid, so `--user` above does not take the socket away — the two flags do
  not fight, they are answering different questions.
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
gets unless somebody changed them. The time-valued ones are **settings, not guarantees**:
they say how often something is attempted, never how long anything takes — least of all
stopping a container ([PROTOCOL.md](PROTOCOL.md#7-cancellation)).

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

In the pipeline editor: **Add node → External → External node**, pick the deployment you
registered, and wire an edge into it from the step whose output it should read. That is
the same thing as adding a script node with this configuration, which is what it stores
and what an API caller writes directly:

```json
{"step_kind": "external", "external_deployment_id": 12, "params": {}}
```

**BEHAVIOUR.** Whatever the node delivers through its output ports comes back as that
run's **artifacts**, and each one's kind is the **port name** it was delivered under. A
downstream step configured to read `output` finds exactly what was published under
`output` — which is why "the node ran but nothing downstream sees anything" is almost
always a port name that does not match, or an object the marker inventoried but claimed
under no port at all.

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
* **No bounded stop, on any path.** Nothing here guarantees how long it takes to stop a
  container: not the gap between a deadline passing and the first signal, not the time
  between that signal and the kill, not how soon a fence lands, not how long a container
  that should be gone can keep writing. Every interval is a timer of ours plus a docker
  daemon, an HTTP request that may be retried, and a periodic pass that may fail and be
  tried again with nothing capping the repeats. **Stop figures** do appear in these
  documents and in the settings table above — read those as settings and as typical values,
  never as limits, since a setting says how often something is attempted and never how long
  it takes. That reading is for stop durations only. The values the platform **stamps or
  enforces** are a different kind of number and are exact, and you should size against them
  exactly: the runtime budget, the moment a job's upload credentials stop working, the lease
  stamped when a job is claimed, the 1 GiB ceiling on a single object, the 8 MiB ceiling on
  a document. The full statement, and what it means for a node author, is at the top of
  [PROTOCOL.md](PROTOCOL.md#7-cancellation). For an operator the practical form is: never
  size a maintenance window, a drain or a redeploy on the assumption that a stop completes
  in a known time.
* **Hitting the runtime budget leaves a running container's run parked, and collects
  nothing.** The agent does stop the container when the budget runs out, but the
  orchestrator refuses the terminal report that would normally finish the job: the last
  heartbeat before the deadline shortened the lease to end at the deadline, and a report
  on an expired lease is turned away. Nothing arms collection, so no marker is read and
  nothing the step produced is published, and the execution stays at "Waiting for
  runner" until somebody cancels it. The stop begins politely, but the grace the agent
  asks docker for is cut short — the platform's own next heartbeat comes back refused and
  the agent kills the container. When that lands is not something a node or an operator
  can plan around; no interval on any stop path is bounded
  ([PROTOCOL.md](PROTOCOL.md#7-cancellation)). The one job that ends differently is one
  still being **prepared** when its budget runs out: the deadline is not being watched
  yet, the claim's own five-minute
  lease has not been shortened yet, and a failure there is reported and accepted, so the
  run finishes as failed rather than parking. Whether that one collects anything depends
  on where in preparation it died, and "it failed during preparation" does not answer
  that: the container is created and started **before** the agent's own log and
  heartbeat threads are. A failure in the earlier steps — an unreadable job description,
  a failed image pull — leaves no container and nothing to collect; a failure starting
  one of those threads, which a busy host really does produce, leaves the container
  running and still sends the report that arms collection. The start itself is the
  ambiguous middle: a `docker run` that raises may still have left a container running,
  which is why the agent goes and takes one down by name before it reports. Full
  derivation, and which half of it was executed rather than read, in
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
* **A fenced container is killed outright.** The runtime deadline normally begins with a
  SIGTERM, though it need not; the fencing path gives the container a SIGKILL and nothing
  else, always. It therefore
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

## Watching a run, live

**BEHAVIOUR, and it is why `docker logs` on its own is not the answer.** The agent removes
a job's container when the job ends. So the container whose output you want is gone by the
time you go looking for it, and the runs worth reading are exactly the short ones that went
wrong. To see a step's own output on the machine that ran it you have to attach at the
moment the container **starts**.

`logs.sh` in this repository does that, by watching the docker daemon's event stream:

```bash
./logs.sh agent    # the agent: claims, heartbeats, container lifecycle
./logs.sh node     # every job container the agent starts, from its first line
./logs.sh both     # the two interleaved and tagged (the default)
```

**BEHAVIOUR.** Job containers are named `lspo-<execution>-g<generation>` and carry the
labels the agent puts on them, including `lspo.agent` with the agent's own name — which is
what lets the script follow only the work this agent started on a machine running several.

**RECOMMENDATION.** Start it before you start the run rather than after. The interesting
part of a failing step is usually its first three lines, and those are the ones a container
that has already exited can no longer show you.

**BEHAVIOUR, and it decides which of the two journals to trust.** The same node output is
shipped to the orchestrator and appears live in the run view, which is the only option when
the agent is on somebody else's machine. It is not the same document: the platform keeps a
tail of the last **1000 lines** and drops the oldest when a run is chattier than that, and
the durable copy stored with the execution is built from that same trimmed buffer (see
[PROTOCOL.md](PROTOCOL.md#33-logging), which is also why a step should say the
important things once, at the end, in few lines). The copy on the machine is the whole of
it, for as long as the container lives.

**BEHAVIOUR.** The agent's own journal is an ordinary `docker logs -f lspo-agent`, and it
is the one that explains a run which never started at all: a refused image pull, a
credentials directory it could not write, an environment variable outside the allowlist.
None of those ever reach a container of yours, so none of them appears in a node's log.

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
| Uploads start failing partway through a long run | the credentials envelope expired, roughly fifteen minutes in | BEHAVIOUR — normally visible only once somebody raises the node's `timeout_seconds` past 900, because on a default registration the credentials expire at the same moment the container is asked to stop. Not exclusively, though: when the stop arrives politely there is an interval after it, so a node that handles the stop and writes its marker on the way out hits the expiry inside that interval even on the default | re-read the credentials file at or near `expires_at` |
| Upload refused with a policy error | the object key does not start with `staging.post.key_prefix`, or the object is over 1 GiB | RULE — enforced by the storage service, so the refusal is an HTTP error | prefix the key explicitly; split the object |
| The container is stopped at almost exactly fifteen minutes and the run then sits at "Waiting for runner" forever | the runtime budget the revision declared (900 seconds by default) ran out | BEHAVIOUR — a SIGTERM, then a SIGKILL once the platform's next heartbeat comes back refused, or docker's own kill at the end of the agent's 30 second stop timeout if no refusal arrives first. Which of the two lands, and when, is not bounded ([PROTOCOL.md](PROTOCOL.md#7-cancellation)). Then a terminal report the orchestrator refuses, because the lease was shortened to end at the deadline. Nothing is collected, and a run stopped this way is not marked failed either ([PROTOCOL.md](PROTOCOL.md#7-cancellation)) | set `timeout_seconds` on the pipeline node, or publish a revision declaring more, so the step finishes on its own; then cancel the parked run to release its quota slot. A SIGTERM handler makes the container exit cleanly, but it does not make this case recoverable |
| The container is killed with no warning and nothing is collected | the job was fenced. Six triggers, listed in [PROTOCOL.md](PROTOCOL.md#71-fencing-the-stop-with-no-grace-period-at-all); the common two are an orchestrator the agent cannot reach for longer than the job's lease plus the agent's `LEASE_EXPIRY_GRACE_S`, and a job revoked while it ran. **An operator pressing Cancel produces this same symptom**, and is the commonest cause of it. A step running out of its runtime budget produces it too, by a different route: the stop starts politely, and the platform's next heartbeat comes back refused and turns it into a kill — or, when the agent cannot reach the orchestrator at all and no refusal ever arrives, docker's own kill at the end of the stop timeout does it instead. **None of those arrivals is bounded in time**, and none of the intervals above should be planned around ([PROTOCOL.md](PROTOCOL.md#7-cancellation)) | BEHAVIOUR — a SIGKILL with no grace period, so no further marker can be written. Three of the six leave the agent able to report, and where that report is accepted, collection runs and reads any valid marker that was already in staging — which under the write-the-marker-last discipline is usually, not certainly, none; on the other three the execution stays parked until an operator cancels it, and cancelling collects nothing either | check the agent's connectivity and the agent's own log for the fence reason; nothing in the node can prevent this |
| A stopped run keeps going, then dies | no SIGTERM handler, so PID 1 discarded the signal and the grace ran out — or was cut short by the platform's next heartbeat | BEHAVIOUR (discarding the signal is a property of Linux, not of the platform) | install a handler that sets a flag |
| A cancelled run's partial output does not reach anything downstream | nothing was collected, because cancelling a step whose container is **still running** does not read its marker | BEHAVIOUR — by design, on both branches of the cancellation race. One exception, and it is not about a running container: a cancellation arriving while collection is **already under way** lets that collection finish, and what it verified is attached to the cancelled run as diagnostics under no output port — visible in the artifact browser, readable by nobody downstream ([PROTOCOL.md](PROTOCOL.md#7-cancellation)) | nothing to fix in the node. If partial output matters, let the step finish or fail on its own rather than cancelling it |
| Logs stop partway through | you exceeded the shipping rate, or a single line exceeded 64 KiB and its tail was discarded | BEHAVIOUR — the excess is dropped silently | fewer, shorter lines |
| Logs never appear at all | a logging library that defaults to WARNING and to stderr only, or a buffered stdout | Unchecked | configure the logger explicitly and set `PYTHONUNBUFFERED=1` |
| A container's output is gone before you could read it | the agent removes a job's container when the job ends | BEHAVIOUR — nothing is retained locally afterwards | attach at the start instead: [Watching a run, live](#watching-a-run-live) |
| Log lines arrive mangled, with stray `[31m` in them | you printed ANSI colour; the escape byte is stripped as a control character and the rest survives | BEHAVIOUR | print plain text |
| The node runs but downstream steps see nothing | the objects were inventoried but claimed under no output port | BEHAVIOUR — an unclaimed object is verified and kept, but not offered downstream | claim them under a port; `produced_ports` is what becomes downstream artifacts |
| A configuration key on the node seems to do nothing | unknown keys are accepted and ignored | BEHAVIOUR — validation ignores what it does not recognise | check the spelling against the configuration table above |
| The disk on the agent's machine fills up | inputs downloaded and never deleted; no storage limit exists | Unchecked | delete each input when done; see the residual limits above |
