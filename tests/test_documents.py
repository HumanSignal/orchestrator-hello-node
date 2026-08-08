"""The documents a step reads and writes, and the ceilings on them.

Contract documents are control data, not payload, so every read AND every write is
bounded: the manifest and the marker at 8 MiB (both legitimately carry inventories, and
a wide batch can pin thousands of objects), ``result.json`` at 1 MiB, because it carries
only metrics and a summary and anything approaching a megabyte there is payload in the
wrong place. A producer that ignores the ceiling only finds out when somebody else fails
to read what it wrote.

Compatibility runs the other way: unknown fields are IGNORED, never rejected. A newer
orchestrator may add keys an older customer image has never heard of, so dropping a field
is a breaking change and adding one is not.
"""

from __future__ import annotations

import json

from conformance import contract, platform_rules
from conformance.job import InputSpec
from conformance.markers import conforms_today, expected_red_until_fixed

#: Comfortably over the 1 MiB ceiling on result.json, and well under the 8 MiB one on
#: the manifest that carries it — so the ONLY document in violation is the one the step
#: wrote itself.
BULKY_PARAM_BYTES = 3 * 1024 * 1024


@conforms_today
def test_params_reach_the_step_unchanged(make_job, sample_input):
    """``params`` are carried verbatim: the contract is not a filter.

    Setup:    parameters with nesting, non-ASCII text, a float, a bool and a null.
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
def test_unknown_additive_fields_are_ignored_not_rejected(make_job, sample_input):
    """A newer orchestrator adds keys; an older image must not care.

    Setup:    an envelope, a manifest and every input entry carrying fields that do not
              exist in this version of the contract.
    Action:   run.
    Validate: the step finishes normally.
    """
    job = make_job(
        inputs=[sample_input],
        envelope_extra={'issued_by': 'a newer orchestrator', 'refresh_hint_s': 300},
        manifest_extra={'scheduling_class': 'batch', 'tenant': {'id': 4}},
        input_extra={'content_type': 'text/csv', 'etag': 'W/"abc"'},
    )
    result = job.run()

    assert result.exit_code == 0, result.output
    assert job.marker()['status'] == 'succeeded'


@conforms_today
def test_the_result_document_has_the_shape_the_contract_declares(make_job, sample_input):
    """Setup: an ordinary run. Action: read ``result.json``. Validate: it parses as a
    contract result document — version 1, with ``metrics`` and ``summary`` objects."""
    job = make_job(inputs=[sample_input])
    result = job.run()
    assert result.exit_code == 0, result.output

    raw = job.endpoint.body_of(contract.RESULT_FILENAME)
    document = contract.validate_result(json.loads(raw.decode('utf-8')), raw_bytes=raw)
    assert document['metrics']['files'] >= 1


@expected_red_until_fixed
def test_the_result_document_stays_under_its_ceiling(make_job, sample_input):
    """A step must not write a document the other side is forbidden to read.

    Setup:    a job whose ``params`` are three megabytes — a large but entirely legal
              manifest, well inside the 8 MiB manifest ceiling.
    Action:   run.
    Validate: the run succeeds and ``result.json`` is at most 1 MiB.

    The step copies ``params`` into its summary without looking at their size, so a
    manifest the orchestrator was happy to write produces a result document the
    orchestrator will refuse to read. The failure surfaces at collection, on the far
    side of all the real work, as an unreadable contract document rather than as
    anything about the parameters that caused it.
    """
    job = make_job(inputs=[sample_input], params={'payload': 'x' * BULKY_PARAM_BYTES})
    result = job.run()
    assert result.exit_code == 0, result.output

    raw = job.endpoint.body_of(contract.RESULT_FILENAME)
    contract.validate_result(json.loads(raw.decode('utf-8')), raw_bytes=raw)


@expected_red_until_fixed
def test_the_step_reports_progress(make_job):
    """A run with no progress signal is a run nobody can tell apart from a stuck one.

    Setup:    four inputs, so there is something to be part-way through.
    Action:   run and read the container's output the way the agent's log buffer does.
    Validate: at least one well-formed ``@lspo:progress`` line was emitted, and the
              fractions never go backwards.

    Progress is opt-in and out of band: a step writes
    ``@lspo:progress {"fraction": 0.4, "phase": "copying"}`` and the agent consumes that
    line and reports it on the next heartbeat. The agent deliberately does NOT invent a
    fraction from elapsed time, because a made-up progress bar is a lie a human then
    plans around — so a step that never writes one has no progress at all, and a long
    batch looks identical to a hung one for its entire duration.
    """
    job = make_job(inputs=[InputSpec(relpath=f'{n}.csv', data=b'row\n' * 100) for n in range(4)])
    result = job.run()
    assert result.exit_code == 0, result.output

    _, samples = platform_rules.split_log(result.lines)
    assert samples, f'the step reported no progress at all; it said:\n{result.output}'
    fractions = [sample['fraction'] for sample in samples]
    assert fractions == sorted(fractions), f'progress went backwards: {fractions}'
