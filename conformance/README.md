# The conformance harness

A black-box test rig for this node. It builds the image from this repository's own
`Dockerfile`, runs it as a container the way the orchestrator's agent would, and judges
it entirely from the outside: the environment it was given, the HTTP traffic it made,
the objects it uploaded, **the order it uploaded them in**, and the exit code it died
with. It never imports `node.py`, never patches it, and never mounts anything over it.

```bash
pip install -r requirements-dev.txt
python -m pytest                 # CI mode: known defects run as expected failures
python -m pytest --red-for-real  # the true result — this is how the baseline was measured
python -m pytest -m conforms_today
```

Docker is required. If there is no daemon the suite skips itself with a message rather
than pretending, because a harness that fakes the container proves nothing.

## The one rule

**A test here must be red against the node as it is today and green only once the node is
fixed.** That is what makes it evidence rather than decoration. `node.py` is not to be
touched by anything that adds tests here.

If a test you expected to fail passes, that is not a test to adjust — it is either a bug
in the harness or a mistake in the analysis, and which one it is matters. Both times it
happened while this harness was being written, it was the analysis: see the SIGTERM entry
in `CONFORMANCE-BASELINE.md`.

## Every test declares its subject

A green run means nothing unless you know what each test was holding to account, so
`tests/conftest.py` refuses to run any test that does not carry exactly one of these:

| Marker | Subject | Today |
|---|---|---|
| `expected_red_until_fixed` | `node.py` | fails — a written-down defect |
| `conforms_today` | `node.py` | passes — a regression guard |
| `subject_is_platform` | the agent / collector / contract | no edit to `node.py` can move it |
| `harness_self_test` | this harness | proves the instrument before its readings are trusted |

The expected-red group runs as **strict** xfail. CI is therefore green today, and turns
red the moment a fix lands while the marker is still on the test — which is the prompt to
move that test into `conforms_today`.

## What is in here

| File | What it is |
|---|---|
| `contract.py` | The wire contract, **re-implemented from the specification**, not vendored from the orchestrator. A harness that shares code with the system it judges inherits that system's bugs and stops being able to see them. Any disagreement between this file and `external/contract.py` is itself a finding. |
| `fakes3.py` | The fake object store: presigned-style GETs, presigned-POST uploads, one key prefix, records arrival order, refuses on command. Deliberately **stricter than real S3** — see its module docstring for the three ways and why each matters. |
| `job.py` | One synthetic job: the credential envelope, `invocation.json`, the credentials directory, and the atomic `os.replace` swap that models rotation. |
| `docker.py` | Container lifecycle through the `docker` CLI — the one interface that is unarguably outside the node. |
| `platform_rules.py` | Rules that belong to the agent, written down so they are executable: the environment allowlist, exit-code classification, progress-line absorption. |
| `markers.py` | The four groups. |

## Rotation without a clock

Credentials expire on a wall clock in production, which would make "does the step reload
before every transfer?" either a fifteen-minute test or a clock-skew test. Here they
expire by **generation**: the store issues `gen-1`, and at a point in the traffic the test
chooses it atomically replaces `creds.json` with `gen-2` and stops accepting `gen-1`.
Four moments are modelled, and they are not interchangeable:

* after the last input is read — the first write happens under a dead credential;
* after the first upload — distinguishes "reloaded once" from "reloads every time";
* after the first input — a rotation between two reads;
* *inside* a request: the store accepts the body, rotates, and only then answers 403 —
  the mid-operation expiry, which a "check the expiry before you start" fix does not cover.
