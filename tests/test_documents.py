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

import pytest

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

    Setup:    the SAME job twice — once with a plain manifest, once with a manifest
              carrying two fields that do not exist in this version of the contract.
    Action:   run both.
    Validate: the extra fields changed nothing. Whatever the node does with this job, it
              does the same thing with the fields present.

    **Compared rather than asserted, because the rule is about a difference.** "Adding a
    field is not a breaking change" is a statement about two runs, and a test that simply
    required the second to exit 0 would be requiring this node to succeed — a demand the
    contract never makes, and one that would fail an implementation which legitimately
    refuses this job for some entirely unrelated reason.

    The manifest is one of the documents that rule is about — it is a contract model, and
    "adding one is not [a breaking change]" is a promise made to the reader of it. The
    credential envelope is NOT one of those models, which is why it has a test of its own
    below rather than sharing this citation.
    """
    plain = make_job(inputs=[sample_input]).run()
    if plain.exit_code != 0:
        pytest.skip(
            f'this node does not complete an ordinary job (exit {plain.exit_code}), so comparing the two '
            f'runs would be comparing two failures and would pass whatever the extra fields did'
        )
    extended = make_job(
        inputs=[sample_input],
        manifest_extra={'scheduling_class': 'batch', 'tenant': {'id': 4}},
    ).run()

    assert extended.exit_code == plain.exit_code, (
        f'the same job exited {plain.exit_code} with a plain manifest and {extended.exit_code} with two '
        f'unknown fields added to it, so adding a field to the manifest IS a breaking change for this '
        f'node:\n{extended.output}'
    )


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
    """Setup:    the same job twice — once ordinary, once with an envelope, and every
              input entry inside it, carrying invented fields.
    Action:   run both.
    Validate: the extra fields changed nothing.

    Compared rather than asserted, for the reason given on the manifest test above: the
    expectation is that adding a key makes no difference, and "the run succeeds" is a
    different and stronger claim than the one being made.
    """
    plain = make_job(inputs=[sample_input]).run()
    if plain.exit_code != 0:
        pytest.skip(
            f'this node does not complete an ordinary job (exit {plain.exit_code}); two failures compared '
            f'against each other would agree no matter what the extra keys did'
        )
    extended = make_job(
        inputs=[sample_input],
        envelope_extra={'issued_by': 'a newer orchestrator', 'refresh_hint_s': 300},
        input_extra={'content_type': 'text/csv', 'etag': 'W/"abc"'},
    ).run()

    assert extended.exit_code == plain.exit_code, (
        f'the same job exited {plain.exit_code} with an ordinary envelope and {extended.exit_code} with '
        f'unknown keys added to it and to its input entries:\n{extended.output}'
    )


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
    and nothing about the contents. A step that writes no result document at all breaks
    no rule — the contract describes the document, it does not require one — so that case
    skips rather than failing, and the exit code is not asserted: the shape rule binds a
    document whenever one is written, whatever the run went on to do.
    """
    job = make_job(inputs=[sample_input])
    job.run()

    if contract.RESULT_FILENAME not in job.endpoint.keys_in_order():
        pytest.skip('the step wrote no result.json, which the contract permits — no document, no shape')
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
    Validate: **no oversized ``result.json`` reaches the store.** That is the whole
              assertion, and everything else about the run is deliberately left alone.

    **Two conforming answers, neither of them required here.** Failing loudly rather than
    writing the document is what the rule cited above describes wanting — *"bounding the
    write makes a producer fail loudly at the point of the mistake"* — so the exit code is
    not asserted. And ``result.json`` is optional: the contract describes the document, it
    does not oblige anybody to produce one, so a run that writes none skips rather than
    passes or fails. An earlier version of this test required exit 0 and then required the
    document to exist, which would have turned either correct fix red.

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
    job.run()

    if contract.RESULT_FILENAME not in job.endpoint.keys_in_order():
        pytest.skip(
            'the step wrote no result.json for this job. Nothing requires one, and a ceiling on a '
            'document constrains the document that gets written — there is nothing here to be over it'
        )
    raw = job.endpoint.body_of(contract.RESULT_FILENAME)
    assert len(raw) <= contract.MAX_RESULT_BYTES, (
        f'the step wrote a {len(raw)}-byte result.json, over the {contract.MAX_RESULT_BYTES}-byte '
        f'ceiling — the contract\'s own reader is forbidden to read it, and the producer is the side '
        f'that was supposed to find that out'
    )


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
