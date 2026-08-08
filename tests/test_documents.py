"""The documents a step reads and writes, the ceilings on them, and progress.

Contract documents are control data, not payload, so every read AND every write is
bounded: the manifest and the marker at 8 MiB (both legitimately carry inventories, and
a wide batch can pin thousands of objects), ``result.json`` at 1 MiB, because it carries
only metrics and a summary. A producer that ignores the ceiling only finds out when
somebody else fails to read what it wrote.

Compatibility runs the other way: unknown fields are IGNORED, never rejected. A newer
orchestrator may add keys an older customer image has never heard of, so dropping a field
is a breaking change and adding one is not. **That rule is about the contract's own
models** — the manifest, the marker, the result document. The credential envelope is a
plain dict with no model behind it, so the same expectation applied to IT is ours rather
than the contract's, and the two are tested separately.
"""

from __future__ import annotations

import json

from conformance import contract, platform_rules
from conformance.job import InputSpec
from conformance.markers import (
    conforms_today,
    expected_red_until_fixed,
    our_policy,
    reference_quality,
    traces_to,
)

#: Comfortably over the 1 MiB ceiling on result.json, and well under the 8 MiB one on
#: the manifest that carries it — so the ONLY document in violation is the one the step
#: wrote itself.
BULKY_PARAM_BYTES = 3 * 1024 * 1024


@conforms_today
@reference_quality(
    'external/contract.py carries params "verbatim; it is not a filter and performs no redaction", but '
    'nothing obliges a step to echo them anywhere. This node puts them in its summary, and the value of '
    'pinning that is the round trip: a step that re-encoded a float, dropped a null or mangled non-ASCII '
    'on the way through would be teaching that to everyone who copies this file.'
)
def test_params_reach_the_step_unchanged(make_job, sample_input):
    """Setup:    parameters with nesting, non-ASCII text, a float, a bool and a null.
    Action:   run.
    Validate: what comes back in ``result.json`` is equal to what went in.
    """
    params = {
        'threshold': 0.75,
        'enabled': True,
        'absent': None,
        'label': 'проверка — ünïcode',
        'nested': {'list': [1, 2, {'deep': 'yes'}]},
    }
    job = make_job(inputs=[sample_input], params=params)
    result = job.run()
    assert result.exit_code == 0, result.output

    echoed = json.loads(job.endpoint.body_of(contract.RESULT_FILENAME).decode('utf-8'))
    assert echoed['summary']['params'] == params


@conforms_today
@traces_to(
    'external/contract.py: "Every model tolerates unknown fields (extra=\'ignore\'): a newer '
    'orchestrator may add keys that an older customer image has never heard of, and vice versa. Dropping '
    'a field is therefore a breaking change; adding one is not."'
)
def test_unknown_fields_in_the_manifest_are_ignored_not_rejected(make_job, sample_input):
    """A newer orchestrator adds keys to ``invocation.json``; an older image must not care.

    Setup:    a manifest carrying fields that do not exist in this version of the contract.
    Action:   run.
    Validate: the step finishes normally.

    The manifest is one of the documents that rule is about — it is a contract model, and
    "adding one is not [a breaking change]" is a promise made to the reader of it. The
    credential envelope is NOT one of those models, which is why it has a test of its own
    below rather than sharing this citation.
    """
    job = make_job(
        inputs=[sample_input],
        manifest_extra={'scheduling_class': 'batch', 'tenant': {'id': 4}},
    )
    result = job.run()

    assert result.exit_code == 0, result.output
    assert job.marker()['status'] == 'succeeded'


@conforms_today
@our_policy(
    'The credential envelope is not a contract model. external/contract.py\'s extra=\'ignore\' rule '
    'governs the pydantic documents — the manifest, the marker, the result — and the envelope is a plain '
    'dict built in runners/credentials.py, with no model and no stated compatibility rule for its own '
    'unknown keys or for the entries in its inputs list. The nearest thing to a rule is that the '
    'envelope carries its own ENVELOPE_SCHEMA_VERSION which "a shape change that an old agent could not '
    'understand bumps", which implies additive keys do not bump it — an implication, not a sentence. So '
    'tolerating them is our choice, and worth pinning because it is exactly how a field gets added: to '
    'the envelope first, where the oldest customer images will see it.'
)
def test_unknown_fields_in_the_credential_envelope_are_ignored_not_rejected(make_job, sample_input):
    """Setup:    an envelope, and every input entry inside it, carrying invented fields.
    Action:   run.
    Validate: the step finishes normally.
    """
    job = make_job(
        inputs=[sample_input],
        envelope_extra={'issued_by': 'a newer orchestrator', 'refresh_hint_s': 300},
        input_extra={'content_type': 'text/csv', 'etag': 'W/"abc"'},
    )
    result = job.run()

    assert result.exit_code == 0, result.output
    assert job.marker()['status'] == 'succeeded'


@conforms_today
@traces_to(
    'external/contract.py ResultDoc: schema_version, "metrics for numbers the pipeline may chart, '
    'summary for anything a human reads", both dicts — and external/versioning.py parses it through the '
    'same version gate as everything else.'
)
def test_the_result_document_has_the_shape_the_contract_declares(make_job, sample_input):
    """Setup: an ordinary run. Action: read ``result.json``. Validate: it parses as a
    contract result document — version 1, with ``metrics`` and ``summary`` objects.

    What is in those two objects is entirely the step's business; this asserts the shape
    and nothing about the contents.
    """
    job = make_job(inputs=[sample_input])
    result = job.run()
    assert result.exit_code == 0, result.output

    raw = job.endpoint.body_of(contract.RESULT_FILENAME)
    document = contract.validate_result(json.loads(raw.decode('utf-8')), raw_bytes=raw)
    assert isinstance(document['metrics'], dict) and isinstance(document['summary'], dict)


@expected_red_until_fixed
@traces_to(
    'external/contract.py: MAX_RESULT_BYTES = 1024 * 1024, under the heading "Contract documents are '
    'control data, not payload, so every read AND every write is bounded" — "The result document carries '
    'only metrics and a summary; anything approaching a megabyte there is payload in the wrong place." '
    'external/io.py says the same about the direction that matters here: "bounding the write makes a '
    'producer fail loudly at the point of the mistake, instead of publishing a document that only turns '
    'out to be unreadable later, in somebody else\'s process."'
)
def test_the_result_document_stays_under_its_ceiling(make_job, sample_input):
    """A step must not write a document the other side is forbidden to read.

    Setup:    a job whose ``params`` are three megabytes — a large but entirely legal
              manifest, well inside the 8 MiB manifest ceiling.
    Action:   run.
    Validate: the run succeeds and ``result.json`` is at most 1 MiB.

    The step copies ``params`` into its summary without looking at their size, so a
    manifest the orchestrator was happy to write produces a result document the contract's
    own reader refuses. Worth knowing what this does and does not cost today: nothing in
    the current collection path calls ``read_result``, so the oversized document is
    written and never read. It is the sanctioned reader that would refuse it, and the
    contract that says the ceiling bounds writes as well as reads — so this is a document
    the step is not allowed to produce, and the day anything reads it is the day it
    becomes an outage on the far side of all the real work.
    """
    job = make_job(inputs=[sample_input], params={'payload': 'x' * BULKY_PARAM_BYTES})
    result = job.run()
    assert result.exit_code == 0, result.output

    raw = job.endpoint.body_of(contract.RESULT_FILENAME)
    contract.validate_result(json.loads(raw.decode('utf-8')), raw_bytes=raw)


@expected_red_until_fixed
@reference_quality(
    'Progress is OPT-IN and this harness no longer says otherwise. agent/logbuf.py: "A workload that '
    'wants the Runs UI to show a progress bar writes a line @lspo:progress {…}. … A step that never '
    'writes one simply has no progress — the agent does not invent a fraction from elapsed time." A node '
    'that emits none is fully conformant. It is asserted here because the reference node is where an '
    'author learns the protocol exists at all: nothing in the contract documents will teach it to '
    'somebody who only ever reads the example, and a long batch with no progress is indistinguishable '
    'from a hung one for its whole duration.'
)
def test_the_reference_node_demonstrates_the_progress_protocol(make_job):
    """A run with no progress signal is a run nobody can tell apart from a stuck one.

    Setup:    four inputs, so there is something to be part-way through.
    Action:   run and read the container's output the way the agent's log buffer does.
    Validate: at least TWO distinct fractions were emitted and they never go backwards.

    Two, not one: a single constant line at startup satisfies "did it emit progress?"
    while telling a watcher nothing, and this test exists to make a stuck run
    distinguishable from a working one.
    """
    job = make_job(inputs=[InputSpec(relpath=f'{n}.csv', data=b'row\n' * 100) for n in range(4)])
    result = job.run()
    assert result.exit_code == 0, result.output

    _, samples = platform_rules.split_log(result.lines)
    fractions = [sample['fraction'] for sample in samples]
    assert len(set(fractions)) >= 2, (
        f'the step reported {len(fractions)} progress sample(s) with {len(set(fractions))} distinct '
        f'fraction(s), so nothing about it moves; it said:\n{result.output}'
    )
    assert fractions == sorted(fractions), f'progress went backwards: {fractions}'
