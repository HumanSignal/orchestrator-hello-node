# PROTOCOL: the contract, in the order one attempt happens

This preamble is **background**. What follows is the normative document, and it follows the
life of a single container attempt: bootstrap, inputs, work, outputs, the completion
marker, and how the attempt ends.

Every statement is labelled **RULE** (the platform refuses or fails the run),
**BEHAVIOUR** (what the platform does, which you must plan for) or **RECOMMENDATION**
(what a good node does; the platform permits otherwise). Unlabelled text is background.

Also **background**: values here are literal and were verified against the orchestrator at
commit `6b2ff82c70f26d0ceaa1a841137f1b3cfb08186b`, and source paths appear after a value as
provenance.

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
(`external/contract.py:56-59`). You locate them by name, never by configuration, and so
does the platform: collection joins your staging prefix to the literal string
`__lspo_complete.json` and reads whatever is there (`external/io.py:221-229`). A receipt
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

**BEHAVIOUR.** Every container receives exactly these nine variables, in addition to any
variables the deployment declared and the agent's operator allowed
(`agent/runner.py:2287-2299`):

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
(`agent/runner.py:2295`). There is no variable called `LSPO_CREDENTIALS`; the agent has
never set one.

**RECOMMENDATION, with no working alternative.** Read your credentials path from
`LSPO_CREDENTIALS_FILE`. A node that reads `LSPO_CREDENTIALS` works only because its own
Dockerfile happens to define that name, and it dies the moment the line is dropped. No
platform check refuses you here; you simply have nothing to read. (The `node.py` in this
repository still has this defect; see [AUTHORING.md](AUTHORING.md#known-gaps-in-nodepy).)

**BEHAVIOUR.** `LSPO_INVOCATION_URI` and `LSPO_STAGING_PREFIX` are addresses, not access.
They are usually `s3://...` URIs, and your container holds no AWS identity, so it cannot
read or write them directly. They are useful in a log line and useless as an API. Read the
job description and write your outputs through the credentials envelope instead
(section 1.2).

**RULE.** Environment variables your deployment declares are named in the job description
but valued by the machine the agent runs on, and the agent's operator keeps an allowlist
(`LSPO_AGENT_ALLOWED_ENV`, exact names or glob patterns, empty by default which means
nothing is passed). A declared name that is not on the allowlist **fails the job before
your container starts**, naming the variable (`agent/runner.py:2274-2282`,
`agent/config.py:209-227`). A name that is allowed but simply absent from the agent's
environment produces a warning and the variable is not set
(`agent/runner.py:2283-2286`).

**RECOMMENDATION.** Do not require secrets through this channel if you can avoid it. There
is currently no way to give a container a third-party API key except by asking the agent's
operator to put it in that machine's environment and allowlist the name.

### 1.2 The credentials envelope

**BEHAVIOUR.** The agent writes one JSON document to the path in
`LSPO_CREDENTIALS_FILE`. The **directory** containing it is bind-mounted read-only into
your container; the file itself is not mounted, deliberately, so that the agent can
replace it under a running container and you will see the new one
(`agent/creds.py:10-16`, `agent/creds.py:88-98`).

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
(`runners/credentials.py:386-397` for the first, `runners/credentials.py:507-525` for the
second, constants at `:63-71`). Nothing here is a duty on you; it is what you will find in
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
of that. (Compare `runners/credentials.py:378-383` with
`external/contract.py:337-362`.)

**RECOMMENDATION.** Validate the envelope before trusting it: the `schema_version` you
implement, a `scheme` you support, a `staging.mode` you support, and the presence of the
fields you are about to use. Fail with a clear sentence rather than a `KeyError` five
frames deep.

**RECOMMENDATION.** Ignore fields you do not recognise rather than rejecting the
document. The orchestrator's own parsers are configured to ignore unknown fields
precisely so that adding a field is not a breaking change
(`external/contract.py:306-309`). Strict about the fields you know, tolerant about the
ones you do not: both, at the same time.

### 1.3 Where the credentials live, and who may read them

**BEHAVIOUR.** The agent creates one directory per job on its own disk, mode `0700`, with
the credentials file inside it at mode `0600`, and bind-mounts that directory read-only
into your container (`agent/creds.py:88-98`, `agent/identity.py:64-65`). The directory is
owned by the numeric uid the agent process runs as.

**BEHAVIOUR.** In object-storage mode the agent does **not** force your container's user;
your image runs as whatever `USER` it declares (`agent/runner.py:2973-2974` returns an
empty user for anything that is not local-path mode).

The consequence is a coupling that nothing in the platform manages or checks: a `0700`
directory owned by uid A is unreadable by a process running as uid B.

**RECOMMENDATION, with a hard consequence.** Your image's numeric uid must equal the
numeric uid the agent process runs as. In the shipped agent image that is **10001**
(`Dockerfile.agent`, `useradd --system --uid 10001 ... lspo`, then `USER lspo`), and it is
the only reason the example node works: `examples/hello-node/Dockerfile` in the
orchestrator independently chose the same number. So **build your image to run as uid
10001**, and check with whoever operates the agent, because an agent started directly on a
host rather than from that image runs as the invoking user, typically uid 1000, and then
10001 is the wrong answer. An image whose uid does not match gets permission denied on its
own credentials file, and the failure looks like a broken node rather than a mismatched
uid. Nothing in the platform detects or warns about this today.

**RECOMMENDATION.** Do **not** solve this by running as root. Root does bypass the
permission check, so it appears to work, and it is the wrong fix: there is no sandbox
around your container (see [OPERATIONS.md](OPERATIONS.md#residual-limits-stated-plainly)),
so a root workload is a root process on somebody's machine for no benefit. If uid 10001 is
impossible for you, say so to whoever operates the agent rather than escalating privilege.

**BEHAVIOUR, local demo mode only.** When the staging area is a local directory the agent
forces your container to the agent process's own uid and gid, with **no supplementary
groups** (`agent/runner.py:2991`). Your image's own user is ignored, so anything that
depends on it, most obviously writing under that user's home directory, works in
production and fails in the demo.

**RECOMMENDATION.** Write scratch files to `/tmp` or into your staging area, never into
your image user's home directory. `/tmp` is writable by any uid; a home directory is not.

---

## 2. Inputs

### 2.1 Reading the job description

**BEHAVIOUR.** `invocation.json` is written by the orchestrator before the job is
queued, and the writer refuses to publish one larger than 8 MiB
(`external/contract.py:76`, `external/io.py:148-159`). So it is bounded, and you may rely
on that.

**RECOMMENDATION.** Bound your read anyway: read at most 8 MiB and refuse a longer
document, rather than streaming an unbounded body into memory because a proxy returned
something unexpected.

**BEHAVIOUR.** The current contract version is `1`, and it is also given to you in
`LSPO_CONTRACT_VERSION`. In the platform's own parsers a missing `schema_version` means
version 1 (`external/versioning.py:102`), and a `schema_version` that is not a real
integer, notably JSON `true`, is malformed rather than version 1
(`external/versioning.py:105-108`). In Python `True == 1`, which is exactly the trap that
check exists to close.

**RECOMMENDATION.** Mirror that in your own reader: refuse a `schema_version` you do not
implement, treat a missing one as 1, and do not let a boolean pass as 1. Nothing checks
this on your side, so a node that ignores the version will one day parse a version 2
document under version 1 rules and produce plausible nonsense.

### 2.2 `invocation.json`, field by field

**RULE, and it governs all three tables in this section.** Every "Type" and "Required" cell
below is a check in the parser the orchestrator itself uses
(`external/contract.py:407-492`): a document that breaks one is invalid, and the job does
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

An input port (`external/contract.py:337-401`):

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | **yes** | the port name your code refers to. Today this is always `input` |
| `payload_kind` | string | **yes** | logical content type. Taken from the **first** upstream artifact's own kind, or the literal string `file` when that artifact carries no kind at all. It is not the node's `input_payload_kind` setting, which only selects which artifact gets pinned |
| `layout` | `"file"` or `"prefix"` | **yes** | today always `"file"` |
| `cardinality` | `"one"`, `"at_least_one"` or `"many"` | **yes** | today always `"many"`, and it is enforced by the parser |
| `objects` | array | no, defaults to `[]` | |
| `prefix_digest` | 64 hex chars, or `null` | required when `layout` is `"prefix"` | |

An input object (`external/contract.py:315-334`):

| Field | Type | Required | Notes |
|---|---|---|---|
| `uri` | string | **yes** | the true storage address; usually not readable from your container |
| `sha256` | 64 lowercase hex characters | **yes** | |
| `size` | integer >= 0 | **yes** | |
| `relpath` | canonical relative path, or `null` | no for a file port, **yes** for every object of a prefix port | |
| `upstream_execution_id` | integer >= 1, or `null` | no | which execution produced this object |

**BEHAVIOUR.** A step with no upstream artifacts gets `"inputs": []`, an empty list with
**no ports at all**, rather than one port with no objects
(`handlers/steps/external.py:336-337`). Your code must handle a job with no input port
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
never what you read** (`examples/hello-node/node.py:83-87` in the orchestrator says this
in as many words).

**RECOMMENDATION, and the single most valuable one in this document.** Verify each input
against its pinned `sha256` and `size` before you act on it, and refuse an input that
arrives with no pin. A presigned URL points at a key, and "the bytes at that key today"
is not automatically "the bytes that were there when this job was built". A node that
skips this check can process the wrong version of its input and produce output that
passes every check the platform makes on the way back, because those checks are about
what you wrote.

**BEHAVIOUR.** Two inputs can arrive with the **same** `relpath` and the same `name`. The
orchestrator sets an input's relpath to the last segment of its URI
(`handlers/steps/external.py:381`), and nothing de-duplicates within a file-layout port;
the uniqueness check exists only for folder ports (`external/contract.py:378-383`). Two
upstream steps that both produce `rows.csv` therefore collide.

**RULE.** Your output relpaths must be unique within one marker
(`external/contract.py:571-579`).

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
(`agent/executors/docker_exec.py:248-274`). A node that downloads every input to `/tmp` and
keeps it there can therefore fill the disk of the machine the agent runs on, which is
somebody else's laptop or server. Delete each input when you are done with it, or stream it
and never land it at all.

**BEHAVIOUR, and there IS a ceiling on the NUMBER of them, indirectly.** Every input, with
its URI, its hash, its size and its relpath, is written into the job description, and the
writer refuses to publish one larger than **8 MiB** (section 2.1,
`external/io.py:148-157`). So the input count is bounded after all, by a limit that depends
on how long your URIs are. Measured against the real models at the commit these documents
were verified at: about **330 bytes per pinned input object** with 95-character URIs, which
puts the ceiling at roughly **25,000 inputs** — 25,445 fitted, 25,446 did not. Past it the
step fails **before the job is created**, at the moment the orchestrator tries to write the
job description, so no container ever starts and there is nothing for your node to handle.
It is not a limit you can design around; it is one to know about before wiring a node
downstream of something that produces tens of thousands of files.

### 2.4 How long you actually get

**BEHAVIOUR.** `timeout_seconds` is what the node's configuration requested. The absolute
deadline is stamped when a runner **claims** the job, not when the job description was
written, because the wait for capacity is unbounded and is not the step's to spend. Your
container cannot observe that deadline. Note also that pulling your image happens after
the claim, so a slow pull is spent out of your budget.

**BEHAVIOUR, and the number that matters is 900, not 3600.** The budget is resolved in
three steps: the pipeline node's own `timeout_seconds` if it sets one, otherwise the
budget the registered revision declares, otherwise a platform fallback of 3600 seconds
(`handlers/steps/external.py:221-239`, `runners/jobs.py:67-81`,
`LSPO_EXTERNAL_DEFAULT_TIMEOUT_S` at `lspo/settings/base.py:562`). **The fallback is
almost never reached**, because both ways of registering a node write a budget into the
revision, and both write the same one: **900 seconds** (the setup command,
`noderegistry/management/commands/external_demo_setup.py:38`, and the Settings screen's
registration API, `noderegistry/serializers.py:26`). So unless somebody deliberately
raised it, **your container is stopped fifteen minutes after the job was claimed**, and
that is reported as a failure rather than as a cancellation (section 7).

**BEHAVIOUR, and it is why section 4.3 reads the way it does.** The credential envelope's
own lifetime is also 900 seconds, and it is additionally clamped so that it can never
outlive the run's deadline (`runners/credentials.py:216-219`). With both numbers at 900
the two coincide: on a default registration the first envelope you are handed already
expires at the moment your container is killed anyway, so a node that never re-reads its
credentials will not visibly fail on that account — it will simply be terminated. Raise
the budget (a `timeout_seconds` on the pipeline node, which takes effect immediately) and
the two come apart at once: credentials still last 900 seconds, the run lasts as long as
you asked for, and a node that read its credentials once can no longer upload anything
after the first fifteen minutes. **That is the case worth writing your node for**, because
it is the one an operator creates the first time a real job needs more than a quarter of
an hour.

**RECOMMENDATION.** Measure your own elapsed time from process start and aim to finish,
including uploads and the marker, comfortably inside `timeout_seconds` — the value in the
job description is the real answer for this run, whatever the defaults are.

---

## 3. Doing the work

### 3.1 Your configuration

**BEHAVIOUR.** `params` in the job description is the only part of the node's
configuration that ever reaches your code. It is copied verbatim from what the pipeline
author typed, with no filtering and no redaction
(`external/contract.py:425-426` and `:457`, `pipelines/config_schemas.py`). Everything else about the
node, including which deployment it points at, stays on the orchestrator's side.

**RECOMMENDATION.** Validate `params` yourself and fail with a readable message. Nothing
between the pipeline author and your code checks its shape.

### 3.2 What your container may reach

**BEHAVIOUR.** Your container has ordinary outbound networking unless the agent's operator
put it on a restricted docker network (`LSPO_AGENT_NETWORK`, empty by default which means
the daemon's default network).

**BEHAVIOUR.** The agent's own credentials never enter your container. The only access you
are given is what the envelope carries (`agent/README.md`, "Least privilege").

**BEHAVIOUR.** Resource ceilings, applied by the agent to every workload container and
configurable by its operator (`agent/config.py:123-136`,
`agent/executors/docker_exec.py:250-262`): 2 CPUs (`LSPO_AGENT_CPUS`), **2 GiB of memory**
(`LSPO_AGENT_MEMORY`, default `2g`), 512 processes (`LSPO_AGENT_PIDS_LIMIT`), and
`no-new-privileges`.

### 3.3 Logging

**BEHAVIOUR.** Your stdout and stderr are merged into one stream, split on newlines, and
shipped to the orchestrator by the agent (`agent/executors/docker_exec.py:321-363`). They
appear live in the run view and are written into a durable log file stored with the
execution when the run ends (`orchestrator.ndjsonl`, attached to the execution as an
artifact — `pipelines/external_finalize.py:2203-2212`).

**BEHAVIOUR, and each of these loses data:**

* A line longer than **64 KiB** is flushed with a visible truncation marker and **the rest
  of that line is discarded** (`agent/executors/docker_exec.py:95-99`, `:328-335`). One
  giant JSON blob on a single line loses its tail.
* The agent holds at most **2000 lines** per job between heartbeats; when it overflows the
  **oldest** are dropped and a warning line records how many
  (`agent/logbuf.py:38-119`, capacity from `LSPO_AGENT_LOG_BUFFER_LINES`).
* Heartbeats are every **20 seconds** and carry at most **100 lines** each. If you produce
  more than five lines per second on average, you are losing the excess.
* Each line is cut to **4096 characters** on the way through the agent, and the server
  **refuses** rather than trims a batch that breaches its own ceilings: at most 100 lines,
  4096 bytes per line, 128 KiB per batch, 256 KiB per request body
  (`runners/serializers.py:38-53`, `runners/auth.py:71`). A refused heartbeat costs the
  lease renewal it was carrying, not just the log lines.
* The live view keeps only the last **1000** entries — and **so does the durable file**.
  The lines are held in a buffer that is trimmed to its most recent 1000 entries every
  time one arrives (`pipelines/log_stream.py:38`, `:329-355`), and the file written at the
  end of the run is built from that same buffer
  (`pipelines/external_finalize.py:2173-2200`). A container that prints a hundred thousand
  lines has lost ninety-nine thousand of them before anything durable is written. **There
  is no complete copy of your output anywhere.**
* The copy kept on the execution row itself is smaller again, bounded by both a line count
  and a byte count, and it keeps the **first** few lines plus the newest that fit, with a
  marker at the cut saying how many went (`pipelines/external_finalize.py:2215-2300`).

**RECOMMENDATION.** Say the important things once, at the end, in few lines. A step that
prints one line per record will lose its beginning and will not notice.

**BEHAVIOUR.** Your lines are stored as **text**, and control characters are removed from
them on arrival — every C0 character except tab and newline
(`runners/reports.py:575-599`, `external/text.py:44` and `:67-81`). The removal is silent and
nothing is replaced in their place. The practical consequence is colour: an ANSI escape
sequence loses its leading escape byte and keeps the rest, so a line you meant to print in
red is stored as `[31mfailed[0m`.

**RECOMMENDATION.** Print plain text with no terminal control of any kind — no colour, no
progress bar that redraws itself with carriage returns, no spinner. None of it survives,
and what is left of it is noise in a log somebody is reading to find out what your step
did.

**BEHAVIOUR, and it surprises everyone.** Your container's log lines are shipped
**unredacted**. The agent redacts URLs in messages it composes itself and in the
completion report's error text (`agent/redact.py`, `agent/runner.py:2111`, `:2129`,
`:2161`), but the workload's own stdout goes straight into the buffer with no redaction at
all (`agent/runner.py:2164-2166`).

**RECOMMENDATION, and treat it as non-negotiable even though nothing enforces it: never
print a presigned URL, a token or a credential.** A presigned URL's query string **is** a
read credential for that object, and these logs are durable, shown to everyone who can see
the run, and searchable. Nothing in the platform will stop you or warn you.

**RECOMMENDATION.** This is easier to get wrong than it sounds. Popular HTTP clients put
the full URL, signature included, into the text of an HTTP error. Catch transport
exceptions and log your own sentence, with the URL removed or reduced to scheme, host and
path.

### 3.4 Progress

**BEHAVIOUR, opt-in.** A single stdout line of exactly this shape is consumed by the agent
and reported as progress on the next heartbeat (`agent/logbuf.py:34`, `:65-92`):

```
@lspo:progress {"fraction": 0.4, "phase": "encoding"}
```

* The prefix is `@lspo:progress ` **including the trailing space**.
* The rest of the line must be a JSON object with a `fraction` that converts to a float
  between `0.0` and `1.0` inclusive.
* `phase` is optional, coerced to a string, stripped of control characters and cut to 64
  characters (`agent/logbuf.py:81-91`, `runners/serializers.py:56-73`).
* A malformed progress line is **not** consumed: it appears in your log as an ordinary
  line. That is deliberate, so a typo is visible rather than silent.
* Only the most recent sample is reported per heartbeat.

**BEHAVIOUR.** A node that emits none simply has no progress. The agent never invents a
fraction from elapsed time. Nothing requires you to report progress.

**RECOMMENDATION.** Emit progress for anything that runs longer than a minute or two. It
is the only signal an operator has that a long step is alive.

### 3.5 Memory: two defaults that collide

**BEHAVIOUR.** The largest single object you may upload is **1 GiB**
(`LSPO_EXTERNAL_MAX_OBJECT_BYTES`, default `1024*1024*1024`, `lspo/settings/base.py:581`),
enforced by the upload policy itself and re-checked at collection. The default memory
limit for your container is **2 GiB** (`LSPO_AGENT_MEMORY`, default `2g`).

Those two numbers are the whole warning. An object the platform explicitly permits, held
in memory once as input bytes and once as an upload body, is 2 GiB of resident memory
against a 2 GiB limit. The kernel kills the container, and **an OOM kill writes no
marker**, so the run dies with a bare exit code and no account of itself. Nothing in the
orchestrator's limits is exceeded on the way there.

**RECOMMENDATION.** Stream in both directions. Read inputs to a temporary file or in
chunks, hash while you stream, and upload from a file handle rather than from a `bytes`
object. Do this even for a node that "only handles small files": the input size is chosen
by whoever wires the pipeline, not by you.

---

## 4. Outputs

### 4.1 Where your output goes

**BEHAVIOUR.** Everything you produce is addressed by a **relpath** relative to your
staging prefix. The staging prefix is scoped to this execution, this attempt and this
generation, and ends in `attempts/<attempt>/gen-<generation>` for exactly that reason: a
superseded runner physically cannot write into the live attempt's area
(`external/contract.py:476-492`).

**RULE.** A relpath must be canonical (`external/contract.py:236-283`):

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
(`external/contract.py:218-233`). A port name is an identifier twice over — a downstream
step selects its input by matching that string, and the delivered artifact is stored under
it — so it is held to the same standard as a path.

**BEHAVIOUR, and it catches people.** The name is checked for emptiness *after* trimming
but is **not** trimmed: `" output "` passes, and then travels onward with its spaces intact,
as the artifact kind a downstream step has to match exactly. This was exercised against the
parser. Trim your own port names.

**RULE.** Relpaths listed under `produced_ports` are held to the full canonical-relpath
rule as well, not merely to "is it in the inventory" (`external/contract.py:567`). There
is no spelling that is legal in one place and not the other.

**RULE.** A relpath is checked twice: once when your marker is parsed, and again when
collection resolves it against the staging prefix
(`pipelines/external_finalize.py:1384-1416`). Both refuse traversal.

**BEHAVIOUR.** A relpath containing a `%` or a space behaves differently on the two
storage backends. Object-store keys are joined raw; local file URIs are percent-quoted per
segment (`pipelines/external_finalize.py:1363-1381`). This was a real bug: a file named
`rate%20.csv` was looked for at `rate .csv`.

**RECOMMENDATION.** Keep relpaths to unaccented letters, digits, `-`, `_`, `.` and `/`.
You gain nothing from an exotic name and you inherit two backends' disagreements.

### 4.2 Uploading, in object-storage mode

**BEHAVIOUR.** `staging.post` is a presigned form POST policy. Its conditions are
(`runners/credentials.py:462-472`):

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

**BEHAVIOUR.** An envelope is valid for `LSPO_RUNNER_CREDS_TTL_S`, **default 900 seconds**
(`lspo/settings/base.py:575`), clamped so that it never outlives the job's own runtime
deadline (`runners/credentials.py:216-219`).

**BEHAVIOUR, and it is the failure this whole section exists for. A node that reads its
credentials once cannot upload its outputs, or its own completion marker, after about
fifteen minutes.** This is the most common way a working node fails on
its first long job — but only on a job that is *allowed* to be long. Section 2.4 has the
arithmetic: a node registered the default way is given a 900-second runtime budget, the
same 900 seconds the credentials last, so the two expire together and the container is
killed at the same moment its credentials die. The failure appears the first time an
operator raises the budget, which is exactly when the node is finally being asked to do
something substantial. Write for that case now; it is not a hypothetical, it is the second
week.

**BEHAVIOUR.** The agent asks the orchestrator for a fresh envelope when the current one
is within 60 seconds of expiring (`LSPO_AGENT_CREDS_REFRESH_MARGIN_S`,
`agent/creds.py:107-115`) and replaces `creds.json` atomically underneath you, by writing
a new file and renaming it over the old one (`agent/creds.py:90-98`). **You are not
signalled.** The directory is mounted rather than the file precisely so that the
replacement is visible to a process that reads the path again.

**BEHAVIOUR.** Installing a refreshed envelope does **not** revoke the URLs from the old
one. A presigned URL stays valid until its own expiry, whatever happens to the file it
came from.

**RECOMMENDATION.** Respect the envelope's stated `expires_at`: re-read the credentials
file when you are at or near it, and before writing the marker at the end of a long run.
Re-reading before literally every transfer is legal but unnecessary, and a caching
implementation that watches `expires_at` is perfectly correct.

**RECOMMENDATION.** Recover from a refusal. On an unambiguous expiry or authorization
rejection, re-read the credentials file and retry **once, only if the envelope actually
changed**. If it did not change, fail transiently rather than looping. Never blindly
retry an ambiguous POST transport failure: the upload may already have been accepted.

**RECOMMENDATION, and it is where the obvious implementation goes wrong.** "Did the
envelope change?" is a question about the document's **contents**, not about the object
your program is holding. Re-reading the file parses fresh objects every time, so a
comparison by object identity — Python's `is`, JavaScript's `===` on the parsed result —
is always "different", and the guard you thought you wrote never fires: it retries on
every refusal, including the ones where nothing changed. Compare the documents themselves,
or the part of them that actually grants access (`staging.post.fields`, which carries the
signature). `expires_at` alone is the weakest of the three, because it is clamped to the
run's deadline (section 2.4) and two envelopes issued near the end of a run can state
almost the same moment.

**BEHAVIOUR, worth knowing when you debug a clock problem.** A presigned **GET** can
outlive the `expires_at` the envelope states, by up to the budget that was left when it
was signed, because credentials resolve inside the signer before the expiry date is
stamped. The upload **POST** policy cannot: its expiry is computed before signing
(`runners/credentials.py:187-210`). So treat `expires_at` as exact for writes and as a
lower bound for reads.

### 4.4 `result.json`

**BEHAVIOUR. Nothing in the orchestrator ever reads `result.json`.** There is no caller of
the contract's own `read_result` anywhere outside the contract package. The run's metrics
come from the completion marker and the objects that were actually published: the
execution records `objects_published`, `bytes_published` and `exit_code`
(`pipelines/external_finalize.py:2112-2118`).

**BEHAVIOUR.** Because nothing reads it, the contract's 1 MiB ceiling for this document is
not enforced against you. It becomes an ordinary object of yours, subject to the 1 GiB
per-object ceiling like any other.

**BEHAVIOUR.** If you list `result.json` in the marker's `objects` it is copied and
verified like any other object. If you also claim it under an output port, it becomes a
downstream artifact under that port's name. Both are legal. The orchestrator's own test of
the example node expects exactly that.

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
(`pipelines/external_finalize.py:2678-2699`). Nothing requires you to write one.

**RECOMMENDATION.** If your step's own output matters, write it as a file and inventory
it. The platform's record of your stdout is a tail of the last 1000 lines and nothing more
(section 3.3); a file you upload and claim under a port is kept whole, verified, and
delivered.

---

## 5. The completion marker

**BEHAVIOUR.** The marker is `__lspo_complete.json`, read from the **root** of your staging
prefix — collection joins that prefix to the literal filename and reads whatever is there
(`external/io.py:221-229`). It is the terminal receipt for the attempt.

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
parser makes (`external/contract.py:519-568`); a marker that breaks one is not read at all.
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
| `error` | string or `null` | **no** | quoted into the failure reason, cut at 500 characters (`pipelines/external_finalize.py:181`, `:1618-1629`). Control characters are removed from it |

**RULE.** On a run the platform classifies as **succeeded**, which normally means your
process exited 0 and nothing cancelled or timed it out, a marker is **required**. A missing
marker after a reported success fails the execution: there is no inventory, so there is
nothing to publish and nothing to verify (`pipelines/external_finalize.py:1083-1087`).

**RULE.** On such a run the marker's own `status` must be `"succeeded"`. A process that
exits 0 while its marker says `failed` fails the execution
(`pipelines/external_finalize.py:1088-1092`).

**RULE.** `execution_id`, `attempt` and `generation` must equal the launch the platform
believes it is collecting (`pipelines/external_finalize.py:1093-1099`). Copy them from the
job description, not from memory of an earlier attempt. A mismatch is refused with "a
receipt from a superseded or unrelated run must never be collected as this one".

**RULE.** Every relpath in `produced_ports` must appear in `objects`
(`external/contract.py:582-606`). The inventory is what carries the hash and the size.

**RULE.** Within one port a relpath may appear only once; the parser refuses a repeat
(`external/contract.py:594-605`). The **same relpath in two different ports is legal** and
is sometimes what you want.

**RULE.** Relpaths in `objects` are globally unique across the whole inventory
(`external/contract.py:571-579`). One file, one entry.

**RULE.** Every port name is non-empty after trimming and carries no control characters,
and every relpath under a port is canonical — the same rules as section 4.1
(`external/contract.py:218-233`, `:567`).

**RULE.** The marker document must be at most **8 MiB**; the reader refuses a larger one
(`external/contract.py:77`, `external/io.py:162-172`). For a very wide batch, that bounds
how many objects one attempt can inventory.

**RULE.** Every `sha256` is exactly 64 lowercase hexadecimal characters, with no `sha256:`
prefix, no uppercase and no trailing newline (`external/contract.py:129`, matched with
`fullmatch` because in Python a `$` anchor also matches before a trailing newline).

**RULE.** Every integer is a real JSON integer. `"1"`, `true` and `1.0` are all rejected,
and every id and counter must be at least 1 (`external/contract.py:297-303`).

**BEHAVIOUR.** `error` is **cleaned, not refused**: control characters other than tab and
newline are removed from it as the document is parsed
(`external/contract.py:608-636`). The asymmetry with names (section 4.1) is deliberate.
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
the hash and size you promised (`pipelines/external_finalize.py:1103-1152`, `:1297-1323`).
A marker written before its objects were finished fails there, as a size or hash mismatch.

The reason to do it anyway is what the invariant buys: **the marker's existence is the
only thing that distinguishes a half-finished run from a complete one.** Write it last and
that whole class of failure is impossible rather than merely detected. The contract module
itself states it as an instruction to you — "the step writes it **strictly last**"
(`external/contract.py:519-526`) — and the orchestrator's test suite even asserts the
order for the one node it ships (`tests/test_hello_node_example.py:107-115`). Neither of
those is a check on *your* node.

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
(`pipelines/external_finalize.py:2043-2065`), so an unclaimed object does not appear
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

**BEHAVIOUR, and it decides when the recommendation above actually pays.** That marker is
read on every path where your own terminal report is what ends the job: a failure of your
own, a container stopped by the **runtime deadline**, and a cancellation your node declared
itself by exiting 20 without being asked. It is **not** read when an **operator** cancels
the run, because the platform has already ended the job before your report arrives — see
section 7. Write the marker anyway; it costs one document and it is what makes the first
three cases recoverable.

**BEHAVIOUR, and a quiet way to lose everything.** A failed or cancelled marker is held to
the same identity check and the same document rules. "Best effort" applies to which objects
survive, not to whether the document is valid. But on this path an unreadable, invalid or
mismatched marker is **ignored silently** rather than reported
(`pipelines/external_finalize.py:1487-1520`): the run fails with a generic reason, your
`error` text never appears, and nothing you produced is salvaged. So a malformed failure
marker costs you exactly the information it existed to carry, and says nothing about
itself.

---

## 6. Exit codes

**BEHAVIOUR.** The classes (`external/contract.py:86-120`):

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
your process returned (`pipelines/external_finalize.py:1645-1649`, `:1675`), and an
operator retries by hand from the run view. A future automatic retry path may honour exit
10 without any change to your node.

**RECOMMENDATION.** Use the codes as they are defined anyway. Exit 1 for a condition a
second run might survive, such as a storage timeout or a rate limit. Exit 10 for a
condition no retry can fix, such as an input your node cannot parse or a configuration
that is wrong. Treating 1 and 10 as interchangeable produces images that misbehave on the
day retries land.

**RECOMMENDATION.** In-node retry of a transient storage failure is optional. The
contract's own mechanism for "try this again" is exit code 1, which asks the platform to
retry the attempt. What is genuinely wrong is misclassifying: reporting a transient
storage condition as permanent tells the platform never to retry something that would
have worked.

---

## 7. Cancellation

**BEHAVIOUR, and it is the first thing to know here, because the natural guess is wrong.**
Two different events stop a container that has not finished, and they do not behave alike.
The **runtime deadline** is a polite stop every time: SIGTERM, then SIGKILL thirty seconds
later. An **operator pressing Cancel** usually is not: in the ordinary case your container
is killed outright, with no signal it can catch and no opportunity to write anything.
Which of the two a cancellation turns out to be is decided by a race that nothing in your
node can influence. Both are set out below; a third way to be stopped, fencing, is section
7.1.

**BEHAVIOUR, the dependable polite stop: the runtime deadline.** When the deadline passes
the agent asks docker to stop your container — SIGTERM, then SIGKILL after **30 seconds**
(`agent/runner.py:2317-2324` calling `agent/executors/docker_exec.py:405-415`, the default
timeout, which the caller does not override). Nothing has to arrive from anywhere for this
to happen: the agent watches the clock itself, in the same loop that waits for your
container, so it fires even when the orchestrator is unreachable.

**BEHAVIOUR.** A container stopped for its deadline is classified `failed`, not
`cancelled`, and that question is asked before your exit code is looked at
(`agent/runner.py:2332-2333`), so nothing your process exits with can turn it into a
cancellation. On a default registration the deadline is the fifteen-minute mark; see
section 2.4. **The marker you manage to write on the way out is read**: its `error` becomes
the execution's failure reason and its inventoried objects are salvaged (section 8). This
is the path on which a SIGTERM handler pays for itself in full.

**BEHAVIOUR, and this one is measured rather than assumed.** Your program almost certainly
runs as **PID 1** inside its container: the agent overrides neither the entrypoint nor the
command and does not ask docker for an init helper
(`agent/executors/docker_exec.py:248-274` contains no `entrypoint`, `command` or `init`
key), so an exec-form `ENTRYPOINT` makes your process number 1. This is where an operating
system property bites: Linux gives process 1 no default signal dispositions, so SIGTERM is
discarded unless you installed a handler for it. That is a kernel rule rather than
something the platform does, so it is stated here as a fact about Linux and not as a
citation of our code. A node that installs no handler does not die when it is asked to
stop. It **ignores the request entirely**, finishes the whole batch, and writes a
`succeeded` marker after the platform asked it to stop, unless the work outlasts the 30
second grace, at which point it is killed outright. This was measured by adding a one-line
handler to the example image, after which it exited at once.

**BEHAVIOUR.** The platform's classification is authoritative and it asks "was
cancellation requested?" **before** it looks at your exit code
(`agent/runner.py:2326-2340`). So a cancelled run stays cancelled whatever you exit with,
and exit code 20 is **not required** for that. The order of questions is: was this job
fenced, was the agent forced to exit without waiting, did the runtime deadline pass, was
cancellation requested or was the exit code 20, was the exit code 0, did the container
vanish, and only then the exit code's own class. (The second of those is a special case: on
a forced agent exit nothing is reported at all, because the agent leaves your container
running for its successor to adopt rather than ending the job — see section 7.1.)

**BEHAVIOUR, and here is why operator cancellation is different from the deadline.** When
an operator presses Cancel on a step that is running on a runner, the orchestrator does not
merely ask your container to stop. In **one database transaction** it raises a cancel flag
the agent can read, moves the **launch** — the platform's record that this agent holds this
job — to a terminal state, and finishes the execution as cancelled
(`pipelines/cancellation.py:87-119`, `:299-350`). All three land together, so there is no
moment at which the flag is visible and the job is still live.

**BEHAVIOUR.** The only channel that reaches a process on somebody else's machine is the
answer to the agent's **heartbeat**, which carries a `cancel` flag
(`runners/reports.py:210-217`); on seeing it the agent asks docker for the same polite stop
the deadline uses (`agent/runner.py:2500`, `:2528-2536`). But a heartbeat is answered at
all only if it can first renew the job's lease, and a lease cannot be renewed on a launch
that has gone terminal — such a heartbeat is refused with `lease_lost`
(`runners/reports.py:188`, `:294-316`).

**BEHAVIOUR, and this is the consequence.** The agent's response to `lease_lost` on a
heartbeat is not a polite stop. It **fences** the job: one SIGKILL, no SIGTERM, no grace
period, and no terminal report at all (`agent/runner.py:2465-2466`, then `:2553-2589`
calling `agent/executors/docker_exec.py:417-440` — it is the last row of the table in
section 7.1).

**BEHAVIOUR, so cancellation is a race, and the odds are against the polite path.** For the
cooperative stop to happen, a single heartbeat has to straddle the cancelling transaction
exactly: renew its lease **before** that transaction commits and read the cancel flag
**after** it, which is possible only because the flag is read after the renewal and outside
its transaction (`runners/reports.py:188-212`). A heartbeat that begins a moment later is
refused and fences instead. A heartbeat that finishes a moment earlier is answered `cancel:
false`, and then the *next* one — up to a full interval later — is the one that gets
refused. Heartbeats are **20 seconds** apart by default (`agent/config.py:123`) and each is
a single HTTP request, so that window is a small fraction of the interval. **The ordinary
outcome of pressing Cancel is that your container is killed outright.**

**BEHAVIOUR.** There is a second, slower route to the same kill. The agent periodically
re-reads the orchestrator's list of jobs assigned to it and fences everything the list does
not name (`agent/runner.py:1112-1163`), and a cancelled launch has already dropped off that
list (`runners/claim.py:804-817`). That fence is also a SIGKILL, though it keeps permission
to report. It runs on a **120 second** timer by default (`agent/config.py:124`), so it
normally arrives long after the heartbeat has fenced the job.

**RECOMMENDATION, worth building even though it is not guaranteed to run.** Handle a stop
request, in this shape:

1. Install a SIGTERM handler that sets a flag. Do not do the work in the handler.
2. Have your normal control flow check the flag between units of work, and stop.
3. Keep the inventory of what you already uploaded (see the next point).
4. Re-read your credentials, write a `cancelled` marker **last**, and exit 20.

**BEHAVIOUR, and it is what that handler is actually worth.** On the **deadline** path
everything the handler does is collected: the marker is read, its `error` becomes the
failure reason, its objects are salvaged. On an **operator cancellation** none of it is —
see the next statement. So write the handler for the deadline, for the narrow cancellation
window, and so that a stopped container stops rather than churning through work nobody
wants; not because a cancelled run will deliver its partial output.

**BEHAVIOUR, and it is the second surprise in this section.** An operator cancellation
**never collects your marker or your objects**, on either branch of the race above.
Collection is armed by the agent's terminal report, and only when that report is the thing
that ends the launch (`runners/reports.py:428-436`). After a cancellation the launch is
already terminal, so a report arriving afterwards is answered "already finished" and arms
nothing (`runners/reports.py:389-390`, `:482-499`). The periodic sweep that rescues
interrupted collections does not reach this case either: its scans skip a terminal attempt
and require a non-terminal execution, and the cancelling transaction makes both terminal at
once (`pipelines/external_finalize.py:513-540`). The collector's own source says it in as
many words — "A direct cancellation never reaches collection"
(`pipelines/external_finalize.py:2488-2497`).

**BEHAVIOUR.** So a node that receives the SIGTERM in the narrow window, stops cleanly,
writes a valid `cancelled` marker inventorying everything it had uploaded, and exits 20,
has that marker read by nobody. Its objects stay in a staging area that expires.

**BEHAVIOUR.** What an operator cancellation does keep is your **log**. The lines your
container streamed are written to durable storage and onto the execution before the live
buffer is dropped (`pipelines/external_finalize.py:2488-2537`, called from
`pipelines/cancellation.py:180-189`). That is the whole of what survives a cancellation.

**BEHAVIOUR, the one exception, and it is not about a running node.** If the cancellation
arrives while collection is **already under way** — your container has exited and reported,
and the platform is part-way through copying its objects — the collection is deliberately
allowed to finish, and what it verified is attached to the cancelled execution as
diagnostics carrying no output port (`pipelines/cancellation.py:313-318`,
`pipelines/external_finalize.py:1932-1999`). Nothing downstream receives it. Your process is
long gone by then, so there is nothing here for your node to do.

**RECOMMENDATION.** Reserve part of the 30 second grace for step 4, and make sure your
network calls cannot swallow it. A cooperative flag cannot be checked while you are
blocked in a socket read: a 120 second read timeout against a 30 second grace means a stop
that lands during a transfer produces neither a marker nor any salvage. Use timeouts and
chunk sizes well under the grace period.

**RECOMMENDATION, and this one decides whether partial work survives at all.** Accumulate
your object inventory somewhere the failure and stop paths can still see it, not in a local
variable of the function that does the work. On every path where salvage happens — your own
failure, the runtime deadline, a cancellation you declared yourself — it publishes **only
what the marker inventories** (`pipelines/external_finalize.py:1523-1581`). A node that
uploads three objects, fails on the fourth, and then writes a marker with an empty
inventory has left those three objects in a staging area that expires, and nothing will
ever collect them.

### 7.1 Fencing: the stop with no grace period at all

**BEHAVIOUR.** The runtime deadline is a polite stop, and cancellation sometimes is. There
is a third way your container is stopped, and it is never polite.

**BEHAVIOUR.** On the second path your container is **killed outright** — one SIGKILL, no
SIGTERM first, no thirty seconds, no opportunity to write anything
(`agent/executors/docker_exec.py:417-440`, called from `agent/runner.py:2553-2589`). The
agent calls this **fencing**, and it does it whenever it concludes that it no longer speaks
for your job, because the one thing the design will not tolerate is two processes writing
into one output area.

**BEHAVIOUR.** There are six triggers, and they do **not** all have the same consequence.
What separates them is whether the agent still has the standing to say how the job ended:
some fences take the *work* away while leaving the agent's own credential valid, and on
those the agent still sends a terminal report, which is what lets collection run at all.

**BEHAVIOUR, and read the last column literally.** "Yes" means only that the agent
**retains permission to attempt a report**. It does not mean the report is accepted, and it
does not by itself mean anything is collected. The completion endpoint is fenced by lease
id and session epoch and answers 409 to an agent that has been superseded
(`runners/reports.py:391-403`), and a report arriving after the launch has already gone
terminal by some other route is answered "already finished" and arms no collection at all
(`runners/reports.py:389-390`, `:482-499`). "No" is unambiguous; "Yes" is a permission
rather than an outcome.

| What happened | Container | May the agent still report? |
|---|---|---|
| The orchestrator became unreachable and the job's lease ran out (`agent/runner.py:2538-2551`) | SIGKILL | **No.** There is nobody reachable to tell |
| The job stopped being listed as assigned to this agent — revoked, or its launch already went terminal elsewhere (`agent/runner.py:1112-1163`) | SIGKILL, delivered a moment later by the sweep that matches containers by name, or by the start path if no container exists yet | **Yes**, deliberately: the report is what frees the job's capacity slot |
| The organization that owns the job was switched off, arriving as a 403 (`agent/runner.py:2661-2677`) | SIGKILL | **Yes**, for the same reason. Other jobs on the same agent are untouched |
| The agent's own identity was refused — a rotated or retired runner, a drained pool (`agent/runner.py:1690-1767`) | SIGKILL, for **every** job it holds, then the agent exits | **No.** The credential it would report with is exactly what stopped being recognised |
| The agent's heartbeat thread failed — the measured case was its disk filling up while writing your refreshed credentials (`agent/runner.py:2376-2393`) | SIGKILL | **Yes**, carrying the real reason: "the disk was full", not a blank failure |
| A lease-lost or job-not-found answer to one of the agent's own calls — starting the job, a heartbeat, a credential re-issue, the final report (`agent/runner.py:2116`, `:2466`, `:2687`, `:2824`) | SIGKILL | **No** |

**BEHAVIOUR, and it is worth stating because the opposite is the natural guess.** An
ordinary agent shutdown is **not** a fence. The first signal to the agent means "finish
what you are doing": it stops claiming new work and then waits for the jobs it holds, with
no deadline at all, because a step may legitimately run for hours
(`agent/runner.py:776-814`, `:847-880`). A forced second signal makes the agent exit at
once and **deliberately leaves your container running**, addressed by a name the next agent
will recognise, so that agent adopts it and the work is not thrown away. Your container is
neither killed nor signalled on either path.

**BEHAVIOUR, and this is the part to plan around.** A SIGKILL cannot be caught, handled or
delayed, so a fenced container writes nothing further — no marker, no last object, not a
line of log. What is collected afterwards is therefore exactly what a **valid marker for
this attempt had already recorded**, if one was in the staging area when the kill landed,
and nothing else: collection reads that marker, checks that its `execution_id`, `attempt` and
`generation` name this attempt and not a superseded one, and then copies and verifies every
object it inventories, keeping what verifies (`pipelines/external_finalize.py:1459-1482`,
`:1523-1581`). On the three fences whose report is accepted, that collection happens
immediately.

**BEHAVIOUR, and it is worse than the previous paragraph sounds.** On the fences that
report nothing, **nothing is ever collected**. There is no watchdog
([OPERATIONS.md](OPERATIONS.md#residual-limits-stated-plainly)), so the execution simply
stays parked at "Waiting for runner" until an operator cancels it — and cancelling does not
run collection either (section 7): it writes the log down and finishes the run. Whatever
your container had already uploaded, and any marker it had already written, are left in a
staging area that expires.

**BEHAVIOUR, so the summary is narrower than "everything is lost", and for a well-behaved
node it usually amounts to the same thing.** If you follow the strongest recommendation in
this document and write your marker last, then when a fence lands mid-run there is no
marker, and nothing you produced is collected. The salvage path is not dead code — it is
what recovers the work of a node that failed, or hit its deadline, or stopped itself, and
wrote a marker on the way out (section 5) — it simply has nothing to read after a SIGKILL
that arrived first. What is never true is that a fence gives your process a chance to
react.

**RECOMMENDATION.** Do not design a node whose entire output appears in its last minute.
Nothing you can write survives a SIGKILL, so the only defence is to have less at risk when
it lands: finish and upload work in units, keep runs comfortably inside their budget, and
emit progress (section 3.4) so that an operator watching a long step can see it is alive
rather than cancelling it on suspicion.

---

## 8. What happens after you exit

This section is **background**: nothing in it is yours to implement, and knowing it explains
most of the failure messages you will see.

**BEHAVIOUR.** The agent reports your exit code and its own classification. On a success
the orchestrator reads your marker, checks its identity, and then for every object in
`objects`:

1. copies it from staging into a **published** area your credentials cannot reach;
2. re-reads it **there** and holds it to the size and hash your marker promised
   (`pipelines/external_finalize.py:1103-1152`, `:1297-1323`).

Copy first, verify second, because your upload policy is still live and a hash taken in
staging is a statement about the past.

**BEHAVIOUR, on a successful run.** Any single object that fails verification causes
**everything this collection published to be deleted** and the execution to fail
(`pipelines/external_finalize.py:1002-1007`, `:1326-1348`). A partial delivery is worse
than a failed one, because nothing downstream can tell which it got.

**BEHAVIOUR, on a failed or cancelled run — when collection runs at all.** The same copy
and verify runs per object, and whatever verifies is kept while the rest is dropped with a
log line (`pipelines/external_finalize.py:1523-1581`). Salvaged objects are attached to the
execution as diagnostics: they carry no output port and are not offered to any downstream
step. Section 7 says which endings reach this path: an operator's cancellation does not,
and neither do three of the six fences.

**BEHAVIOUR.** Each delivered object becomes one downstream artifact whose kind is the
**output port name** you delivered it through (`pipelines/external_finalize.py:2057`). A
downstream step configured to read `output` finds exactly what you published under
`output`.

**BEHAVIOUR.** The published location contains an identifier of the collection that won,
which you cannot predict. Never construct a published URI by hand; follow the URIs in the
execution's result (`pipelines/external_finalize.py:2640-2675`).

**BEHAVIOUR.** The run's metrics are `objects_published`, `bytes_published` and
`exit_code`, taken from what was actually published and from your marker's echo of the
exit code.
