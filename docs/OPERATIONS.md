# OPERATIONS: registering, running, and the limits of this release

**Background**, about this document: it is an appendix. Nothing here changes how you write
a node; all of it changes whether the node you wrote ever runs.

Labels as elsewhere — **RULE**, **BEHAVIOUR**, **RECOMMENDATION** — and this line is
**background** about them.

---

## Before anything

**BEHAVIOUR.** External nodes are behind a feature flag that is **off by default**
(`LSPO_EXTERNAL_NODES_ENABLED`, `lspo/settings/base.py`). On a deployment where it is
off, none of the registration endpoints answer and no external node runs.

**BEHAVIOUR.** The code supports customer-run nodes and hosted builds. Availability
depends on the installation's flags; it is not restricted to dev or staging. There is no
hostile-code sandbox around your container: it runs with the docker daemon's ordinary privileges on the machine hosting the
agent, with `no-new-privileges` set and the resource ceilings in
[PROTOCOL.md](PROTOCOL.md#32-what-your-container-may-reach), and the platform's own
position is that this is the customer's trusted code rather than hostile code.

---

## Registering a node

**BEHAVIOUR.** Settings → **External Nodes** → **Connect node** offers image
registration and, when enabled, hosted builds. The image path takes a name and a
digest-pinned image and creates or reuses a pool, deployment and revision. Registration
does not start an agent or execute a pipeline.

### From the web interface

**BEHAVIOUR.** Image registration returns the agent start command and, only when it
minted one, the pool's registration token. The token is shown once; only its hash is
stored. Reusing a pool normally returns `PASTE_THE_POOL_TOKEN_HERE` in the command.
Use the saved token or deliberately rotate it. The **Runners** column shows enrolled
agents and whether they are online.

**BEHAVIOUR.** Registering the same image under the same deployment reuses its immutable
revision. Changing the requested timeout alone does not edit that revision. Moving a
deployment away from a pool with enrolled agents requires explicit confirmation because
those agents do not move with it (`noderegistry/services.py`, `noderegistry/serializers.py`).

### From a shell on the orchestrator

**BEHAVIOUR.** The management command uses the same image-registration service:

```bash
python manage.py external_demo_setup \
  --image '<registry>/my-node@sha256:<64 lowercase hex>' \
  --api-base 'https://orchestrator.example.com' \
  --organization '<organization slug or id>'
```

**BEHAVIOUR.** `--organization` can be omitted only when the command can resolve the
single organization. Set `--api-base` to the address the agent can actually reach: the
command's default is a local development address. The command prints the deployment id
and agent start command; it prints a registration token only when newly minted.
`--timeout-seconds` defaults to **900**. `--rotate-token` deliberately replaces the
registration token; `--confirm-pool-move` permits moving away from an occupied pool.

**BEHAVIOUR.** The command's printed step 2 still says there is no user interface for
external nodes. That instruction is stale: use the Settings registration tab and the
pipeline editor's **Add node → External → External node** entry described below.

**BEHAVIOUR.** Rotating the pool token, or disabling new registrations, does not stop
already enrolled agents. They authenticate using their own runner tokens. Retiring a
runner, deactivating its pool, or deactivating the owning organization removes that
authority (`runners/auth.py`, `runners/enrollment.py`, `noderegistry/services.py`).

### The image reference

**RULE.** Digest-pinned, in one of two spellings: `registry/name@sha256:<64 hex>` or a bare
`sha256:<64 hex>` image id. A tag is refused.

```bash
# with a registry
docker build -t <registry>/my-node:dev .
docker push <registry>/my-node:dev
docker inspect --format='{{index .RepoDigests 0}}' <registry>/my-node:dev

# one machine, no registry
docker build -t my-node:dev .
docker image inspect --format='{{.Id}}' my-node:dev     # -> sha256:...
```

**BEHAVIOUR.** A bare image id produces a warning at registration and is accepted. Nothing
re-checks it afterwards, and nothing counts the runners in the pool, so a bare id on a
multi-machine pool simply fails on every host that does not already hold the image, with a
missing-image error rather than anything that names the real cause.

---

## Hosted builds

**BEHAVIOUR.** Hosted mode builds your repository with AWS CodeBuild, pushes the image
to the installation's private ECR registry, and runs it on an existing managed pool.
It requires both `LSPO_EXTERNAL_NODES_ENABLED=true` and
`LSPO_HOSTED_BUILDS_ENABLED=true`. Unlike customer-run mode, the platform's build service
reads and executes your repository's Dockerfile. The same workload contract applies
(`noderegistry/building.py`, `noderegistry/hosted_views.py`).

**BEHAVIOUR.** Choose the repository build path in **Connect node**, supply an approved
HTTPS repository URL, choose a managed pool, and optionally select a branch or tag.
A blank ref uses the repository's default branch. The build uses the root Dockerfile;
the platform supplies its own build instructions, rather than running a repository
`buildspec.yml`. A successful build records the resolved commit and publishes a
digest-pinned revision. Until the first build succeeds there is no runnable revision;
a failed rebuild leaves the previous revision selected.

**RULE.** The repository URL must match `LSPO_HOSTED_REPO_ALLOWLIST`. URLs carrying
credentials, query strings or fragments are refused. Private repository access belongs
to the build project's connection, not to a token embedded in the URL
(`noderegistry/building.py`, `validate_repo_url`).

**RULE.** Raw 40-character commit hashes, pull-request refs (`pr/…`, `pull/…`) and
`refs/…` namespaces are refused; use a plain branch or tag. The resolved commit is recorded
on the build. Hosted builds also refuse a requested node runtime above **129,600 seconds**
(`MAX_NODE_TIMEOUT_S`, 2160 minutes); the default is 900 seconds
(`noderegistry/building.py`, `validate_requested_ref`, `_node_runtime_budget`).

**BEHAVIOUR.** Managed pools must already exist and have usable capacity. This flow
does not give the author a customer-agent enrollment command. Building and running are
separate operations; a successful build does not prove that a runner is online.

**RECOMMENDATION.** Use the platform's
[hosted-node runbook](https://github.com/HumanSignal/orchestrator/blob/master/docs/hosted-nodes.md)
for build infrastructure and managed-pool administration. Keep this template's
Dockerfile at the repository root and test the image locally before requesting a build.

---

## Starting the agent

**BEHAVIOUR.** The **agent** is our process, running on your machine. It is the only thing
that talks to the orchestrator: it enrols once, polls for work, starts your container,
streams its logs back and reports the result. It listens on no port; every connection is
outbound.

**BEHAVIOUR.** The generated command uses
**`ghcr.io/humansignal/lspo-agent:latest`**. Its initial pull checks access from the
agent's machine before replacing the existing container. Registry access and the
installation's current image are operational facts to verify on that machine.

Both the Connect reply and the setup command print the start line already filled in —
prefer either of those, because they carry the real pool name and, when that registration
minted one, the real token. The general form:

```bash
mkdir -p "$HOME/lspo-agent" && chmod 700 "$HOME/lspo-agent"

# Safe to re-run: it fetches a newer agent image and replaces the running agent. A failed fetch changes nothing.
docker pull -q ghcr.io/humansignal/lspo-agent:latest &&
c=$(docker ps -aq -f 'name=^/?lspo-agent$') &&
if [ -n "$c" ]; then docker stop -t 20 "$c" && docker rm "$c"; fi &&
docker run -d --name lspo-agent \
  --user "$(id -u):$(id -g)" \
  --security-opt label=disable \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --group-add $(getent group docker | cut -d: -f3) \
  -v "$HOME/lspo-agent:$HOME/lspo-agent:z" \
  -e LSPO_AGENT_WORKDIR="$HOME/lspo-agent/state" \
  -e LSPO_AGENT_API_URL=https://orchestrator.example.com \
  -e LSPO_AGENT_POOL=self-hosted \
  -e LSPO_AGENT_REGISTRATION_TOKEN=PASTE_THE_POOL_TOKEN_HERE \
  -e LSPO_AGENT_NAME=$(hostname) \
  -e LSPO_AGENT_MAX_CONCURRENT_JOBS=1 \
  --stop-timeout 300 \
  ghcr.io/humansignal/lspo-agent:latest
```

**BEHAVIOUR.** On SELinux hosts, the shared `:z` label lets the agent and job
containers reach the same state tree; `--security-opt label=disable` applies to the
agent that controls the host Docker socket. The platform also labels workload mounts.
These flags match `noderegistry/services.py` and `agent/executors/docker_exec.py`;
do not substitute a private `:Z` label on this shared state directory.

Three of those values are placeholders and the printed command has them filled in: the
orchestrator's address, the pool name, and the token — see the registration section above
for when the reply carries a token and when it does not.

**BEHAVIOUR.** The block supports repeat invocation: it fetches the image, stops and
removes the agent that is already running, and starts a fresh one. With a job in flight,
`docker stop -t 20` gives the old agent only 20 seconds before a forced kill. A surviving
job container may need adoption by the replacement agent, subject to its current lease
and authority; repeatability does not guarantee an uninterrupted job. Drain active jobs
before a planned replacement. On a machine that has never run it, the two middle lines find nothing and say
nothing. Before they existed, the second paste ended in `docker: Error response from daemon:
Conflict. The container name "/lspo-agent" is already in use`, which is an error message about
a machine that was working perfectly.

**BEHAVIOUR.** If the fetch fails, nothing is taken away. No network, an unreachable registry,
a package whose visibility was changed back — in every one of those you see docker's own pull
error and keep the agent you already have, running, untouched. That holds at a shell prompt as
well as inside a script, and it is the `&&` at the end of those lines that makes it hold: an
interactive shell has no `set -e`, so without the chain a failed pull would stop nothing, and
the block would go on to remove a working agent and then start a stale cached image or none at
all. The same conditionality covers the later steps: a stop that did not work removes nothing,
a removal that did not work starts nothing.

**RECOMMENDATION.** Quote those lines into your own runbook as they are — one chain, and the
filter still in its single quotes. Splitting the chain into separate lines reads the same and
looks tidier, and it is the version that destroys a healthy agent the first time a registry is
unreachable; dropping the quotes leaves `?` as a shell glob, so a matching filename in the
working directory is handed to docker in place of the filter.

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

**BEHAVIOUR, for all four bullets below.** Four parts of the `docker run` line itself are
load-bearing — the `&&` chain above it is a fifth — and each one fails in its own way when it
is wrong (`agent/README.md`, "Run it"). None of them is a duty on your node — they are the
operator's, and they are here because your node is what visibly breaks:

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
(`agent/identity.py`).

### Agent settings that change what your node sees

**BEHAVIOUR, for the table below.** All are `LSPO_AGENT_*` and all are the agent operator's
to set, not yours (`agent/config.py`). The defaults are what your node
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
ceiling at all (`agent/executors/docker_exec.py`).

---

## Which user your image runs as

**BEHAVIOUR.** Your image may run as **any user it likes**, and no uid has to match
anything on the agent's side. Each job's credentials directory is created on the agent's
disk mode `0711` — anyone may walk through it, only the agent may list it — with the file
inside it mode `0444`, readable by every uid and writable by none, and **both modes are
re-applied on every write**, a mid-job credential refresh included
(`agent/creds.py`, `CREDS_DIR_MODE` / `CREDS_FILE_MODE` /
`JobCredentials.write`). What keeps the credential off the rest of the machine is the
agent's own working directory **above** the mounted leaf, which is owner-only and is
bind-mounted into nothing (`agent/identity.py`, `ensure_private_workdir`) — not the
file's own mode.

**BEHAVIOUR.** In object-storage mode the agent does **not** force your container's user,
so your image runs as its own `USER`, and the file is deliberately readable by every uid
so that this stays safe to do (`agent/runner.py` `_local_staging_mounts`).

**RULE, and it is the only thing the mode bits still ask of you — enforced by the kernel
rather than by any check the platform makes on your node.** Open the exact path the
agent gives you in `LSPO_CREDENTIALS_FILE`. Do **not** list the directory it is in: `0711`
grants traversal, not enumeration, so `os.listdir("/lspo/creds")` is a permission error for
every user except the agent. Nothing needs enumeration — you were told the name.

**RECOMMENDATION.** Run as a non-root user of your own choosing. Not because a permission
depends on it — none does — but because there is no sandbox around your container, so a
root workload is a root process on somebody else's machine for no benefit.

### What this replaced, because a document that changed its mind owes you the reason

Earlier revisions of this page, of `PROTOCOL.md` and of the example `Dockerfile` told you
to build your image as **uid 10001**, and that instruction is now wrong in both of its
halves. Following it is what would hurt you, so it is worth being explicit about what
changed rather than quietly deleting it.

* **The file was `0600` in a `0700` directory owned by the uid the agent ran as.** An
  image declaring any other user got a permission error on its own credentials, and the
  only repair a customer could find was to run their container as root — the platform
  punishing the careful choice. The modes above replaced that: confidentiality now comes
  from an ancestor nobody else can traverse, which is a property the workload's uid cannot
  affect.
* **"The agent is uid 10001" was never something you could rely on, and is no longer even
  the common case.** That is the account the agent's own image declares, but the command
  an operator is given starts the agent with `--user "$(id -u):$(id -g)"`, so a deployed
  agent runs as the person who pasted it. Two independent numbers were being treated as
  one.

**BEHAVIOUR, local demo mode only.** With local-path staging the agent forces your
container to its own uid and gid with no supplementary groups, so your image's user is
ignored entirely — the opposite problem, and it is still live. Anything that writes under
that user's home directory works in production and fails in the demo. Use `/tmp` or the
staging directory.

---

## Pointing a pipeline node at your deployment

**BEHAVIOUR.** In the pipeline editor: **Add node → External → External node**, pick the deployment you
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
did set (`pipelines/config_schemas.py`).

| Key | Type | Required | Meaning | Reaches your container |
|---|---|---|---|---|
| `step_kind` | `"external"` | yes | discriminator | no |
| `external_deployment_id` | integer > 0 | **yes** | which registered deployment runs. Booleans are rejected explicitly | only through its effects |
| `params` | object | no, default `{}` | **the only node configuration your code ever sees**, copied verbatim | **yes** |
| `timeout_seconds` | integer > 0 or null | no | requested runtime budget. **Set this to give a node more than the 900 seconds its revision declares** — it wins over the revision and takes effect at once | **yes** |
| `queue_timeout_seconds` | integer > 0 or null | no | how long the job stays claimable, default 3600 | no |
| `input_payload_kind` | string or null | no | require an upstream artifact of this kind; absent means every upstream artifact, including none | indirectly |

**RULE.** New API writes reject unknown top-level external-step configuration keys.
A typo such as `timeout_second` cannot be saved as a supported setting. Put the
container's own options inside `params`, whose shape the platform does not validate.
Older stored nodes with stray keys can still run; the handler ignores those keys
(`pipelines/config_schemas.py`, `handlers/steps/external.py`).

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
(`noderegistry/models.py`):

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
  you will see — and a deadline stop whose agent cannot report can end up there too, as described
  below.
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
* **Runtime expiry has a reporting window, not automatic recovery.** The default stop
  window gives the workload a configured 180-second stop allowance and the agent a
  further 60 seconds to report. A valid report accepted during that window fails the
  run and can salvage a valid marker's objects as diagnostics. If the agent loses its
  lease or cannot report before the window closes, the run can still remain parked.
  The envelope expiry can be clamped to the reporting cutoff; it does not identify
  the workload's earlier kill cutoff, and the stop instruction is not forwarded. See
  [PROTOCOL.md](PROTOCOL.md#7-cancellation).

* **No automatic retry.** Every failed external attempt is recorded as transient
  regardless of your exit code, and nothing re-attempts. An operator retries by hand.
* **No aggregate upload quota.** One object is bounded at 1 GiB. Nothing caps the number of
  objects or the total bytes a container may upload before its credentials expire.
* **No limit on the SIZE of inputs, and an indirect one on their number.** Nothing caps one
  input object or their total bytes. The count, however, is capped after all: every input
  and its metadata are written into the job description, and the writer refuses to publish
  one over 8 MiB (`external/io.py`). Measured against the real models, that is
  roughly **25,000 inputs** with 95-character URIs, at about 330 bytes each. A step wired
  downstream of something that produced ten thousand files runs fine; one wired downstream
  of something that produced fifty thousand fails **before any container starts**, while
  the orchestrator is still writing the job description. See
  [PROTOCOL.md](PROTOCOL.md#23-reading-the-input-objects).
* **No disk ceiling on the container.** The agent sets a CPU limit, a memory limit and a
  process limit, and no storage limit (`agent/executors/docker_exec.py`). A node
  that downloads its inputs to `/tmp` and never deletes them can fill the disk of the
  machine hosting the agent — which is a customer's machine, running their other work.
  Nothing warns, and the first symptom is unrelated things failing on that host.
* **Presigned reads can outlive their stated expiry.** A presigned GET can remain valid
  past the envelope's `expires_at` by up to the budget left when it was signed. The upload
  policy cannot. Treat `expires_at` as exact for writes, and as a lower bound for reads.
* **The terminal log is not a complete transcript.** The live stream retains 1000
  entries. The durable file merges that tail with lines already saved on the execution;
  it cannot recover lines neither source retained. A node can upload its own complete
  log as an inventoried object.
* **A fenced container is killed outright.** The runtime deadline normally begins with a
  SIGTERM, though it need not; the fencing path gives the container a SIGKILL and nothing
  else, always. It therefore
  writes no further marker, and what is collected afterwards is only what a valid marker
  for that attempt had already recorded — under the recommended write-the-marker-last
  discipline, usually nothing. Usually and not always: the marker is written last, but
  it does not vanish at the instant the process does, and a fence landing between its
  upload and the agent seeing the container go collects what it names. Fencing triggers differ in whether the agent may even report the outcome; the
  account in [PROTOCOL.md](PROTOCOL.md#71-fencing-the-stop-with-no-grace-period-at-all)
  explains that report permission depends on the cause. Note that an ordinary agent shutdown is
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
tail of the last **1000 entries** and drops the oldest when a run is chattier than that.
The durable copy merges the retained tail with lines already saved on the execution (see
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
| Container dies with permission denied on its credentials | **not** a uid mismatch — the file is `0444` in a `0711` directory, so any user can open it. Either you listed the directory instead of opening the path (`0711` grants traversal, not enumeration), or your image makes `/lspo` itself non-traversable and has defeated its own mount, or the agent's state lives somewhere its modes are not enforced (a CIFS/SMB share, Docker Desktop file sharing, some NFS exports) | Unchecked in your container; the agent refuses at startup for the cases it can detect | open the exact path in `LSPO_CREDENTIALS_FILE` rather than listing its directory; check nothing in your image narrows `/lspo`. Do **not** rebuild as a particular uid — no uid is required, and do not "fix" it by running as root |
| Node fails with "`LSPO_CREDENTIALS` is not set" | your code reads the wrong variable name | Unchecked — the platform sets `LSPO_CREDENTIALS_FILE` and cannot police how you read it | read `LSPO_CREDENTIALS_FILE` |
| Run fails with "no completion marker" | a successful report had no usable `__lspo_complete.json` | RULE — success requires a valid marker | write one before exiting successfully. Also attempt a marker on failure so accepted reports can salvage inventoried files |
| Run fails naming a hash or size mismatch | the object changed after you hashed it, or the marker was written before the upload finished | RULE — every published object is re-read and held to the marker | hash the bytes you actually wrote. (*RECOMMENDATION, not part of the rule*: write the marker last — nothing observes write order, so this failure is the only symptom you will ever see of getting it wrong) |
| Uploads start failing partway through a long run | the workload cached an expired envelope, or the agent could not refresh it | BEHAVIOUR — envelopes have a default 900-second lifetime even when the runtime budget is longer | re-read the credentials file near `expires_at`; inspect the agent log if the file did not change |
| Upload refused with a policy error | the object key does not start with `staging.post.key_prefix`, or the object is over 1 GiB | RULE — enforced by the storage service, so the refusal is an HTTP error | prefix the key explicitly; split the object |
| The container stops near its runtime budget | the configured `timeout_seconds` elapsed | BEHAVIOUR — the default stop window permits a failure report and diagnostic salvage; a lost lease or closed window can still leave the run parked | give the node enough budget, inspect the agent log and failure reason; cancel a parked execution to release held quota |
| The container is killed with no warning and nothing is collected | operator cancellation or a fence, such as a lost lease or refused agent identity | BEHAVIOUR — a SIGKILL offers no chance to write a marker; collection depends on an accepted report and an existing valid marker | check the agent log and connectivity; see [PROTOCOL.md](PROTOCOL.md#71-fencing-the-stop-with-no-grace-period-at-all) |
| A stopped run keeps going, then dies | no SIGTERM handler, so PID 1 discarded the signal and the grace ran out, or was shortened by a later stop instruction | BEHAVIOUR (discarding the signal is a property of Linux, not of the platform) | install a handler that sets a flag |
| A cancelled run's partial output does not reach anything downstream | nothing was collected, because cancelling a step whose container is **still running** does not read its marker | BEHAVIOUR — by design, on both branches of the cancellation race. One exception, and it is not about a running container: a cancellation arriving while collection is **already under way** lets that collection finish, and what it verified is attached to the cancelled run as diagnostics under no output port — visible in the artifact browser, readable by nobody downstream ([PROTOCOL.md](PROTOCOL.md#7-cancellation)) | nothing to fix in the node. If partial output matters, let the step finish or fail on its own rather than cancelling it |
| Logs stop partway through | you exceeded the shipping rate, or a single line exceeded 64 KiB and its tail was discarded | BEHAVIOUR — buffer overflow emits a warning with the dropped count; oversized stream lines carry `…[truncated]`. Later transport or retention loss can also lose those notices | fewer, shorter lines; check the agent log and upload an inventoried log file when a complete transcript matters |
| Logs never appear at all | a logging library that defaults to WARNING and to stderr only, or a buffered stdout | Unchecked | configure the logger explicitly and set `PYTHONUNBUFFERED=1` |
| A container's output is gone before you could read it | the agent removes a job's container when the job ends | BEHAVIOUR — exited containers are removed unless the agent enables `LSPO_AGENT_KEEP_CONTAINERS` | attach at the start instead: [Watching a run, live](#watching-a-run-live) |
| Log lines arrive mangled, with stray `[31m` in them | you printed ANSI colour; the escape byte is stripped as a control character and the rest survives | BEHAVIOUR | print plain text |
| The node runs but downstream steps see nothing | the objects were inventoried but claimed under no output port | BEHAVIOUR — an unclaimed object is verified and kept, but not offered downstream | claim them under a port; `produced_ports` is what becomes downstream artifacts |
| Saving external-node configuration rejects a key | unknown top-level configuration key | RULE — external configuration is closed at the API write boundary | check the table above; put container-specific options inside `params` |
| The disk on the agent's machine fills up | inputs downloaded and never deleted; no storage limit exists | Unchecked | delete each input when done; see the residual limits above |
