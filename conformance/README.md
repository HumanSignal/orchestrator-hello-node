# The conformance harness

A black-box test rig for this node. It builds the image from this repository's own
`Dockerfile`, runs it as a container the way the orchestrator's agent would, and judges
it entirely from the outside: the environment it was given, the HTTP traffic it made,
the objects it uploaded, **the order it uploaded them in**, and the exit code it died
with. It never imports `node.py`, never patches it, and never mounts anything over it.

```bash
pip install -r requirements-dev.txt
python -m pytest                 # CI mode: an expected-red test would run as an expected failure
python -m pytest --red-for-real  # the true result — identical today, because nothing is expected-red
python -m pytest -m conforms_today
python -m pytest --collect-only -q --print-labels   # every test's group, basis and citation

# Check every quoted rule against the orchestrator's real sources (see below).
LSPO_ORCHESTRATOR_SRC=/path/to/orchestrator LSPO_ORCHESTRATOR_REF=origin/master python -m pytest
```

Docker is required. If there is no daemon the suite skips itself with a message rather
than pretending, because a harness that fakes the container proves nothing.

## The one rule, and the rule about the rule

**A test that claims a defect must be shown red against code that has the defect, and green
only once that code is fixed.** That is what makes it evidence rather than decoration.
`node.py` was repaired under exactly that discipline: the twenty tests that measured its
defects were red first and are green now, and `CONFORMANCE-BASELINE.md` keeps the
measurement of what each defect cost. Since the node passes everything today, a new claim
has to earn its red another way — build the offending behaviour into a throwaway copy of
`node.py` and show the assertion fails against that. It is how the two false passes
described below were proved closed. The real `node.py` is not to be touched by anything
that adds tests here.

**And a test may not demand more than the thing it cites.** This harness is the oracle
for fixing the reference node and for the documentation written from that fix, so an
assertion that overreaches does not stay a local mistake: it becomes wrong code, and then
wrong documentation that teaches the wrong code to every node author afterwards.

The bar, in one sentence: **a test may carry `basis_contract` only if the quoted rule,
read literally, makes the observed behaviour a violation.** Not "there is a rule about
this area". Not "a good node would". If the rule says *"a marker, if written, must …"*
then a test requiring a marker to EXIST is not resting on it. When in doubt, demote —
`basis_reference_quality` costs nothing and claims nothing false. Three rounds of review
have taken the contract-labelled count from 23 to 10 to 6; each fall was a correction, not
a regression. See the top of `CONFORMANCE-BASELINE.md`.

**And a test asserts the PROHIBITION, never the remedy.** Write "this run must not exhibit
the prohibited outcome" and let every other outcome pass. The moment a test adds "and it
must exit 0", or "and this optional document must exist", it has stopped describing the
contract and started describing one particular conforming implementation — so the next
person's **correct** fix goes red for the wrong reason, and the obvious response to a red
test that should be green is to weaken the code until it passes. Two questions catch it:

* *is there a conforming implementation that would fail this?* Refusing a job it cannot do
  safely, failing loudly instead of writing a document, checking every input before
  producing anything — all conformant, and all of them broke assertions here.
* *is the premise something a plausible fix could legitimately make false?* If so it is a
  condition (`pytest.skip`, saying which legal choice the step made), not a failure. If
  only a node that does nothing at all could make it false, it is a premise and stays an
  assertion — otherwise this reasoning empties the suite, since the contract requires no
  step to produce anything.

When the rule is about a difference — *"adding a field is not a breaking change"* — run
the job both ways and compare. Asserting that the second run succeeds smuggles in a
requirement nobody wrote.

**And the same care is owed in the other direction, where the mistake is a FALSE PASS.**
Asserting less than the prohibition is the more dangerous error, because the harness goes
green over exactly the behaviour it exists to forbid. Two shapes produced one each:

* **an oracle that inspects only part of what the rule bounds.** A ceiling on a document
  bounds every write of it, so reading back what the store *serves* — the most recent
  write — lets a step publish an oversized document and then overwrite it with a small one.
  Ask what the rule bounds, then look at all of it.
* **a "not applicable" branch that is not conditioned on what makes it inapplicable.** A
  missing marker is permitted after a failure or a cancellation and forbidden after a
  success, so skipping on *any* missing marker hands the next author an escape route:
  delete the document the test objects to, keep exiting 0, and the proof goes quiet. Every
  `pytest.skip` must name the condition that makes the rule silent, and check it.

The general form: write down the exact sentence, then ask whether an implementation could
satisfy the assertion while breaking the sentence. If it could, the assertion is not the
sentence yet.

If a test you expected to fail passes, that is not a test to adjust — it is either a bug
in the harness or a mistake in the analysis, and which one it is matters.

## Every test declares its subject AND its authority

`tests/conftest.py` refuses to run any test that does not carry exactly one group and
exactly one basis.

| Group | Subject | Today |
|---|---|---|
| `expected_red_until_fixed` | `node.py` | empty — the twenty defects it marked are fixed |
| `conforms_today` | `node.py` | passes — a regression guard |
| `subject_is_platform` | the agent / collector / contract | no edit to `node.py` can move it |
| `harness_self_test` | this harness | proves the instrument before its readings are trusted |

| Basis | What the assertion rests on |
|---|---|
| `basis_contract("…")` | A rule in the platform's sources, **quoted in the marker**. Collection fails if the citation does not name one of the files in `markers.AUTHORITATIVE_SOURCES`, or if it quotes nothing at least twelve characters long. |
| `basis_reference_quality("…")` | What a reference implementation should demonstrate. The contract permits otherwise; a node that does the opposite is conformant. Never document one of these as a requirement. |
| `basis_our_policy("…")` | A compatibility or hygiene choice this repository makes, which the platform does not state — keeping the legacy credentials variable working, for instance. |

The expected-red group runs as **strict** xfail, and no test carries it now: the twenty
that did were moved into `conforms_today` when the node was repaired, so CI mode and
`--red-for-real` cannot report different things until a new defect is recorded. Strict is
the load-bearing word, and it is why the group is kept rather than deleted — a test left
marked after its fix lands passes unexpectedly and turns the run red, which is the prompt
to move it across and correct `CONFORMANCE-BASELINE.md`. A defect that quietly went away
is a fact somebody has to be told.

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
| `fakes3.py` | The fake object store: presigned-style GETs, presigned-POST uploads with the whole signed form checked, one key prefix, arrival order recorded, credentials that expire, and an in-flight ledger so a test can wait for the store to finish (`settle()`) instead of reading it mid-request. See its module docstring for where it is stricter than S3 and where it now matches it exactly. |
| `job.py` | One synthetic job: the credential envelope, `invocation.json`, the credentials directory, and the atomic `os.replace` swap that models a refresh. |
| `docker.py` | Container lifecycle through the `docker` CLI — the one interface that is unarguably outside the node. A container that has not exited reports **no** exit code, never docker's misleading `0`. |
| `platform_rules.py` | Rules that belong to the agent, written down so they are executable: the environment merge and its allowlist, exit-code classification, progress-line absorption. |
| `markers.py` | The four groups and the three bases. |
| `citations.py` | Two checks on a `basis_contract` citation: that it names a source and quotes a rule (always), and that every quoted fragment is really in that source (when the orchestrator's sources are available). |

## Checking that a citation quotes something real

Naming a file proves nothing about a rule. `citations.py` enforces the structural half at
collection time — name an authoritative source, and quote at least twelve characters out
of it — and offers a verbatim half that needs the platform's sources:

```bash
LSPO_ORCHESTRATOR_SRC=/path/to/orchestrator LSPO_ORCHESTRATOR_REF=origin/master \
  python -m pytest tests/test_harness_self.py -k citation
```

Without `LSPO_ORCHESTRATOR_SRC` it skips and says so; this repository is standalone and
does not vendor the platform, and a check that passed because it had nothing to read would
be worse than none. Matching ignores line wrapping, indentation, Sphinx markup, comment
markers, the seams between adjacent Python string literals, and which quote character was
used. It reads the citations out of the test files with `ast`, not off the collected
tests, so the answer does not get weaker when you narrow the selection.

**Run it against the commit the node is deployed against.** It has now caught what it was
built for: `origin/master` moved mid-round, one cited rule had been reworded, and the check
named it within minutes. Three platform rules had drifted past this harness's own
re-implementation (control characters in a relpath, in a port name, and in a progress
phase) and all three are mirrored — see `CONFORMANCE-BASELINE.md`. Nothing else in this
repository notices a platform change, so a green run with `LSPO_ORCHESTRATOR_SRC` unset
proves nothing about drift.

**What it cannot do**, and it is the important half: it establishes that the sentence
exists, not that it SUPPORTS the assertion. Every over-claim corrected in this harness so
far cited a real file and quoted a real sentence — including one withdrawn this round,
whose sentence was about credentials expiring and whose claim was about when a store
authorizes a request. That step stays with a reviewer; what the check buys is that the
sentence is out in the open where a reader can weigh it.

## Credentials: expiry, not revocation

Production credentials are presigned URLs with a deadline. The orchestrator re-signs a
fresh envelope before the old one expires and the agent replaces `creds.json` atomically
in the mounted directory — but **issuing a new envelope does not revoke the old one**.
Nothing can: a signature over a deadline cannot be taken back.

This endpoint models exactly that. A credential is refused when its own expiry has passed,
judged **once, when the request's HEADERS arrive — before its body has been read**, as S3
authorizes the request it receives rather than re-checking a large upload on the way out.
Never merely because a newer credential exists, and never after the request has already
been accepted. That last detail is not a nicety: this store used to stamp a POST's arrival
*after* parsing its whole body, so a credential expiring while the bytes were still on the
wire refused an upload that began inside its lifetime — revocation in expiry's clothes,
surviving in the one place the self-test could not look, because a hook runs after the body
too. It is proven now by sending a body in two halves across the expiry instant. **When a
store authorizes is a fact about S3, not a platform rule** — no orchestrator source states
it — so that self-test is labelled `basis_our_policy`, not `basis_contract`.

Two moments are modelled, and they are not interchangeable:

* an envelope that **runs out** while the step is working — the file on disk has been
  refreshed, the copy in the step's memory has not;
* an envelope that was **already dead** when the step picked it up — a stale file left
  behind by a half-finished migration.

There used to be a third: a credential killed mid-request, so the store took the body and
the refusal arrived afterwards. That is **revocation wearing an expiry's clothes**. Real
S3 cannot produce it, no platform source asks a step to survive it, and the test built on
it demanded that a step retry a transfer the store had already accepted. Both it and the
machinery that expressed it are gone, and a self-test now proves the opposite: a
credential that expires during an upload does not retroactively refuse it.

The round before that, this file revoked every previous credential the instant a new one
was minted. It made a correct, expiry-aware step look broken, and it would have forced
"re-read the credentials file before every single transfer" — a requirement the platform
does not make. `agent/creds.py` says the opposite in as many words: the agent refreshes
before expiry *"so the workload never has to handle an expired file"*.

**One consequence for anyone writing a test here.** If a test needs a fresh `creds.json`
published while a request is in flight, register `refresh_when` on `on_request` BEFORE the
hook that holds the response open. Publishing it after the response has been written is a
race the step can lose through no fault of its own, and a test that intermittently fails a
correct implementation is worse than no test.
