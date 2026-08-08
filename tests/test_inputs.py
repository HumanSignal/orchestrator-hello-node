"""What the step reads, and what it does with it.

Every input arrives pinned: the orchestrator hashed the object when it built the job, and
that hash is its statement about what this step is supposed to be reading.

**Most of this file is reference quality, not conformance, and it says so.** The contract
does not require a step to verify its inputs, to copy them anywhere, or to produce any
particular output at all — it describes the documents, not the work. What makes these
tests worth running is that this repository is the file customers copy: the behaviour
asserted here is the behaviour the example is supposed to teach, and the orchestrator's
own test for this node asserts the same things. Two tests here ARE conformance, and both
are about documents rather than about copying: the marker must be one the collector can
parse, and every relpath it inventories must name an object that really exists under it.
"""

from __future__ import annotations

import pytest

from conformance import contract, docker
from conformance.job import InputSpec
from conformance.markers import conforms_today, expected_red_until_fixed, reference_quality, traces_to

#: One eighth of a gigabyte of input against a 64 MiB container. Big enough that no
#: amount of interpreter overhead explains the difference, small enough to stay quick.
OVERSIZED_INPUT_BYTES = 128 * 1024 * 1024
TIGHT_MEMORY = '64m'

_PIN_CHECKING = (
    'Nothing in external/contract.py obliges a step to verify what it read — the pins are there so that '
    'it CAN. This is asserted because it is what the example is for: the orchestrator\'s own '
    'tests/test_hello_node_example.py holds this same file to it '
    '("test_an_input_that_does_not_match_its_pin_fails_the_step", "test_an_input_with_no_pin_at_all_is_'
    'refused"), and because a step that skips the check can process the wrong version of its input and '
    'still produce output that passes every check on the way back — those are about what it WROTE.'
)

_COPYING = (
    'The contract permits arbitrary output semantics: a step may produce anything, or nothing. That the '
    'reference node copies each input through unchanged is ITS behaviour, and the reason to pin it is '
    'that customers copy this file — a passthrough that quietly mangled a name or dropped a byte would '
    'be taught to everyone who started from it.'
)


@conforms_today
@reference_quality(_COPYING)
def test_a_job_with_no_inputs_still_completes(make_job):
    """A step with nothing to read still has something to say.

    Setup:    a job with an empty input list.
    Action:   run.
    Validate: ``result.json`` and the marker are written, in that order, and the marker
              says the run succeeded.

    The ORDER is contract (the marker is written last); the fact that a step with no
    inputs writes a ``result.json`` at all is this node's own choice.
    """
    job = make_job(inputs=[])
    result = job.run()

    assert result.exit_code == 0, result.output
    assert job.endpoint.keys_in_order() == [contract.RESULT_FILENAME, contract.MARKER_FILENAME]
    assert job.marker()['status'] == 'succeeded'


@conforms_today
@reference_quality(_COPYING)
def test_several_inputs_are_all_copied(make_job):
    """Setup:    four inputs across two ports.
    Action:   run.
    Validate: every input's bytes arrive under ``outputs/``, unchanged.
    """
    inputs = [
        InputSpec(relpath='a.csv', data=b'alpha\n', port='left'),
        InputSpec(relpath='b.csv', data=b'bravo\n', port='left'),
        InputSpec(relpath='c.csv', data=b'charlie\n', port='right'),
        InputSpec(relpath='d.csv', data=b'delta\n', port='right'),
    ]
    job = make_job(inputs=inputs)
    result = job.run()

    assert result.exit_code == 0, result.output
    for spec in inputs:
        assert job.endpoint.body_of(f'outputs/{spec.relpath}') == spec.data


@conforms_today
@traces_to(
    'pipelines/external_finalize.py _publish_and_verify resolves every inventoried relpath against the '
    'staging prefix and copies it — "for obj in marker.objects: source = _resolve_object_uri('
    'staging_prefix, obj.relpath)". A marker whose relpath does not name the key the object was written '
    'under fails collection on a missing object, with no hint that the NAME was the problem.'
)
def test_the_inventory_names_the_keys_the_objects_were_really_written_under(make_job):
    """Percent signs, spaces and non-ASCII are ordinary object keys, not escapes.

    Setup:    three inputs whose names contain a literal ``%``, a space, and non-Latin
              characters, plus one with directory components.
    Action:   run.
    Validate: every relpath the marker inventories resolves to an object the store
              actually holds, byte for byte.

    This is the conformance half of "the copy went through": a step that
    percent-encodes a key on the way out and reports the un-encoded name in its marker
    publishes an inventory that does not match the store, and collection fails on an
    object nobody can find.
    """
    inputs = [
        InputSpec(relpath='100%25-done.csv', data=b'percent\n'),
        InputSpec(relpath='with space.csv', data=b'space\n'),
        InputSpec(relpath='ünïcode-日本.csv', data=b'unicode\n'),
        InputSpec(relpath='year=2026/month=08/rows.csv', data=b'nested\n'),
    ]
    job = make_job(inputs=inputs)
    result = job.run()
    assert result.exit_code == 0, result.output

    uploaded = set(job.endpoint.keys_in_order())
    for obj in job.marker()['objects']:
        assert obj['relpath'] in uploaded, (
            f'the marker inventories {obj["relpath"]!r}, which the store never received; it holds {sorted(uploaded)}'
        )
    for spec in inputs:
        assert job.endpoint.body_of(f'outputs/{spec.relpath}') == spec.data


@conforms_today
@reference_quality(_PIN_CHECKING)
def test_an_input_whose_bytes_changed_is_refused(make_job):
    """Setup:    an input pinned to one digest, served as different bytes of the same length.
    Action:   run.
    Validate: the step refuses it, exits permanent (10), and says which object.
    """
    job = make_job(inputs=[InputSpec(relpath='swapped.csv', data=b'pinned', served=b'pinnEd')])
    result = job.run()

    assert result.exit_code == contract.EXIT_PERMANENT, f'expected a permanent refusal:\n{result.output}'
    marker = job.marker()
    assert marker['status'] == 'failed'
    assert 'swapped.csv' in (marker['error'] or ''), marker['error']


@conforms_today
@reference_quality(_PIN_CHECKING)
def test_an_input_of_the_wrong_size_is_refused(make_job):
    """Setup:    an input the JOB pins at 999 bytes — in the manifest and the envelope
              alike, as production copies one validated list into both — which the store
              serves as three.
    Action:   run.
    Validate: refused, permanently, naming the pinned size.

    The pin override deliberately lands in both documents. Changing only the envelope
    would hand the step a manifest and an envelope that contradict each other, which is a
    malformed platform document rather than the thing this test is named for.
    """
    job = make_job(inputs=[InputSpec(relpath='short.csv', data=b'abc', size=999)])
    result = job.run()

    assert result.exit_code == contract.EXIT_PERMANENT, result.output
    assert '999' in (job.marker()['error'] or ''), job.marker()['error']


@conforms_today
@reference_quality(_PIN_CHECKING)
def test_an_envelope_entry_with_no_hash_pin_is_refused(make_job):
    """An input nobody can verify is an input nobody should act on.

    Setup:    an envelope entry with no ``sha256``. The manifest still carries one,
              because it cannot do otherwise — ``external/contract.py`` types
              ``InputObject.sha256`` as a required digest, so an unpinned input is a
              shape only the envelope can express. The test is named for what it really
              builds.
    Action:   run.
    Validate: refused, permanently.
    """
    job = make_job(inputs=[InputSpec(relpath='unpinned.csv', data=b'anything', drop_sha256=True)])
    result = job.run()

    assert result.exit_code == contract.EXIT_PERMANENT, result.output
    assert job.marker()['status'] == 'failed'


@expected_red_until_fixed
@traces_to(
    'external/contract.py CompletionMarker._check_inventory_is_unique refuses a marker that inventories '
    'one relpath twice — "one file, one entry (two entries could otherwise carry two different hashes '
    'for the same path)" — and pipelines/external_finalize.py raises ExternalCollectionError for a '
    'marker it cannot parse: "The step wrote an invalid completion marker". Nothing is published.'
)
def test_the_marker_stays_parseable_when_two_ports_carry_the_same_name(make_job):
    """The step must not write a document the collector refuses.

    Setup:    two input ports, each with an object called ``data.csv``, holding different
              bytes.
    Action:   run.
    Validate: the marker the step wrote parses against the contract.

    The step derives its output path from the input's ``relpath`` alone and ignores the
    ``port`` the envelope carries beside it (``runners/credentials.py`` puts one there:
    ``entry = {key: obj[key] for key in ('port', 'relpath', 'sha256', 'size')}``). Both
    objects go to ``outputs/data.csv`` and the marker inventories that path twice, with
    two different hashes. The run exits 0 and publishes NOTHING, because the collector
    refuses the whole document at parse time — the worst possible shape for a failure,
    since everything looked fine from the outside.
    """
    job = make_job(
        inputs=[
            InputSpec(relpath='data.csv', data=b'from the left port\n', port='left'),
            InputSpec(relpath='data.csv', data=b'from the right port\n', port='right'),
        ]
    )
    result = job.run()
    assert result.exit_code == 0, result.output

    job.marker()  # raises ContractViolation if the collector would refuse this document


@expected_red_until_fixed
@reference_quality(
    'Which output path an object belongs on is entirely the step\'s business — the contract constrains '
    'the document, not the layout. But a step that silently loses one of two inputs is not a step anybody '
    'should copy, and this is what the collapse costs beyond the unparseable marker: the second upload '
    'overwrites the first, so one customer\'s data is simply gone. How to keep them apart (namespacing by '
    'port is the obvious way) is the fix slice\'s decision, and this test deliberately does not pre-empt it.'
)
def test_neither_input_survives_at_the_others_expense(make_job):
    """Two ports may legitimately carry a file of the same name. They are different files.

    Setup:    two input ports, each with an object called ``data.csv``, holding different
              bytes.
    Action:   run.
    Validate: both sets of bytes are somewhere in the store afterwards.
    """
    job = make_job(
        inputs=[
            InputSpec(relpath='data.csv', data=b'from the left port\n', port='left'),
            InputSpec(relpath='data.csv', data=b'from the right port\n', port='right'),
        ]
    )
    result = job.run()
    assert result.exit_code == 0, result.output

    bodies = {job.endpoint.body_of(key) for key in set(job.endpoint.keys_in_order()) if key.startswith('outputs/')}
    assert bodies == {b'from the left port\n', b'from the right port\n'}, (
        f'one input overwrote the other; the store holds {bodies}'
    )


@expected_red_until_fixed
@reference_quality(
    'The contract says nothing about memory: the ceiling is the operator\'s (agent/executors/docker_exec.py '
    'passes memory= from the agent\'s config) and the input size is the pipeline\'s. So this is not a '
    'conformance failure. It is here because the example teaches whoever copies it how to move bytes, and '
    'read-it-all-into-a-variable does not survive contact with a real batch: peak memory is at least twice '
    'the input, and an OOM kill exits 137, which external/contract.py classify_exit calls transient — so '
    'the orchestrator faithfully retries a job that cannot ever succeed.'
)
def test_an_input_larger_than_the_container_is_not_fatal(make_job):
    """Memory is a ceiling the operator sets, not a promise about input size.

    Setup:    a 128 MiB input, a container limited to 64 MiB of memory and no swap.
    Action:   run.
    Validate: the container is not OOM-killed, and the object is copied through.
    """
    if not docker.swap_limit_supported():
        pytest.skip(
            'this kernel has no swap accounting, so docker cannot enforce a hard memory ceiling; '
            'the container would page out instead of being OOM-killed and this test would pass '
            'for a reason that has nothing to do with the step'
        )
    job = make_job(
        inputs=[InputSpec(relpath='big.bin', synthetic_size=OVERSIZED_INPUT_BYTES)],
        max_object_bytes=OVERSIZED_INPUT_BYTES * 2,
    )
    result = job.run(memory=TIGHT_MEMORY, timeout=300)

    assert not result.oom_killed, (
        f'the container was OOM-killed reading a {OVERSIZED_INPUT_BYTES // (1024 * 1024)} MiB input into '
        f'{TIGHT_MEMORY} of memory; it exited {result.exit_code}, which classifies as '
        f'{contract.classify_exit(result.exit_code)!r} — so this is retried forever'
    )
    assert result.exit_code == 0, result.output
    assert 'outputs/big.bin' in job.endpoint.keys_in_order()
