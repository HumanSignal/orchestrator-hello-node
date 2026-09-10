# PROTOCOL: the contract, in the order one attempt happens

This preamble is **background**. What follows is the normative document, and it follows the
life of a single container attempt: bootstrap, inputs, work, outputs, the completion
marker, and how the attempt ends.

Every statement is labelled **RULE** (the platform refuses or fails the run),
**BEHAVIOUR** (what the platform does, which you must plan for) or **RECOMMENDATION**
(what a good node does; the platform permits otherwise). Unlabelled text is background.

Also **background**: code claims and citations were rechecked against the orchestrator at
default-branch commit `30b0950a` (`master`, reviewed 2026-09-10). Historical measurements
retain their original dates and commits; they were not all rerun. Source paths give provenance; [README.md](README.md#provenance) records the scope.

---

## 0. The shape of one attempt

```
orchestrator                                                     your container
------------                                                     --------------
writes invocation.json into a staging prefix
issues a credentials envelope (short-lived)
   |
   |  agent (our process, your machine)
   |    claims the job, writes creds.json to a directory,
   |    starts your image with that directory mounted read-only
   v
                                            reads LSPO_CREDENTIALS_FILE
                                            reads the credentials envelope
                                            fetches invocation.json
                                            fetches and verifies each input
                                            does the work
                                            uploads output objects
                                            uploads __lspo_complete.json  <- last
                                            exits with a code
   |
   v
reads the marker, copies every object it lists into a
published area, re-reads each one and holds it to the
hash and size the marker promised
```

**RULE for the names in the table below; BEHAVIOUR for the "Required" column.** Four
documents live in the staging prefix and their names are fixed
(`external/contract.py`). You locate them by name, never by configuration, and so
does the platform: collection joins your staging prefix to the literal string
`__lspo_complete.json` and reads whatever is there (`external/io.py`). A receipt
written under any other name therefore does not exist as far as the platform is concerned,
and a run that exited 0 fails for having no marker.

| Filename | Written by | Required |
|---|---|---|
| `invocation.json` | orchestrator | always present |
| your output objects | you | as many as you like, including none |
| `result.json` | you | optional, and **nothing reads it** (see 4.4) |
| `logs.ndjsonl` | you | optional |
| `__lspo_complete.json` | you | see section 5 |

---

## 1. Bootstrap: what the container starts with

### 1.1 Environment variables

**BEHAVIOUR.** The agent injects these nine variables, in addition to the image's own
environment and any variables the deployment declared and the agent's operator allowed
(`agent/runner.py`):

| Variable | Value | Use it for |
|---|---|---|
| `LSPO_CREDENTIALS_FILE` | path to the credentials file inside the container. Always `/lspo/creds/creds.json` today, because both envelope builders write that one constant, but read the variable rather than hardcoding the value | **this is your bootstrap** |
| `LSPO_JOB_ID` | the agent-side job id | logging |
| `LSPO_EXECUTION_ID` | the orchestrator execution this attempt belongs to | logging, cross-checks |
| `LSPO_ATTEMPT` | 1-based attempt counter | logging, cross-checks |
| `LSPO_GENERATION` | fencing token for this physical try | logging, cross-checks |
| `LSPO_IDEMPOTENCY_KEY` | stable key for this logical unit of work, of the form `execution-<id>-attempt-<n>` | de-duplicating your own side effects |
| `LSPO_CONTRACT_VERSION` | contract version of the job description. Always `1` today, because the orchestrator stamps the current version rather than anything a deployment declared | refusing a version you do not implement |
| `LSPO_INVOCATION_URI` | the true storage address of `invocation.json`, for example `s3://bucket/key` | **information only, see below** |
| `LSPO_STAGING_PREFIX` | the true storage address of your staging area | **information only, see below** |

**BEHAVIOUR.** The only credentials variable the agent sets is `LSPO_CREDENTIALS_FILE`
(`agent/runner.py`). There is no variable called `LSPO_CREDENTIALS`. The agent does
not set one, and no commit in the orchestrator's history ever added one to the agent — that
second half is a search of the repository's history rather than a reading of one commit,
and it is stated separately for that reason.

**RECOMMENDATION, with no working alternative.** Read your credentials path from
`LSPO_CREDENTIALS_FILE`. A node that reads `LSPO_CREDENTIALS` works only because its own
Dockerfile happens to define that name, and it dies the moment the line is dropped. No
platform check refuses you here; you simply have nothing to read. (The `node.py` in this
repository had exactly that defect, propped up by a line in its own `Dockerfile` that
defined the name; both are gone. [CONFORMANCE-BASELINE.md](../CONFORMANCE-BASELINE.md)
records what the mistake cost when it was measured.)

**BEHAVIOUR.** `LSPO_INVOCATION_URI` and `LSPO_STAGING_PREFIX` are addresses, not access.
They are usually `s3://...` URIs, and your container holds no AWS identity, so it cannot
read or write them directly. They are useful in a log line and useless as an API. Read the
job description and write your outputs through the credentials envelope instead
(section 1.2).

**RULE.** Environment variables your deployment declares are named in the job description
but valued by the machine the agent runs on, and the agent's operator keeps an allowlist
(`LSPO_AGENT_ALLOWED_ENV`, exact names or glob patterns, empty by default which means
nothing is passed). A declared name that is not on the allowlist **fails the job before
your container starts**, naming the variable (`agent/runner.py`,
`agent/config.py`). A name that is allowed but simply absent from the agent's
environment produces a warning and the variable is not set
(`agent/runner.py`).

**RECOMMENDATION.** Arrange third-party credentials with the agent's operator through
declared, allowlisted environment names. `params` is ordinary configuration copied into
the manifest; it is not a protected secret store.

### 1.2 The credentials envelope

**BEHAVIOUR.** The agent writes one JSON document to the path in
`LSPO_CREDENTIALS_FILE`. The **directory** containing it is bind-mounted read-only into
your container; the file itself is not mounted, deliberately, so that the agent can
replace it under a running container and you will see the new one
(`agent/creds.py`, credential writing and path validation).

Two schemes exist. Object storage, which is what a real deployment uses:

```json
{
  "schema_version": 1,
  "scheme": "s3",
  "credentials_file": "/lspo/creds/creds.json",
  "expires_at": "2026-08-08T12:15:00+00:00",
  "manifest_get": "https://...presigned GET for invocation.json...",
  "inputs": [
    {
      "port": "input",
      "relpath": "rows.csv",
      "sha256": "e3b0c442...64 lowercase hex...",
      "size": 1234,
      "name": "rows.csv",
      "get_url": "https://...presigned GET..."
    }
  ],
  "staging": {
    "mode": "presigned_post",
    "post": {
      "url": "https://bucket.s3.amazonaws.com/",
      "fields": {"key": "...", "policy": "...", "x-amz-signature": "..."},
      "key_prefix": "pipelines/7/executions/42/attempts/1/gen-1/"
    }
  }
}
```

And local paths, which exist only for a single-host demo:

```json
{
  "schema_version": 1,
  "scheme": "local",
  "credentials_file": "/lspo/creds/creds.json",
  "expires_at": "2026-08-08T12:15:00+00:00",
  "manifest_path": "/srv/artifacts/.../invocation.json",
  "inputs": [
    {"port": "input", "relpath": "rows.csv", "sha256": "...", "size": 1234,
     "name": "rows.csv", "local_path": "/srv/artifacts/.../rows.csv"}
  ],
  "staging": {"mode": "local_path", "path": "/srv/artifacts/.../gen-1"}
}
```

**BEHAVIOUR, for the table below.** This is what the agent writes, field by field
(`runners/credentials.py`, the S3 and local envelope builders and their constants). Nothing here is a duty on you; it is what you will find in
the file.

| Field | Meaning |
|---|---|
| `schema_version` | Envelope version, currently `1`. This is the envelope's own version and is separate from the job description's `schema_version`. |
| `scheme` | `"s3"` or `"local"`. Decides which of `manifest_get`/`manifest_path` and `get_url`/`local_path` is present. |
| `credentials_file` | An echo of where this document is mounted. Informational: you already had to read the file to see it. |
| `expires_at` | ISO 8601 timestamp. After this the upload policy stops working. Presigned reads may last slightly longer; see 4.3. |
| `manifest_get` / `manifest_path` | Where to read `invocation.json`. |
| `inputs[]` | One flat entry per input object. `port` names the port it belongs to; `name` is a display name derived from the relpath, or from the object key's last segment when the relpath is empty. |
| `staging.mode` | `"presigned_post"` or `"local_path"`. |
| `staging.post.url` | The form POST endpoint. |
| `staging.post.fields` | Form fields you must send unchanged, except `key` (see 4.2). |
| `staging.post.key_prefix` | The object key prefix every upload must start with. Always ends with `/`. |
| `staging.path` | Local mode only: the directory to write into. |

**RECOMMENDATION, with no working alternative.** Locate the job description through
`manifest_get` (object storage) or `manifest_path` (local), in that order of availability.
Nothing refuses you for trying `LSPO_INVOCATION_URI` instead; you simply hold no
credentials for it. The platform's own note on this reads: reading the invocation URI
first worked in every local test and failed on the first real object-storage launch.

**BEHAVIOUR.** The envelope **flattens the ports**. Each entry carries a `port` name, but
the port's `layout`, its `cardinality` and, for a folder port, its whole-tree digest are
all absent. A node that reads only its credentials therefore cannot verify a tree digest
and cannot tell a folder port from a file port. Fetch the job description if you need any
of that. (Compare `runners/credentials.py` with
`external/contract.py`.)

**RECOMMENDATION.** Validate the envelope before trusting it: the `schema_version` you
implement, a `scheme` you support, a `staging.mode` you support, and the presence of the
fields you are about to use. Fail with a clear sentence rather than a `KeyError` five
frames deep.

**RECOMMENDATION.** Ignore fields you do not recognise rather than rejecting the
document. The orchestrator's own parsers are configured to ignore unknown fields
precisely so that adding a field is not a breaking change
(`external/contract.py`). Strict about the fields you know, tolerant about the
ones you do not: both, at the same time.

### 1.3 Where the credentials live, and who may read them

**BEHAVIOUR.** The agent creates one directory per job on its own disk and bind-mounts it
read-only into your container. The directory is mode `0711` — traversable by anyone,
listable by nobody but the agent — and the credentials file inside it is mode `0444`,
readable by every uid and writable by none. **Both are re-applied on every write**, and
that matters more than it sounds: a refresh replaces the file with a brand-new one, so a
permission granted once and not re-granted would let a short job pass and kill a long one
partway through (`agent/creds.py`, `CREDS_DIR_MODE`, `CREDS_FILE_MODE`,
`JobCredentials.write`).

**BEHAVIOUR.** What keeps that credential off the rest of the machine is not the file's
mode but the agent's own working directory above the mount, which is owner-only and is
bind-mounted into nothing (`agent/identity.py`, `ensure_private_workdir`). The
protection is an ancestor nobody else can traverse; the leaf is deliberately open, and it
is open so that your image's user is never the platform's business.

**BEHAVIOUR.** In object-storage mode the agent does **not** force your container's user;
your image runs as whatever `USER` it declares (`agent/runner.py` `_local_staging_mounts`
returns an empty user for anything that is not local-path mode, and says why: "on object
storage the image's own user is left alone, because the only host path it touches is its
own credentials directory, and that one is deliberately readable by every uid so this
stays true").

**So there is no uid coupling: build your image to run as whatever user suits it.** Earlier
revisions of this document said the opposite — that your numeric uid had to equal the
agent's, "usually 10001". That was true of an older platform and is now false twice over.
The modes were widened precisely so it would stop being true, and the agent is started with
`--user "$(id -u):$(id -g)"`, so a deployed agent runs as the operator rather than as the
account its own image declares. If you built an image around 10001 on the strength of the
old text, nothing breaks — 10001 is as valid as any other number — but you are free of it.

**RULE, and the one thing the modes still ask of you — enforced by the kernel rather
than by a check on your node.** Open the exact path named by
`LSPO_CREDENTIALS_FILE`; never enumerate the directory it lives in. `0711` grants
traversal, not listing, so a directory listing is a permission error for every user but the
agent. You were told the name, so nothing needs the listing.

**RECOMMENDATION.** Run as a non-root user. Not for a permission — none requires it — but
because there is no sandbox around your container (see
[OPERATIONS.md](OPERATIONS.md#residual-limits-stated-plainly)), so a root workload is a
root process on somebody's machine for no benefit.

**BEHAVIOUR to know about, not a duty on you.** Every process inside your container can
read your job's credentials; they already share a filesystem and an environment, and the
envelope is scoped to that attempt's own staging prefix and expires. And an image that
makes `/lspo` non-traversable defeats its own mount — nothing on the host prevents that.

**BEHAVIOUR, local demo mode only.** When the staging area is a local directory the agent
forces your container to the agent process's own uid and gid, with **no supplementary
groups** (`agent/runner.py` `_local_staging_mounts`). Your image's own user is ignored
there — the opposite problem from the one above, and this one is still live — so anything
that depends on it, most obviously writing under that user's home directory, works in
production and fails in the demo.

**RECOMMENDATION.** Write scratch files to `/tmp` or into your staging area, never into
your image user's home directory. `/tmp` is writable by any uid; a home directory is not.

---

## 2. Inputs

### 2.1 Reading the job description

**BEHAVIOUR.** `invocation.json` is written by the orchestrator before the job is
queued, and the writer refuses to publish one larger than 8 MiB
(`external/contract.py`, `external/io.py`). So it is bounded, and you may rely
on that.

**RECOMMENDATION.** Bound your read anyway: read at most 8 MiB and refuse a longer
document, rather than streaming an unbounded body into memory because a proxy returned
something unexpected.

**BEHAVIOUR.** The current contract version is `1`, and it is also given to you in
`LSPO_CONTRACT_VERSION`. In the platform's own parsers a missing `schema_version` means
version 1 (`external/versioning.py`), and a `schema_version` that is not a real
integer, notably JSON `true`, is malformed rather than version 1
(`external/versioning.py`). In Python `True == 1`, which is exactly the trap that
check exists to close.

**RECOMMENDATION.** Mirror that in your own reader: refuse a `schema_version` you do not
implement, treat a missing one as 1, and do not let a boolean pass as 1. Nothing checks
this on your side, so a node that ignores the version will one day parse a version 2
document under version 1 rules and produce plausible nonsense.

### 2.2 `invocation.json`, field by field

**RULE, and it governs all three tables in this section.** Every "Type" and "Required" cell
below is a check in the parser the orchestrator itself uses
(`external/contract.py`): a document that breaks one is invalid, and the job does
not run. "Required" means the document is invalid without that field. These are not
descriptions of the usual shape — they are the refusals.

| Field | Type | Required | Notes |
|---|---|---|---|
| `schema_version` | integer, exactly `1` | no, defaults to `1` | |
| `execution_id` | integer >= 1 | **yes** | strict integer: `"42"`, `true` and `42.0` are all rejected |
| `pipeline_id` | integer >= 1 | **yes** | |
| `organization_id` | integer >= 1, or `null` | no | present when the deployment is tenanted |
| `deployment_id` | integer >= 1 | **yes** | which registered node this is |
| `revision_id` | integer >= 1 | **yes** | which immutable revision of it |
| `image_digest` | string | **yes** | `name@sha256:<64 hex>` or a bare `sha256:<64 hex>` image id |
| `params` | object | no, defaults to `{}` | **your node's configuration.** Carried verbatim, unfiltered, unredacted |
| `env_names` | array of strings | no, defaults to `[]` | names only, never values |
| `inputs` | array of ports | no, defaults to `[]` | port names are unique |
| `staging_prefix` | string | **yes** | URI of your staging area |
| `credentials_file` | string | no, defaults to `/lspo/creds/creds.json` | an echo; see below |
| `timeout_seconds` | integer >= 1 | **yes** | a **requested budget**, not a deadline. **900 in practice**, see 2.4 |
| `idempotency_key` | non-blank string | **yes** | same value as `LSPO_IDEMPOTENCY_KEY` |
| `attempt` | integer >= 1 | **yes** | |
| `generation` | integer >= 1 | **yes** | fencing token |

An input port (`external/contract.py`):

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | **yes** | the port name your code refers to. Today this is always `input` |
| `payload_kind` | string | **yes** | logical content type. Taken from the **first** upstream artifact's own kind, or the literal string `file` when that artifact carries no kind at all. It is not the node's `input_payload_kind` setting, which only selects which artifact gets pinned |
| `layout` | `"file"` or `"prefix"` | **yes** | today always `"file"` |
| `cardinality` | `"one"`, `"at_least_one"` or `"many"` | **yes** | today always `"many"`, and it is enforced by the parser |
| `objects` | array | no, defaults to `[]` | |
| `prefix_digest` | 64 hex chars, or `null` | required when `layout` is `"prefix"` | |

An input object (`external/contract.py`):

| Field | Type | Required | Notes |
|---|---|---|---|
| `uri` | string | **yes** | the true storage address; usually not readable from your container |
| `sha256` | 64 lowercase hex characters | **yes** | |
| `size` | integer >= 0 | **yes** | |
| `relpath` | canonical relative path, or `null` | no for a file port, **yes** for every object of a prefix port | |
| `upstream_execution_id` | integer >= 1, or `null` | no | which execution produced this object |

**BEHAVIOUR.** A step with no upstream artifacts gets `"inputs": []`, an empty list with
**no ports at all**, rather than one port with no objects
(`handlers/steps/external.py`). Your code must handle a job with no input port
present, not merely a port that is empty. A step with no inputs is a legitimate
source-style step and is not an error.

**BEHAVIOUR.** `credentials_file` in the job description is an echo, not a discovery
mechanism. It cannot be one: you needed the credentials to fetch this document. The
authority on where your credentials are is `LSPO_CREDENTIALS_FILE`.

**RECOMMENDATION.** If `LSPO_CREDENTIALS_FILE`, the envelope's own `credentials_file` and
the job description's `credentials_file` do not all agree, say so loudly in your log and
carry on using `LSPO_CREDENTIALS_FILE`. Nothing checks this today, and a disagreement
means somebody's assumption is wrong.

### 2.3 Reading the input objects

**BEHAVIOUR.** The orchestrator hashed every input when it built the job and pinned the
result. It never checks what you actually read. **The platform verifies what you wrote,
never what you read** (`handlers/steps/external.py` pins the objects; `pipelines/external_finalize.py`
verifies the published output). The reference node is maintained in this repository; the
orchestrator no longer vendors `examples/hello-node/node.py`.

**RECOMMENDATION, and the single most valuable one in this document.** Verify each input
against its pinned `sha256` and `size` before you act on it, and refuse an input that
arrives with no pin. A presigned URL points at a key, and "the bytes at that key today"
is not automatically "the bytes that were there when this job was built". A node that
skips this check can process the wrong version of its input and produce output that
passes every check the platform makes on the way back, because those checks are about
what you wrote.

**BEHAVIOUR.** Two inputs can arrive with the **same** `relpath` and the same `name`. The
orchestrator sets an input's relpath to the last segment of its URI
(`handlers/steps/external.py`), and nothing de-duplicates within a file-layout port;
the uniqueness check exists only for folder ports (`external/contract.py`). Two
upstream steps that both produce `rows.csv` therefore collide.

**RULE.** Your output relpaths must be unique within one marker
(`external/contract.py`).

**RECOMMENDATION.** Derive your output names rather than echoing input names: an index, a
hash prefix, the port name, anything that cannot collide. A node that writes
`outputs/<input name>` fails the moment two inputs share a basename, and it fails at the
end of the run, after all the work.

**RECOMMENDATION.** Stream inputs to disk or process them incrementally. See section 3.5
for why holding one in memory is a real risk rather than a style preference.

**BEHAVIOUR.** There is no ceiling on the **size** of your inputs: not on one object, not
on their total. The 1 GiB ceiling in section 3.5 applies to what you **upload**, and the
container is given no disk quota at all — the agent sets a memory limit, a CPU limit and a
process limit when it starts your container, and no storage limit
(`agent/executors/docker_exec.py`). A node that downloads every input to `/tmp` and
keeps it there can therefore fill the disk of the machine the agent runs on, which is
somebody else's laptop or server. Delete each input when you are done with it, or stream it
and never land it at all.

**BEHAVIOUR, and there IS a ceiling on the NUMBER of them, indirectly.** Every input, with
its URI, its hash, its size and its relpath, is written into the job description, and the
writer refuses to publish one larger than **8 MiB** (section 2.1,
`external/io.py`). So the input count is bounded after all, by a limit that depends
on how long your URIs are. Measured against the real models at orchestrator commit
`6b2ff82c70f26d0ceaa1a841137f1b3cfb08186b` on 2026-08-08 (not re-measured in this audit): about **330 bytes per pinned input object** with 95-character URIs, which
puts the ceiling at roughly **25,000 inputs** — 25,445 fitted, 25,446 did not. Past it the
step fails **before the job is created**, at the moment the orchestrator tries to write the
job description, so no container ever starts and there is nothing for your node to handle.
It is not a limit you can design around; it is one to know about before wiring a node
downstream of something that produces tens of thousands of files.

### 2.4 How long you actually get

**BEHAVIOUR.** The requested runtime budget is `timeout_seconds`. The platform resolves
it from the pipeline node first, then the revision, then
`LSPO_EXTERNAL_DEFAULT_TIMEOUT_S` (3600 seconds). Image registration and hosted builds
both default the revision's budget to **900 seconds**. Reusing an existing revision does
not change its budget; a pipeline-node override takes precedence
(`handlers/steps/external.py`, `runners/jobs.py`, `noderegistry/services.py`,
`noderegistry/building.py`).

**BEHAVIOUR.** The deadline starts when the agent **claims** the job. Queueing does not
consume the budget; preparation after claim, including pulling the image, does. The
workload receives the requested duration, not the claim time or absolute deadline.

**BEHAVIOUR.** With the default stop-window settings, reaching the runtime deadline
starts stopping the workload without immediately withdrawing the agent's permission to
report. The configured allowances are **180 seconds for stopping** and **60 seconds
for reporting**. The window ends at the original deadline plus those allowances;
discovering the deadline late does not restart it. A report accepted inside the window
can fail the run and salvage a valid marker's objects as diagnostics. This replaces the
older behavior where the lease ended at the runtime deadline and normal deadline reports
were refused. See [section 7](#7-cancellation) for the remaining failure cases.

**BEHAVIOUR.** Credential lifetime is still at most the configured 900-second envelope
budget, but its upper limit is now the end of the possible stop window, rather than the
runtime deadline alone (`runners/credentials.py`, `runners/jobs.py`). The agent refreshes
the file while the job is running. A workload that caches its first envelope can still
lose upload access after fifteen minutes, including during its final receipt.

**RECOMMENDATION.** Finish work, uploads and the marker comfortably within the requested
budget. Re-read credentials near their stated expiry and before the marker. The stop
window is recovery time, not extra work time. The credential expiry can reflect the
reporting cutoff, but it does not identify the workload's kill cutoff; see
[section 7](#7-cancellation).

---

## 3. Doing the work

### 3.1 Your configuration

**BEHAVIOUR.** `params` in the job description is the only part of the node's
arbitrary configuration that reaches your code. It is copied verbatim from what the pipeline
author typed, with no filtering and no redaction
(`external/contract.py` and `pipelines/config_schemas.py`). Everything else about the
node is not copied wholesale. The manifest separately includes the identity fields,
deployment/revision ids and timeout listed in section 2.2.

**RECOMMENDATION.** Validate `params` yourself and fail with a readable message. Nothing
between the pipeline author and your code checks its shape.

### 3.2 What your container may reach

**BEHAVIOUR.** Your container has ordinary outbound networking unless the agent's operator
put it on a restricted docker network (`LSPO_AGENT_NETWORK`, empty by default which means
the daemon's default network).

**BEHAVIOUR.** The agent's own credentials never enter your container. The only access you
are given is what the envelope carries (`agent/README.md`, "Least privilege").

**BEHAVIOUR.** Resource ceilings, applied by the agent to every workload container and
configurable by its operator (`agent/config.py`,
`agent/executors/docker_exec.py`): 2 CPUs (`LSPO_AGENT_CPUS`), **2 GiB of memory**
(`LSPO_AGENT_MEMORY`, default `2g`), 512 processes (`LSPO_AGENT_PIDS_LIMIT`), and
`no-new-privileges`.

### 3.3 Logging

**BEHAVIOUR.** The agent merges stdout and stderr, splits on newlines, and sends the
result through runner heartbeats. The run view shows those lines live. At termination,
the collector merges the retained stream with the log already on the execution and
writes `orchestrator.ndjsonl`; the execution row receives a separately bounded copy
(`agent/executors/docker_exec.py`, `pipelines/external_finalize.py`).

**BEHAVIOUR.** These limits can discard output:

| Stage | Default limit and result |
|---|---|
| Container stream | A line over 64 KiB is truncated and its remaining bytes discarded |
| Agent buffer | 2000 lines; old entries are evicted, with a warning reporting the gap |
| Undelivered queue | 1000 entries per job, including the loss warning; when reports are not accepted, oldest pending lines are dropped (`agent/runner.py`, `_retain`) |
| Normal heartbeat | Every 20 seconds, at most 100 lines; sustained output above about five lines per second can exceed this rate |
| Agent line | 4096 characters before sending; server limits are measured in bytes |
| Server request | 100 lines, 4096 bytes per line, 128 KiB of logs, 256 KiB request body |
| Live stream | Last 1000 entries; earlier entries not already saved elsewhere cannot be recovered by the terminal log writer |

**BEHAVIOUR.** The agent retains pending lines after transport or general API failures,
checks for lease expiry and retries with at most 1000 pending entries per job. A heartbeat rejected as a bad
request or oversized payload loses the batch it carried and its progress sample, with an
explanation in the agent log. **That refused request earns no lease renewal**; dropping
its payload lets later heartbeats try again without repeating the same refusal. Repeated
failures can still lose the lease and fence the job. A stale sequence response also earns
no renewal, but keeps the payload while the agent resynchronizes its counter. Final log
flushing attempts at most five heartbeats. The durable file receives the whole
**retained merge**, not a fixed 1000-line file and not a complete transcript.
Loss notices explain dropped or unavailable lines. Neither a successful log write nor a
green run proves that every line printed by the workload survived
(`agent/runner.py`, `pipelines/log_stream.py`, `pipelines/external_finalize.py`).

**BEHAVIOUR.** Control characters other than tab and newline are removed. ANSI color
escapes therefore leave stray text such as `[31m`; use plain text.

**BEHAVIOUR.** The current agent removes exact credential values it holds from workload
log lines before buffering them (`agent/logbuf.py`, `agent/redact.py`). This is not a
general filter for arbitrary third-party secrets or every transformed spelling of a
credential, and the raw Docker log on the host is outside that filter.

**RECOMMENDATION.** Never print credentials, presigned URLs or secrets from `params` or
the environment. Redact HTTP exceptions yourself. Say important things in a few lines;
upload a structured log file and inventory it when the full account must be retained.

### 3.4 Progress

**BEHAVIOUR, opt-in.** A single stdout line of exactly this shape is consumed by the agent
and reported as progress on the next heartbeat (`agent/logbuf.py`):

```
@lspo:progress {"fraction": 0.4, "phase": "encoding"}
```

* The prefix is `@lspo:progress ` **including the trailing space**.
* The rest of the line must be a JSON object with a `fraction` that converts to a float
  between `0.0` and `1.0` inclusive.
* `phase` is optional, coerced to a string, stripped of control characters and cut to 64
  characters (`agent/logbuf.py`, `runners/serializers.py`).
* A malformed progress line is **not** consumed: it appears in your log as an ordinary
  line. That is deliberate, so a typo is visible rather than silent.
* Only the most recent sample is reported per heartbeat.

**BEHAVIOUR.** A node that emits none simply has no progress. The agent never invents a
fraction from elapsed time. Nothing requires you to report progress.

**RECOMMENDATION.** Emit progress for anything that runs longer than a minute or two. It
is the only signal an operator has that a long step is alive.

### 3.5 Memory: two defaults that collide

**BEHAVIOUR.** The largest single object you may upload is **1 GiB**
(`LSPO_EXTERNAL_MAX_OBJECT_BYTES`, default `1024*1024*1024`, `lspo/settings/base.py`),
enforced by the upload policy itself and re-checked at collection. The default memory
limit for your container is **2 GiB** (`LSPO_AGENT_MEMORY`, default `2g`).

Those two numbers are the whole warning. An object the platform explicitly permits, held
in memory once as input bytes and once as an upload body, is 2 GiB of resident memory
against a 2 GiB limit. The kernel kills the container, and **an OOM kill gives your process
no chance to write a marker**, so unless one was already there the run dies with a bare
exit code and no account of itself. (The container's exit code is still reported, so this
ending arms collection like any other self-reported failure and a marker you had already
written would be read. Under the write-the-marker-last discipline there is usually none —
usually, because a kill can also land in the interval between the marker's upload finishing
and the process exiting, and then it is there; see section 7.1.) Nothing in the
orchestrator's limits is exceeded on the way there.

**RECOMMENDATION.** Stream in both directions. Read inputs to a temporary file or in
chunks, hash while you stream, and upload from a file handle rather than from a `bytes`
object. Do this even for a node that "only handles small files": the input size is chosen
by whoever wires the pipeline, not by you.

**RECOMMENDATION, and streaming alone does not buy it. This one is measured.** The
container's memory limit counts the **page cache** that its own reads and writes create,
not only what your process holds. So a node that streams perfectly — never more than one
block in memory — is still OOM-killed for moving a large object through a temporary file:
your program's footprint stays flat while the kernel's cache for that file grows to the
size of the object. Ask for those pages back as you go. On Linux that is
`posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED)` every few megabytes, after an `fsync` on
the write side, because dirty pages cannot be dropped; reads need no sync. It is advice
to the kernel rather than a guarantee, and it costs nothing when it is ignored.

Measured against this repository's own image: a 128 MiB input through a 64 MiB container
is OOM-killed without that call and survives with it, and with it the same transfer also
survives a 32 MiB container. The step's own code was identical in both runs — the only
difference is who was holding the bytes.

---

## 4. Outputs

### 4.1 Where your output goes

**BEHAVIOUR.** Everything you produce is addressed by a **relpath** relative to your
staging prefix. The staging prefix is scoped to this execution, this attempt and this
generation, and ends in `attempts/<attempt>/gen-<generation>` for exactly that reason: a
superseded runner physically cannot write into the live attempt's area
(`external/contract.py`).

**RULE.** A relpath must be canonical (`external/contract.py`):

* non-empty, and never starting with `/`;
* no backslashes;
* **no control character at all** — the whole of U+0000 to U+001F, which includes the tab
  and the newline. This is wider than it used to be: the rule was once "no line break",
  and it was widened because a NUL in a relpath is a name no filesystem can hold, so the
  reader that tried to open it crashed several layers below the parser instead of
  refusing the document with a sentence;
* no `.` or `..` path components;
* **no empty components**, so `a//b` and a trailing `/` are both refused.

The last one surprises people. Two spellings of one path would be two inventory entries
for one file, or an alias that dodges the "is this relpath known?" check.

**BEHAVIOUR, and it is the reason a bad name is refused rather than cleaned.** A relpath
is an **identifier**: the platform resolves a real object by it. A cleaned name would
quietly ask for a different file — possibly one that exists and belongs to another port —
and delivering the wrong bytes is worse than delivering none. Free-form prose in the same
document is treated the opposite way; see the note on `error` in section 5.

**RULE.** An **output port name** must be a real name: not empty and not only whitespace
once trimmed, and free of control characters, by the same definition as above
(`external/contract.py`). A port name is an identifier twice over — a downstream
step selects its input by matching that string, and the delivered artifact is stored under
it — so it is held to the same standard as a path.

**BEHAVIOUR, and it catches people.** The name is checked for emptiness *after* trimming
but is **not** trimmed: `" output "` passes, and then travels onward with its spaces intact,
as the artifact kind a downstream step has to match exactly. This was exercised against the
parser. Trim your own port names.

**RULE.** Relpaths listed under `produced_ports` are held to the full canonical-relpath
rule as well, not merely to "is it in the inventory" (`external/contract.py`). There
is no spelling that is legal in one place and not the other.

**RULE.** A relpath is checked twice: once when your marker is parsed, and again when
collection resolves it against the staging prefix
(`pipelines/external_finalize.py`). Both refuse traversal.

**BEHAVIOUR.** A relpath containing a `%` or a space behaves differently on the two
storage backends. Object-store keys are joined raw; local file URIs are percent-quoted per
segment (`pipelines/external_finalize.py`). This was a real bug: a file named
`rate%20.csv` was looked for at `rate .csv`.

**RECOMMENDATION.** Keep relpaths to unaccented letters, digits, `-`, `_`, `.` and `/`.
You gain nothing from an exotic name and you inherit two backends' disagreements.

### 4.2 Uploading, in object-storage mode

**BEHAVIOUR.** `staging.post` is a presigned form POST policy. Its conditions are
(`runners/credentials.py`):

* `["starts-with", "$key", "<key_prefix>"]`, so the storage service itself refuses any key
  outside your generation's prefix, and
* `["content-length-range", 0, 1073741824]`, the per-object ceiling.

**RULE.** Every upload must have a key that starts with `staging.post.key_prefix`. This is
enforced by the storage service, not by us, so a violation is an HTTP refusal rather than
a friendly message.

**RECOMMENDATION.** Set the `key` form field explicitly to `key_prefix + relpath` rather
than relying on the `${filename}` placeholder that arrives in `fields`. It is one line,
it makes nested relpaths work predictably, and it is what the reference does.

**RECOMMENDATION.** Send the fields first and the file part last, as a multipart form.
This is a property of the storage service's form-upload API rather than of our platform:
it ignores form fields that appear after the file part.

**BEHAVIOUR.** In local demo mode there is no POST at all: `staging.path` is a directory
and you write files into it, creating parent directories as needed.

**RECOMMENDATION.** Write both paths behind one function in your code. It is what makes
"the marker is written last" a property of the program rather than a hope, and it makes
the local mode genuinely testable.

### 4.3 Credentials expire during your run

**BEHAVIOUR.** `LSPO_RUNNER_CREDS_TTL_S` defaults to **900 seconds**. Each envelope is
limited by that budget and by the launch's stop-window ceiling: the recorded stop
deadline when there is one, otherwise the runtime deadline plus the configured stop
allowances. With both stop allowances set to zero, the runtime deadline is again the
ceiling (`runners/credentials.py`, `runners/jobs.py`).

**BEHAVIOUR.** The agent asks for a new envelope within 60 seconds of expiry by default,
then atomically replaces `creds.json` in its mounted directory. It sends no signal to
the workload. A process that keeps an old file descriptor or cached document does not
automatically receive the replacement (`agent/creds.py`, `agent/runner.py`).

**BEHAVIOUR.** Replacing the envelope does not revoke its previously issued URLs. Reads
can outlive the envelope's conservative expiry because the signer dates them after
resolving credentials; the POST policy's expiry is computed before signing. This is not
a reason to extend a workload's own cached lifetime (`runners/credentials.py`).

**RECOMMENDATION.** Reopen `LSPO_CREDENTIALS_FILE` at or near `expires_at`, and before
the final marker. On an unambiguous expiry refusal, reload and retry once **only if the
envelope's contents changed**. Object identity is not a useful comparison: every JSON
parse creates a new object. A transport failure after an upload began is ambiguous;
blindly retrying may overwrite a write the store already accepted.

**BEHAVIOUR.** The runtime stop window permits credential refresh while the same agent
still holds a valid lease, but it does not revive a lost lease or let an operator-cancelled
launch regain authority. Read [section 7](#7-cancellation) before relying on uploads
after a stop.

### 4.4 `result.json`

**BEHAVIOUR. Nothing in the orchestrator ever reads `result.json`.** There is no caller of
the contract's own `read_result` anywhere outside the contract package. The run's metrics
come from the completion marker and the objects that were actually published: the
execution records `objects_published`, `bytes_published` and `exit_code`
(`pipelines/external_finalize.py`).

**BEHAVIOUR.** Because nothing reads it, the contract's 1 MiB ceiling for this document is
not enforced against you. It becomes an ordinary object of yours, subject to the 1 GiB
per-object ceiling like any other.

**BEHAVIOUR.** If you list `result.json` in the marker's `objects` it is copied and
verified like any other object. If you also claim it under an output port, it becomes a
downstream artifact under that port's name. Both are legal. This repository's reference node claims it under `report`.

**RECOMMENDATION.** If you write one, claim it under a deliberately separate port, for
example `report`, rather than under the port your real output goes to. Multiple output
ports are collected today. Claiming a diagnostics file under `output` means every
downstream step receives it as if it were a delivery.

### 4.5 `logs.ndjsonl`

**BEHAVIOUR.** The contract invites you to write a structured log file called
`logs.ndjsonl`. It is treated as an ordinary object: it is published only if you list it in
the marker's `objects`, and it is published under the name you chose. The orchestrator
writes its own record of the run to a **sibling** prefix under a different filename
(`orchestrator.ndjsonl`), so it cannot collide with yours
(`pipelines/external_finalize.py`). Nothing requires you to write one.

**RECOMMENDATION.** If your step's own output matters, write it as a file and inventory
it. The platform's record merges the retained live tail with saved execution lines and
collection findings; it is not a complete stdout transcript (section 3.3). A file you upload
and inventory can be kept whole and verified when collection runs; claim it under a port
to offer it downstream on success.

---

## 5. The completion marker

**BEHAVIOUR.** The marker is `__lspo_complete.json`, read from the **root** of your staging
prefix — collection joins that prefix to the literal filename and reads whatever is there
(`external/io.py`). It is the terminal receipt for the attempt.

```json
{
  "schema_version": 1,
  "execution_id": 42,
  "attempt": 1,
  "generation": 1,
  "idempotency_key": "execution-42-attempt-1",
  "status": "succeeded",
  "exit_code": 0,
  "objects": [
    {"relpath": "outputs/rows.csv", "sha256": "<64 lowercase hex>", "size": 1234}
  ],
  "produced_ports": {"output": ["outputs/rows.csv"]},
  "error": null
}
```

**RULE, for the table below.** Every "Type" and "Required" cell is a refusal the marker
parser makes (`external/contract.py`); a marker that breaks one is not read at all.
[CONFORMANCE.md](CONFORMANCE.md#validating-your-own-marker) lists every one of those
refusals individually, enumerated by exercising the parser.

| Field | Type | Required | Notes |
|---|---|---|---|
| `schema_version` | integer, exactly `1` | no, defaults to `1` | |
| `execution_id` | integer >= 1 | **yes** | cross-checked |
| `attempt` | integer >= 1 | **yes** | cross-checked |
| `generation` | integer >= 1 | **yes** | cross-checked |
| `status` | `"succeeded"`, `"failed"` or `"cancelled"` | **yes** | |
| `idempotency_key` | non-blank string, or `null` | **no** | echoed if you set it; **not** cross-checked |
| `exit_code` | integer, may be negative | **no** | a diagnostic echo |
| `objects` | array of `{relpath, sha256, size}` | no, defaults to `[]` | relpaths unique |
| `produced_ports` | object mapping port name to array of relpaths | no, defaults to `{}` | names and relpaths are both validated; see 4.1 |
| `error` | string or `null` | **no** | quoted into the failure reason, cut at 500 characters (`pipelines/external_finalize.py`). Control characters are removed from it |

**RULE.** On a run the platform classifies as **succeeded**, which normally means your
process exited 0 and nothing cancelled or timed it out, a marker is **required**. A missing
marker after a reported success fails the execution: there is no inventory, so there is
nothing to publish and nothing to verify (`pipelines/external_finalize.py`).

**RULE.** On such a run the marker's own `status` must be `"succeeded"`. A process that
exits 0 while its marker says `failed` fails the execution
(`pipelines/external_finalize.py`).

**RULE.** `execution_id`, `attempt` and `generation` must equal the launch the platform
believes it is collecting (`pipelines/external_finalize.py`). Copy them from the
job description, not from memory of an earlier attempt. A mismatch is refused with "a
receipt from a superseded or unrelated run must never be collected as this one".

**RULE.** Every relpath in `produced_ports` must appear in `objects`
(`external/contract.py`). The inventory is what carries the hash and the size.

**RULE.** Within one port a relpath may appear only once; the parser refuses a repeat
(`external/contract.py`). The **same relpath in two different ports is legal** and
is sometimes what you want.

**RULE.** Relpaths in `objects` are globally unique across the whole inventory
(`external/contract.py`). One file, one entry.

**RULE.** Every port name is non-empty after trimming and carries no control characters,
and every relpath under a port is canonical — the same rules as section 4.1
(`external/contract.py`).

**RULE.** The marker document must be at most **8 MiB**; the reader refuses a larger one
(`external/contract.py`, `external/io.py`). For a very wide batch, that bounds
how many objects one attempt can inventory.

**RULE.** Every `sha256` is exactly 64 lowercase hexadecimal characters, with no `sha256:`
prefix, no uppercase and no trailing newline (`external/contract.py`, matched with
`fullmatch` because in Python a `$` anchor also matches before a trailing newline).

**RULE.** Every integer is a real JSON integer. `"1"`, `true` and `1.0` are all rejected,
and every id and counter must be at least 1 (`external/contract.py`).

**BEHAVIOUR.** `error` is **cleaned, not refused**: control characters other than tab and
newline are removed from it as the document is parsed
(`external/contract.py`). The asymmetry with names (section 4.1) is deliberate.
Nothing resolves anything by your error message, so cleaning it keeps your account of the
failure, which is the most useful thing anybody reads; cleaning a *name* would point at a
different file. Note the practical reason it is cleaned at all: the text is written into a
database column that cannot hold a NUL byte, and the write in question is the one that
records the run as finished.

**RECOMMENDATION, and the strongest one in this document.** Write the marker **last**,
after every object it names is durably readable.

Nothing checks this, and nothing can. Collection begins only after your process has
already exited, so the platform never observes the order in which you wrote anything: a
node that writes the marker first, then finishes every object it named before exiting,
succeeds exactly like a well-behaved one. What collection does instead is check the
**consequences** — it copies every object the marker names and re-reads it, holding it to
the hash and size you promised (`pipelines/external_finalize.py`).
A marker written before its objects were finished fails there, as a size or hash mismatch.

The reason to do it anyway is what the invariant buys: **the marker's existence is the
only thing that distinguishes a half-finished run from a complete one.** Write it last and
that whole class of failure is impossible rather than merely detected. The contract module
itself states it as an instruction to you — "the step writes it **strictly last**"
(`external/contract.py`) — and this repository's harness records the upload order for the reference node
(`tests/test_marker_and_ordering.py`). Neither is a check on an arbitrary deployed node.

**A general lesson, worth more than this one rule — and it is background, about how to read
these documents rather than about the platform.** An instruction written in a source file,
however emphatic, is not an enforced rule. It is a statement of intent by whoever
wrote that file. The only things that can refuse you are the checks named in this document
under **RULE**, and when you are deciding what your node must do, "the code says to" and
"the platform will stop me" are different facts with different consequences. This
particular sentence sits in the contract module in bold, and there is no check anywhere
behind it.

**BEHAVIOUR.** An object you inventory but claim under no port is copied and verified, and
then **not offered downstream** on a successful run. It is not lost, but the execution's
artifact list is built from `produced_ports` alone
(`pipelines/external_finalize.py`), so an unclaimed object does not appear
there. If you want it retrievable through the normal path, claim it under a port.

**RECOMMENDATION.** Set `exit_code` in the marker to the code your process is actually
about to return. It is optional, and it is a diagnostic. A marker that says 10 while the
process returns 1 leaves two contradictory accounts of one run, which costs somebody an
hour later.

**RECOMMENDATION.** On a failed or cancelled run, write a marker too. The contract invites
it and collection reads it: your `error` and `exit_code` become the execution's failure
reason, and every object you inventoried is salvaged through the same verified path
(`external/README.md`, "A failed or cancelled marker is read, not discarded"). It is not
required, and a failing node that writes nothing loses all of that.

**BEHAVIOUR.** Salvage requires a terminal report that actually arms collection. Your
own failure or exit 20 can do that, as can a runtime-deadline stop whose report is
accepted inside the configured stop window. Operator cancellation of a running launch
still does not collect its output. Fencing may prevent reporting entirely; see section 7.

**BEHAVIOUR.** Failure and cancellation markers must still parse and match this
execution, attempt and generation. The collector now distinguishes an absent marker,
a storage read failure, an invalid document and a marker from another run, and records
the explanation in the failure reason and log. A document that cannot be verified
publishes nothing (`pipelines/external_finalize.py`, `_look_for_this_attempts_marker`).

---

## 6. Exit codes

**BEHAVIOUR.** The classes (`external/contract.py`):

| Code | Class | Meaning |
|---|---|---|
| 0 | ok | succeeded |
| 1 | transient | this could be retried |
| 10 | permanent | do not retry me |
| 20 | cancelled | stopped on request |
| 30 | contention | lost a lock or lease; another attempt owns the work |
| anything else | transient | including a crash, an OOM kill or a signal death |

**BEHAVIOUR.** Anything unrecognised classifies as **transient**. That is deliberate: a
crash is far more often an accident than a decision.

**BEHAVIOUR, and you must hear both halves of this.** Exit code 10 declares permanent,
do-not-retry intent, and **nothing acts on it today**. There is no automatic retry engine
for external jobs; every failed attempt is recorded as transient regardless of the code
your process returned (`pipelines/external_finalize.py`), and an
operator retries by hand from the run view. A future automatic retry path may honour exit
10 without any change to your node.

**RECOMMENDATION.** Use the codes as they are defined anyway. Exit 1 for a condition a
second run might survive, such as a storage timeout or a rate limit. Exit 10 for a
condition no retry can fix, such as an input your node cannot parse or a configuration
that is wrong. Treating 1 and 10 as interchangeable produces images that misbehave on the
day retries land.

**RECOMMENDATION.** In-node retry of a transient storage failure is optional. The
contract's exit code 1 records retryable intent; the current platform still requires an
operator to retry the attempt. What is genuinely wrong is misclassifying: reporting a transient
storage condition as permanent tells the platform never to retry something that would
have worked.

---

## 7. Cancellation

**BEHAVIOUR.** Runtime expiry, an operator pressing Cancel, and loss of authority
(fencing) have different paths. A local SIGTERM test exercises the node's handler; it
does not establish which path a deployed platform takes or what collection retains.

### Runtime deadline

**BEHAVIOUR.** The default runtime stop window is **180 seconds for the workload to
stop plus 60 seconds for the agent to report** (`LSPO_EXTERNAL_STOP_GRACE_S` and
`LSPO_EXTERNAL_STOP_MARGIN_S`). It ends at the original runtime deadline plus those
durations, even if the first heartbeat notices expiry late. Heartbeat renewal, signing
and terminal reporting are allowed within that window while the same lease remains
valid (`runners/jobs.py`, `runners/reports.py`, `runners/credentials.py`).

**BEHAVIOUR.** The agent watches the runtime deadline locally after preparation and also
receives stop instructions through heartbeats. It sends SIGTERM, keeps the heartbeat
thread running, and sends a kill when the remaining workload grace is exhausted.
New instructions can shorten an already running grace. They cannot restart it or extend
it past the recorded cutoff (`agent/runner.py`, `_await_exit`, `_signal_the_container`,
`_enforce_the_grace`, `_remaining_grace`). Older servers, or a server advertising no
window, use the executor's 30-second blocking stop fallback.

**BEHAVIOUR.** A deadline stop is classified as **failed**, before the process exit
code is considered. If the report is accepted, collection reads a valid marker and
salvages verified objects as diagnostics, with no downstream output port. If the agent
cannot report before its authority expires, the run may remain at **Waiting for runner**:
there is still no general queue/lease watchdog to finish it. Disabling both stop
allowances restores the older behavior where deadline reports lose that extra window.

**BEHAVIOUR.** Preparation consumes the budget after claim, and the normal local
deadline loop begins only after preparation. A slow image pull or preparation failure
can therefore exhaust the lease or reporting window before ordinary stop handling
begins. The stop window does not make an unavailable agent or daemon recover automatically.

### Operator cancellation

**BEHAVIOUR.** Pressing Cancel on a running external execution still sets its cancel
flag and terminalizes its launch in the same transaction. It does **not** open the
runtime stop window (`pipelines/cancellation.py`, `_cancel_external_work`). The next
heartbeat ordinarily receives a lost-lease answer, so the agent fences the container.
Only a heartbeat that renewed before the cancellation transaction and read the flag
after it can request a polite stop.

**BEHAVIOUR.** Neither branch collects the running workload's marker or objects. A
later terminal report is acknowledged as already finished and does not arm collection.
The cancellation path persists the log it has received. If collection was **already in
progress** when cancellation arrived, it may finish and attach verified objects as
diagnostics; it never sends them downstream (`pipelines/cancellation.py`,
`runners/reports.py`, `pipelines/external_finalize.py`).

### What your handler can do

**BEHAVIOUR.** An exec-form `ENTRYPOINT` normally makes your program **PID 1** inside
the container: the agent overrides neither the entrypoint nor command and requests no init
helper (`agent/executors/docker_exec.py`, container creation options). Linux treats the
PID namespace's init process specially: a SIGTERM with the default disposition is ignored
unless the program installs a handler. A node without one can continue its batch until
the grace expires and it is killed. The original 2026-08-08 documentation recorded that
adding a handler to the example made it exit on SIGTERM; that historical experiment was
not repeated for this audit. The current Docker conformance suite separately exercises
the reference node's signal handling.

**RECOMMENDATION.** Install a SIGTERM handler, stop accepting new work, keep the object
inventory available to the failure path, reload credentials, and attempt one final
`cancelled` marker before exiting 20. Register each object before its upload starts:
the store may accept the bytes even if the client never receives an acknowledgement.
A failed or absent object is skipped during salvage; an unlisted object is never examined.

**BEHAVIOUR.** The agent sets the nine bootstrap variables in section 1.1; none gives
the workload its absolute kill cutoff. The heartbeat's stop instruction reaches the
agent and is not forwarded into the credentials or invocation. The envelope's
`expires_at` is clamped to the earlier of its own lifetime and the reporting-window
ceiling; signing may conservatively understate it. Near the end it can therefore
reflect the reporting cutoff, while earlier envelopes expire sooner and can be
refreshed. That value does not identify the workload's kill cutoff, which is earlier
than the reporting cutoff by the configured margin (60 seconds by default), and it is
not a stop-timing contract (`runners/credentials.py`, `runners/jobs.py`).

**BEHAVIOUR.** The configured 180 seconds is not a fresh allowance every workload can
assume at SIGTERM. Late delivery, lost authority, a daemon failure or a hard kill can
leave less time or none. No wall-clock completion bound is guaranteed by those settings.

**RECOMMENDATION.** Make network operations interruptible where possible and bound the
rest. The reference gives DNS resolution and all TCP connection attempts one elapsed
budget of `CONNECT_DEADLINE_S = 10.0`. TLS receives the remaining time as its socket
timeout; `_connect_within` therefore describes the bound as the deadline plus one
handshake operation. The final receipt has its own elapsed deadline,
`RECEIPT_DEADLINE_S = 20.0` in `node.py`. Its separate socket timeouts are 25 seconds
for reads and 10 seconds for uploads. These are local choices, not platform guarantees. A signal handler that
only sets a flag cannot make a blocked network call return. SIGKILL cannot be handled.

### 7.1 Fencing: the stop with no grace period at all

**BEHAVIOUR.** Fencing means the agent tries to kill the workload immediately because it
has lost authority or can no longer keep watching it. No SIGTERM grace is provided.
The causes include an expired lease while the API is unreachable, work no longer
assigned to the agent, a deactivated tenant, a refused agent identity, a failed heartbeat
thread, and lost-lease/job-not-found responses (`agent/runner.py`).

**BEHAVIOUR.** Some fences retain permission to **attempt** a terminal report, including
an internal heartbeat-thread failure. Others cannot report with their lost credentials
or lease. Only an accepted report that arms collection can salvage a valid marker
already in staging. With no marker, uploaded objects are not discovered by listing the
directory. With no accepted report, there is no general watchdog to collect them later.

**BEHAVIOUR.** A marker written last can still be present before the agent observes the
container's exit. A fence in that interval may leave a complete marker available for
salvage. That possibility is not a recovery mechanism a workload can schedule around.

**BEHAVIOUR.** An ordinary agent shutdown is different: its first signal stops new
claims and waits for running jobs. A forced subsequent signal deliberately leaves job
containers for the next agent to adopt. This does not grant the jobs a new runtime
budget or prevent their credentials and leases from expiring.

**BEHAVIOUR.** After a restart, the agent records an adopted job's container as possibly
present before preparation can fail. It attempts to confirm that container has stopped
before sending a terminal report. If Docker cannot confirm teardown, the report can
still fail the job while the container may continue running and writing to staging.
The agent records that uncertainty, stops claiming new work and retries teardown during
reconciliation; repeated failures have no guaranteed completion time
(`agent/runner.py`, `_prepare`, `_finish`, `_settle_container`, `_reconcile_if_due`).
A terminal run status alone is therefore not proof that its former writer has stopped.

**RECOMMENDATION.** Do not assume exclusive access to staging merely because an earlier
run is terminal; design as though another writer may still be present.

**RECOMMENDATION.** Work in units, stream data and keep the inventory outside the work
function. That limits memory and preserves partial work on endings where a marker and
report survive. It cannot guarantee recovery after a kill or operator cancellation.

---

## 8. What happens after you exit

This section is **background**: nothing in it is yours to implement, and knowing it explains
most of the failure messages you will see.

**BEHAVIOUR.** The agent reports your exit code and its own classification. On a success
the orchestrator reads your marker, checks its identity, and then for every object in
`objects`:

1. copies it from staging into a **published** area your credentials cannot reach;
2. re-reads it **there** and holds it to the size and hash your marker promised
   (`pipelines/external_finalize.py`).

Copy first, verify second, because your upload policy is still live and a hash taken in
staging is a statement about the past.

**BEHAVIOUR, on a successful run.** Any single object that fails verification causes
**everything this collection published to be deleted** and the execution to fail
(`pipelines/external_finalize.py`). A partial delivery is worse
than a failed one, because nothing downstream can tell which it got.

**BEHAVIOUR, on a failed or cancelled run — when collection runs at all.** The same copy
and verify runs per object, and whatever verifies is kept while the rest is dropped with a
log line (`pipelines/external_finalize.py`). Salvaged objects are attached to the
execution as diagnostics: they carry no output port and are not offered to any downstream
step. Section 7 distinguishes the endings: a runtime-deadline report can now reach this
path inside the stop window; operator cancellation of running work and fences that
cannot report still do not.

**BEHAVIOUR.** Each delivered object becomes one downstream artifact whose kind is the
**output port name** you delivered it through (`pipelines/external_finalize.py`). A
downstream step configured to read `output` finds exactly what you published under
`output`.

**BEHAVIOUR.** The published location contains an identifier of the collection that won,
which you cannot predict. Never construct a published URI by hand; follow the URIs in the
execution's result (`pipelines/external_finalize.py`).

**BEHAVIOUR.** The run's metrics are `objects_published`, `bytes_published` and
`exit_code`, taken from what was actually published and from your marker's echo of the
exit code.
