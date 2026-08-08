# The conformance harness

A black-box test rig for this node. It builds the image from this repository's own
`Dockerfile`, runs it as a container the way the orchestrator's agent would, and judges
it entirely from the outside: the environment it was given, the HTTP traffic it made,
the objects it uploaded, **the order it uploaded them in**, and the exit code it died
with. It never imports `node.py`, never patches it, and never mounts anything over it.

```bash
pip install -r requirements-dev.txt
python -m pytest                 # CI mode: known gaps run as expected failures
python -m pytest --red-for-real  # the true result — this is how the baseline was measured
python -m pytest -m conforms_today
python -m pytest --collect-only -q --print-labels   # every test's group, basis and citation
```

Docker is required. If there is no daemon the suite skips itself with a message rather
than pretending, because a harness that fakes the container proves nothing.

## The one rule, and the rule about the rule

**A test here must be red against the node as it is today and green only once the node is
fixed.** That is what makes it evidence rather than decoration. `node.py` is not to be
touched by anything that adds tests here.

**And a test may not demand more than the thing it cites.** This harness is the oracle
for fixing the reference node and for the documentation written from that fix, so an
assertion that overreaches does not stay a local mistake: it becomes wrong code, and then
wrong documentation that teaches the wrong code to every node author afterwards. A review
of the first version found five defects that were preferences of ours rather than rules —
see the top of `CONFORMANCE-BASELINE.md` for what was withdrawn and why.

If a test you expected to fail passes, that is not a test to adjust — it is either a bug
in the harness or a mistake in the analysis, and which one it is matters.

## Every test declares its subject AND its authority

`tests/conftest.py` refuses to run any test that does not carry exactly one group and
exactly one basis.

| Group | Subject | Today |
|---|---|---|
| `expected_red_until_fixed` | `node.py` | fails |
| `conforms_today` | `node.py` | passes — a regression guard |
| `subject_is_platform` | the agent / collector / contract | no edit to `node.py` can move it |
| `harness_self_test` | this harness | proves the instrument before its readings are trusted |

| Basis | What the assertion rests on |
|---|---|
| `basis_contract("…")` | A rule in the platform's sources, **quoted in the marker**. Collection fails if the citation does not name one of the files in `markers.AUTHORITATIVE_SOURCES`. |
| `basis_reference_quality("…")` | What a reference implementation should demonstrate. The contract permits otherwise; a node that does the opposite is conformant. Never document one of these as a requirement. |
| `basis_our_policy("…")` | A compatibility or hygiene choice this repository makes, which the platform does not state — keeping the legacy credentials variable working, for instance. |

The expected-red group runs as **strict** xfail. CI is therefore green today, and turns
red the moment a fix lands while the marker is still on the test — which is the prompt to
move that test into `conforms_today`.

A caveat about `subject_is_platform`: most of those tests are **restatements** of a rule,
checked against this harness's own copy of it. Nobody runs this suite against the
orchestrator, so a change there cannot turn them red by itself. Their value is that the
rules the rest of the suite leans on are written down with a citation a human can check in
one step — which is exactly how the "the agent injects exactly nine variables" error in
this file was found. Two of them are not restatements and really do exercise the
mechanism, with real containers: the bind-mounted-file test and the 0700-permissions test.

## What is in here

| File | What it is |
|---|---|
| `contract.py` | The wire contract, **re-implemented from the specification**, not vendored from the orchestrator. A harness that shares code with the system it judges inherits that system's bugs and stops being able to see them. The price is drift, which is why `fixtures/` exists. |
| `fixtures/` | The orchestrator's three frozen golden version-1 documents, copied byte for byte. Validating them is the one *measurement* that `contract.py` still agrees with the real parser. |
| `fakes3.py` | The fake object store: presigned-style GETs, presigned-POST uploads with the whole signed form checked, one key prefix, arrival order recorded, credentials that expire. See its module docstring for where it is stricter than S3 and where it now matches it exactly. |
| `job.py` | One synthetic job: the credential envelope, `invocation.json`, the credentials directory, and the atomic `os.replace` swap that models a refresh. |
| `docker.py` | Container lifecycle through the `docker` CLI — the one interface that is unarguably outside the node. A container that has not exited reports **no** exit code, never docker's misleading `0`. |
| `platform_rules.py` | Rules that belong to the agent, written down so they are executable: the environment merge and its allowlist, exit-code classification, progress-line absorption. |
| `markers.py` | The four groups and the three bases. |

## Credentials: expiry, not revocation

Production credentials are presigned URLs with a deadline. The orchestrator re-signs a
fresh envelope before the old one expires and the agent replaces `creds.json` atomically
in the mounted directory — but **issuing a new envelope does not revoke the old one**.
Nothing can: a signature over a deadline cannot be taken back.

This endpoint models exactly that. A credential is refused when its own expiry has passed,
judged at the moment the request ARRIVES (as S3 authorizes a request when it receives it),
and never merely because a newer one exists. Three moments are modelled, and they are not
interchangeable:

* an envelope that **runs out** while the step is working — the file on disk has been
  refreshed, the copy in the step's memory has not;
* a credential killed **mid-request** (`Endpoint.expire`): the store takes the body and
  the refusal arrives afterwards, which is what an expiring upload policy looks like from
  inside the container and what a "check the expiry before you start" fix does not cover;
* an envelope that was **already dead** when the step picked it up — a stale file left
  behind by a half-finished migration.

An earlier version of this file revoked every previous credential the instant a new one
was minted. It made a correct, expiry-aware step look broken, and it would have forced
"re-read the credentials file before every single transfer" — a requirement the platform
does not make. `agent/creds.py` says the opposite in as many words: the agent refreshes
before expiry *"so the workload never has to handle an expired file"*.
