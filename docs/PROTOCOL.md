# PROTOCOL: the contract, in the order one attempt happens

This is the normative document. It follows the life of a single container attempt:
bootstrap, inputs, work, outputs, the completion marker, and how the attempt ends.

Every statement is labelled **RULE** (the platform refuses or fails the run),
**BEHAVIOUR** (what the platform does, which you must plan for) or **RECOMMENDATION**
(what a good node does; the platform permits otherwise). Unlabelled text is background.

Values are literal and were verified against the orchestrator at commit
`d8a78355ae7d78096f050b3268260f65f4b29692`. Source paths appear after a value as
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

Four documents live in the staging prefix. Their names are fixed and are part of the
contract; you locate them by name, never by configuration
(`external/contract.py:55-58`).

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

Field by field (`runners/credentials.py:386-397` for the first,
`runners/credentials.py:507-525` for the second, constants at `:63-71`):

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
`external/contract.py:302-327`.)

**RECOMMENDATION.** Validate the envelope before trusting it: the `schema_version` you
implement, a `scheme` you support, a `staging.mode` you support, and the presence of the
fields you are about to use. Fail with a clear sentence rather than a `KeyError` five
frames deep.

**RECOMMENDATION.** Ignore fields you do not recognise rather than rejecting the
document. The orchestrator's own parsers are configured to ignore unknown fields
precisely so that adding a field is not a breaking change
(`external/contract.py:271-274`). Strict about the fields you know, tolerant about the
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
(`external/contract.py:75`, `external/io.py:148-159`). So it is bounded, and you may rely
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

Source: `external/contract.py:372-457`. "Required" means the document is invalid without
it, by the same parser the orchestrator uses.

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
| `timeout_seconds` | integer >= 1 | **yes** | a **requested budget**, not a deadline |
| `idempotency_key` | non-blank string | **yes** | same value as `LSPO_IDEMPOTENCY_KEY` |
| `attempt` | integer >= 1 | **yes** | |
| `generation` | integer >= 1 | **yes** | fencing token |

An input port (`external/contract.py:302-366`):

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | **yes** | the port name your code refers to. Today this is always `input` |
| `payload_kind` | string | **yes** | logical content type. Taken from the **first** upstream artifact's own kind, or the literal string `file` when that artifact carries no kind at all. It is not the node's `input_payload_kind` setting, which only selects which artifact gets pinned |
| `layout` | `"file"` or `"prefix"` | **yes** | today always `"file"` |
| `cardinality` | `"one"`, `"at_least_one"` or `"many"` | **yes** | today always `"many"`, and it is enforced by the parser |
| `objects` | array | no, defaults to `[]` | |
| `prefix_digest` | 64 hex chars, or `null` | required when `layout` is `"prefix"` | |

An input object (`external/contract.py:280-299`):

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

**BEHAVIOUR.** `timeout_seconds` is what the node's configuration requested. The absolute
deadline is stamped when a runner **claims** the job, not when the job description was
written, because the wait for capacity is unbounded and is not the step's to spend. Your
container cannot observe that deadline. Note also that pulling your image happens after
the claim, so a slow pull is spent out of your budget.

**RECOMMENDATION.** Measure your own elapsed time from process start and aim to finish,
including uploads and the marker, comfortably inside `timeout_seconds`.

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
the uniqueness check exists only for folder ports (`external/contract.py:343-348`). Two
upstream steps that both produce `rows.csv` therefore collide.

**RULE.** Your output relpaths must be unique within one marker
(`external/contract.py:535-544`).

**RECOMMENDATION.** Derive your output names rather than echoing input names: an index, a
hash prefix, the port name, anything that cannot collide. A node that writes
`outputs/<input name>` fails the moment two inputs share a basename, and it fails at the
end of the run, after all the work.

**RECOMMENDATION.** Stream inputs to disk or process them incrementally. See section 3.5
for why holding one in memory is a real risk rather than a style preference.

---

## 3. Doing the work

### 3.1 Your configuration

**BEHAVIOUR.** `params` in the job description is the only part of the node's
configuration that ever reaches your code. It is copied verbatim from what the pipeline
author typed, with no filtering and no redaction
(`external/contract.py:390-392`, `pipelines/config_schemas.py`). Everything else about the
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
execution when the run ends.

**BEHAVIOUR, and each of these loses data:**

* A line longer than **64 KiB** is flushed with a visible truncation marker and **the rest
  of that line is discarded** (`agent/executors/docker_exec.py:95-99`, `:328-335`). One
  giant JSON blob on a single line loses its tail.
* The agent holds at most **2000 lines** per job between heartbeats; when it overflows the
  **oldest** are dropped and a warning line records how many
  (`agent/logbuf.py:36-110`, capacity from `LSPO_AGENT_LOG_BUFFER_LINES`).
* Heartbeats are every **20 seconds** and carry at most **100 lines** each. If you produce
  more than five lines per second on average, you are losing the excess.
* Each line is cut to **4096 characters** on the way through the agent, and the server
  **refuses** rather than trims a batch that breaches its own ceilings: at most 100 lines,
  4096 bytes per line, 128 KiB per batch, 256 KiB per request body
  (`runners/serializers.py:32-43`, `runners/auth.py:71`). A refused heartbeat costs the
  lease renewal it was carrying, not just the log lines.
* The live view keeps only the last **1000** entries.

**RECOMMENDATION.** Say the important things once, at the end, in few lines. A step that
prints one line per record will lose its beginning and will not notice.

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
and reported as progress on the next heartbeat (`agent/logbuf.py:32`, `:63-81`):

```
@lspo:progress {"fraction": 0.4, "phase": "encoding"}
```

* The prefix is `@lspo:progress ` **including the trailing space**.
* The rest of the line must be a JSON object with a `fraction` that converts to a float
  between `0.0` and `1.0` inclusive.
* `phase` is optional, coerced to a string and cut to 64 characters.
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
(`external/contract.py:440-457`).

**RULE.** A relpath must be canonical (`external/contract.py:217-250`):

* non-empty, and never starting with `/`;
* no backslashes;
* no carriage return or line feed;
* no `.` or `..` path components;
* **no empty components**, so `a//b` and a trailing `/` are both refused.

The last one surprises people. Two spellings of one path would be two inventory entries
for one file, or an alias that dodges the "is this relpath known?" check.

**RULE.** A relpath is checked twice: once when your marker is parsed, and again when
collection resolves it against the staging prefix
(`pipelines/external_finalize.py:1247-1279`). Both refuse traversal.

**BEHAVIOUR.** A relpath containing a `%` or a space behaves differently on the two
storage backends. Object-store keys are joined raw; local file URIs are percent-quoted per
segment (`pipelines/external_finalize.py:1226-1244`). This was a real bug: a file named
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
deadline (`runners/credentials.py:216-219`). The default runtime budget for an external
step is **3600 seconds** (`LSPO_EXTERNAL_DEFAULT_TIMEOUT_S`, `lspo/settings/base.py:562`).

Read those two numbers together: **a node that reads its credentials once cannot upload
its outputs, or its own completion marker, after about fifteen minutes.** This is the most
common way a working node fails on its first long job.

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
(`pipelines/external_finalize.py:1910-1916`).

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
writes its own record of the run to a **sibling** prefix under a different filename, so it
cannot collide with yours (`pipelines/external_finalize.py:2093-2104`). Nothing requires
you to write one.

---

## 5. The completion marker

`__lspo_complete.json`, written into the **root** of your staging prefix. It is the
terminal receipt for the attempt.

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

Field by field (`external/contract.py:484-533`):

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
| `produced_ports` | object mapping port name to array of relpaths | no, defaults to `{}` | |
| `error` | string or `null` | **no** | quoted into the failure reason, cut at 500 characters |

**RULE.** On a run the platform classifies as **succeeded**, which normally means your
process exited 0 and nothing cancelled or timed it out, a marker is **required**. A missing
marker after a reported success fails the execution: there is no inventory, so there is
nothing to publish and nothing to verify (`pipelines/external_finalize.py:946-950`).

**RULE.** On such a run the marker's own `status` must be `"succeeded"`. A process that
exits 0 while its marker says `failed` fails the execution
(`pipelines/external_finalize.py:951-955`).

**RULE.** `execution_id`, `attempt` and `generation` must equal the launch the platform
believes it is collecting (`pipelines/external_finalize.py:956-962`). Copy them from the
job description, not from memory of an earlier attempt. A mismatch is refused with "a
receipt from a superseded or unrelated run must never be collected as this one".

**RULE.** Every relpath in `produced_ports` must appear in `objects`
(`external/contract.py:546-571`). The inventory is what carries the hash and the size.

**RULE.** Within one port a relpath may appear only once. The **same relpath in two
different ports is legal** and is sometimes what you want.

**RULE.** Relpaths in `objects` are globally unique across the whole inventory
(`external/contract.py:535-544`). One file, one entry.

**RULE.** The marker document must be at most **8 MiB**; the reader refuses a larger one
(`external/contract.py:76`, `external/io.py:162-172`). For a very wide batch, that bounds
how many objects one attempt can inventory.

**RULE.** Every `sha256` is exactly 64 lowercase hexadecimal characters, with no `sha256:`
prefix, no uppercase and no trailing newline (`external/contract.py:128`, matched with
`fullmatch` because in Python a `$` anchor also matches before a trailing newline).

**RULE.** Every integer is a real JSON integer. `"1"`, `true` and `1.0` are all rejected,
and every id and counter must be at least 1 (`external/contract.py:262-268`).

**RULE, enforced by its consequences.** Write the marker **last**, after every object it
names is durably readable. No component can prove after the fact in what order you wrote
things; what collection can do, and does, is copy every object the marker names and then
re-read it and hold it to the hash and size you promised
(`pipelines/external_finalize.py:1160-1186`). A marker written before its objects finishes
fails there, reported as a size or hash mismatch. Write it last and that class of failure
cannot happen.

**BEHAVIOUR.** An object you inventory but claim under no port is copied and verified, and
then **not offered downstream** on a successful run. It is not lost, but the execution's
artifact list is built from `produced_ports` alone
(`pipelines/external_finalize.py:1863-1885`), so an unclaimed object does not appear
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

**BEHAVIOUR, and a quiet way to lose everything.** A failed or cancelled marker is held to
the same identity check and the same document rules. "Best effort" applies to which objects
survive, not to whether the document is valid. But on this path an unreadable, invalid or
mismatched marker is **ignored silently** rather than reported
(`pipelines/external_finalize.py:1352-1385`): the run fails with a generic reason, your
`error` text never appears, and nothing you produced is salvaged. So a malformed failure
marker costs you exactly the information it existed to carry, and says nothing about
itself.

---

## 6. Exit codes

**BEHAVIOUR.** The classes (`external/contract.py:85-119`):

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
your process returned (`pipelines/external_finalize.py:1503-1522`), and an operator
retries by hand from the run view. A future automatic retry path may honour exit 10
without any change to your node.

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

**BEHAVIOUR.** When a run is cancelled, the agent asks docker to stop your container:
SIGTERM, then SIGKILL after **30 seconds** (`agent/executors/docker_exec.py:405-415`, the
default timeout, which the caller does not override).

**BEHAVIOUR, and this one is measured rather than assumed.** Your program almost certainly
runs as **PID 1** inside its container: the agent overrides neither the entrypoint nor the
command and does not ask docker for an init helper
(`agent/executors/docker_exec.py:248-274` contains no `entrypoint`, `command` or `init`
key), so an exec-form `ENTRYPOINT` makes your process number 1. This is where an operating
system property bites: Linux gives process 1 no default signal dispositions, so SIGTERM is
discarded unless you installed a handler for it. That is a kernel rule rather than
something the platform does, so it is stated here as a fact about Linux and not as a
citation of our code. A node that installs no handler does not die on cancellation. It
**ignores the request entirely**, finishes the whole batch, and writes a `succeeded`
marker after a human pressed stop, unless the work outlasts the 30 second grace, at which
point it is killed outright. This was measured by adding a one-line handler to the example
image, after which it exited at once.

**BEHAVIOUR.** The platform's classification is authoritative and it asks "was
cancellation requested?" **before** it looks at your exit code
(`agent/runner.py:2326-2340`). So a cancelled run stays cancelled whatever you exit with,
and exit code 20 is **not required** for that. The order of questions is: was this agent
fenced, is the agent shutting down, did the runtime deadline pass, was cancellation
requested or was the exit code 20, was the exit code 0, did the container vanish, and only
then the exit code's own class.

**BEHAVIOUR.** A container stopped because its **runtime deadline** passed is classified
`failed`, not `cancelled`, and that check comes first (`agent/runner.py:2332-2333`).

**RECOMMENDATION.** Handle cancellation, in this shape:

1. Install a SIGTERM handler that sets a flag. Do not do the work in the handler.
2. Have your normal control flow check the flag between units of work, and stop.
3. Keep the inventory of what you already uploaded (see the next point).
4. Re-read your credentials, write a `cancelled` marker **last**, and exit 20.

**RECOMMENDATION.** Reserve part of the 30 second grace for step 4, and make sure your
network calls cannot swallow it. A cooperative flag cannot be checked while you are
blocked in a socket read: a 120 second read timeout against a 30 second grace means
cancellation during a transfer produces neither a marker nor any salvage. Use timeouts and
chunk sizes well under the grace period.

**RECOMMENDATION, and this one decides whether partial work survives at all.** Accumulate
your object inventory somewhere the failure and cancellation paths can still see it, not
in a local variable of the function that does the work. Salvage publishes **only what the
marker inventories** (`pipelines/external_finalize.py:1417-1437`). A node that uploads
three objects, fails on the fourth, and then writes a marker with an empty inventory has
left those three objects in a staging area that expires, and nothing will ever collect
them.

---

## 8. What happens after you exit

You do not need to implement any of this, but knowing it explains most failure messages.

**BEHAVIOUR.** The agent reports your exit code and its own classification. On a success
the orchestrator reads your marker, checks its identity, and then for every object in
`objects`:

1. copies it from staging into a **published** area your credentials cannot reach;
2. re-reads it **there** and holds it to the size and hash your marker promised
   (`pipelines/external_finalize.py:966-1015`, `:1160-1186`).

Copy first, verify second, because your upload policy is still live and a hash taken in
staging is a statement about the past.

**BEHAVIOUR, on a successful run.** Any single object that fails verification causes
**everything this collection published to be deleted** and the execution to fail
(`pipelines/external_finalize.py:1189-1211`). A partial delivery is worse than a failed
one, because nothing downstream can tell which it got.

**BEHAVIOUR, on a failed or cancelled run.** The same copy and verify runs per object, and
whatever verifies is kept while the rest is dropped with a log line
(`pipelines/external_finalize.py:1388-1446`). Salvaged objects are attached to the
execution as diagnostics: they carry no output port and are not offered to any downstream
step.

**BEHAVIOUR.** Each delivered object becomes one downstream artifact whose kind is the
**output port name** you delivered it through (`pipelines/external_finalize.py:1877`). A
downstream step configured to read `output` finds exactly what you published under
`output`.

**BEHAVIOUR.** The published location contains an identifier of the collection that won,
which you cannot predict. Never construct a published URI by hand; follow the URIs in the
execution's result (`pipelines/external_finalize.py:2055-2090`).

**BEHAVIOUR.** The run's metrics are `objects_published`, `bytes_published` and
`exit_code`, taken from what was actually published and from your marker's echo of the
exit code.
