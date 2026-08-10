"""What the step reads, and what it does with it.

Every input arrives pinned: the orchestrator hashed the object when it built the job, and
that hash is its statement about what this step is supposed to be reading.

**Most of this file is reference quality, not conformance, and it says so.** The contract
does not require a step to verify its inputs, to copy them anywhere, or to produce any
particular output at all — it describes the documents, not the work. What makes these
tests worth running is that this repository is the file customers copy: the behaviour
asserted here is the behaviour the example is supposed to teach, and for the pin checks
the orchestrator's own test for this node asserts the same thing.

**Where that last claim holds, precisely.** ``tests/test_hello_node_example.py`` requires
the phrases *"hashes to"* and *"the job pinned"* in the marker's error — for the
hash-mismatch case, and only there. It says nothing about the wording of the wrong-size
message or the missing-pin message. This file mirrors the orchestrator on the case it
really covers and says plainly, on the others, that the expectation is ours.

Two tests here ARE conformance, and both are about documents rather than about copying:
the marker must be one the collector can parse, and every relpath it inventories must
name an object that really exists under it.
"""

from __future__ import annotations

import pytest

from conformance import contract, docker
from conformance.job import InputSpec
from conformance.markers import conforms_today, reference_quality, traces_to

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
    'The contract permits arbitrary output semantics: a step may produce anything, or nothing, at any '
    'relpath it likes. That the reference node copies each input through unchanged to '
    'outputs/<the input\'s relpath> is ITS behaviour, and the reason to pin it is that customers copy '
    'this file — a passthrough that quietly mangled a name or dropped a byte would be taught to '
    'everyone who started from it.'
)

#: Names that are ordinary object keys and look like escapes. Shared by the two tests
#: below, which ask different questions about the same run and rest on different
#: authorities: whether the marker names real keys (the collector's rule) and whether the
#: bytes came through under the name they went in with (this node's own behaviour).
AWKWARD_NAMES = [
    InputSpec(relpath='100%25-done.csv', data=b'percent\n'),
    InputSpec(relpath='with space.csv', data=b'space\n'),
    InputSpec(relpath='ünïcode-日本.csv', data=b'unicode\n'),
    InputSpec(relpath='year=2026/month=08/rows.csv', data=b'nested\n'),
]


@conforms_today
@reference_quality(_COPYING)
def test_a_job_with_no_inputs_still_completes(make_job):
    """A step with nothing to read still has something to say.

    Setup:    a job with an empty input list.
    Action:   run.
    Validate: a ``result.json`` is written, the marker is written LAST, and it says the
              run succeeded.

    The ORDER is contract (the marker is written last); the fact that a step with no
    inputs writes a ``result.json`` at all is this node's own choice. What this
    deliberately does NOT say is "and nothing else" — it used to compare the whole key
    list for equality, which forbade the step from writing ``logs.ndjsonl``, a file the
    contract names and invites.
    """
    job = make_job(inputs=[])
    result = job.run()

    assert result.exit_code == 0, result.output
    written = job.endpoint.keys_in_order()
    assert written[-1] == contract.MARKER_FILENAME, f'the marker was not written last: {written}'
    assert contract.RESULT_FILENAME in written, f'this node writes a result even with no inputs; it wrote {written}'
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
    'staging prefix and copies it: "for obj in marker.objects:" … "source = _resolve_object_uri('
    'staging_prefix, obj.relpath)". Its docstring — "Copy every promised object into the published area, '
    'then verify what LANDED there" — is the whole of what collection publishes, so a marker whose '
    'relpath does not name the key the object was written under fails on a missing object, with no hint '
    'that the NAME was the problem.'
)
def test_the_inventory_names_the_keys_the_objects_were_really_written_under(make_job):
    """Percent signs, spaces and non-ASCII are ordinary object keys, not escapes.

    Setup:    three inputs whose names contain a literal ``%``, a space, and non-Latin
              characters, plus one with directory components.
    Action:   run.
    Validate: every relpath the marker inventories names an object the store really
              received.

    A step that percent-encodes a key on the way out and reports the un-encoded name in
    its marker publishes an inventory that does not match the store, and collection fails
    on an object nobody can find.

    This test used to also require every input to reappear at ``outputs/<its relpath>``.
    That is a real expectation of this reference node and it is asserted immediately
    below — but the collector's rule says nothing about where a step puts its outputs or
    whether it copies anything at all, so the two claims cannot share one citation.

    Nothing about the run's outcome is asserted: the rule binds a marker's contents
    whenever one is written, and a run that writes no marker, or one that inventories
    nothing, has nothing this rule could refuse — those skip.
    """
    job = make_job(inputs=AWKWARD_NAMES)
    job.run()

    if contract.MARKER_FILENAME not in job.endpoint.keys_in_order():
        pytest.skip('the step wrote no marker, so there is no inventory to hold against the store')
    uploaded = set(job.endpoint.keys_in_order())
    inventoried = [obj['relpath'] for obj in job.marker()['objects']]
    if not inventoried:
        pytest.skip('the marker inventories nothing, so no relpath in it can name an object that is missing')
    for relpath in inventoried:
        assert relpath in uploaded, (
            f'the marker inventories {relpath!r}, which the store never received; it holds {sorted(uploaded)}'
        )


@conforms_today
@reference_quality(_COPYING)
def test_awkward_names_survive_the_copy_unchanged(make_job):
    """Setup:    the same four names — a literal ``%``, a space, non-Latin characters and
              directory components.
    Action:   run.
    Validate: each input's bytes are readable at ``outputs/<its relpath>``.
    """
    job = make_job(inputs=AWKWARD_NAMES)
    result = job.run()
    assert result.exit_code == 0, result.output

    for spec in AWKWARD_NAMES:
        assert job.endpoint.body_of(f'outputs/{spec.relpath}') == spec.data


@conforms_today
@reference_quality(
    _PIN_CHECKING
    + ' The exact WORDING is pinned here for one case only, and this is that case: the orchestrator\'s '
    'own test_an_input_that_does_not_match_its_pin_fails_the_step asserts "\'hashes to\' in '
    'marker[\'error\'] and \'the job pinned\' in marker[\'error\']". Two suites asserting the same '
    'sentence is what turns a message into an interface, and this file is on the other side of it.'
)
def test_an_input_whose_bytes_changed_is_refused(make_job):
    """Setup:    an input pinned to one digest, served as different bytes of the same length.
    Action:   run.
    Validate: the step refuses it, exits permanent (10), names the object, and says both
              what it got and what was pinned — in the words the orchestrator's own test
              for this file expects.
    """
    job = make_job(inputs=[InputSpec(relpath='swapped.csv', data=b'pinned', served=b'pinnEd')])
    result = job.run()

    assert result.exit_code == contract.EXIT_PERMANENT, f'expected a permanent refusal:\n{result.output}'
    marker = job.marker()
    assert marker['status'] == 'failed'
    error = marker['error'] or ''
    assert 'swapped.csv' in error, error
    for phrase in ('hashes to', 'the job pinned'):
        assert phrase in error, (
            f'the orchestrator\'s own test for this file requires the phrase {phrase!r} in the marker\'s '
            f'error; it says: {error!r}'
        )


@conforms_today
@reference_quality(
    _PIN_CHECKING
    + ' Note what is NOT borrowed here: the orchestrator asserts particular wording only for the '
    'hash-mismatch message, never for this one. That the size failure names the pinned number is an '
    'expectation of ours alone — a size error saying only "the input is the wrong size" would be '
    'conformant, and useless to whoever has to work out which side is wrong.'
)
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


@conforms_today
@traces_to(
    'external/contract.py CompletionMarker._check_inventory_is_unique refuses a marker that inventories '
    'one relpath twice — "one file, one entry (two entries could otherwise carry two different hashes '
    'for the same path)" — and pipelines/external_finalize.py raises ExternalCollectionError for a '
    'marker it cannot parse: "The step wrote an invalid completion marker". Nothing is published. '
    'Deleting the offending marker is not a way out, and the same sources say so: agent/runner.py '
    '_classify reports a job as succeeded on "exit_code == 0", and pipelines/external_finalize.py '
    '_read_and_check_marker then refuses the run outright — "A missing marker after a reported success '
    'is a real failure, not a retry" — because "Nothing can be published: the marker is the inventory '
    'of what the step produced."'
)
def test_the_marker_stays_parseable_when_two_ports_carry_the_same_name(make_job):
    """The step must not write a document the collector refuses.

    Setup:    two input ports, each with an object called ``data.csv``, holding different
              bytes.
    Action:   run.
    Validate: if the step wrote a marker, it parses against the contract.

    **The outcome of the run is not asserted, but it decides what silence means.** The rule
    cited above forbids exactly one thing: publishing a document the collector cannot parse.
    Noticing the collision and refusing the job is a perfectly conformant answer — with a
    valid failure marker or with none at all — and an earlier version of this test required
    exit 0, which would have turned that answer red. The fix belongs to whoever writes it.

    So a missing marker is "nothing to check" ONLY after a failure or a cancellation. After
    exit 0 it is the opposite: the agent reports the job as succeeded, and the collector
    treats a success with no marker as a hard failure of the whole run. An earlier version
    of this test skipped on any missing marker whatsoever, which handed the fix an escape
    route — delete the invalid document, keep exiting 0, and the regression proof goes
    quietly green over a run that publishes nothing and fails at collection.

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

    if contract.MARKER_FILENAME not in job.endpoint.keys_in_order():
        assert result.exit_code != 0, (
            'the step exited 0 and wrote no completion marker. That is not the permitted silence: the '
            'agent reports exit 0 as a success, and the collector refuses a reported success with no '
            'marker — "Nothing can be published: the marker is the inventory of what the step produced". '
            'The whole run fails at collection, having done all of the work'
        )
        pytest.skip(
            f'the step ended the run with exit {result.exit_code} and wrote no marker at all, which the '
            f'contract permits of a failed or cancelled step — a marker that does not exist is not a '
            f'marker the collector refuses'
        )
    job.marker()  # raises ContractViolation if the collector would refuse this document


@conforms_today
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
    Validate: if the step did the work, both sets of bytes are in the store afterwards.

    A step that notices the collision and refuses the job destroys nothing, and that is
    one of the fixes this test promises not to pre-empt — so a failed run skips here
    rather than counting as bytes lost.

    **The two bodies must be AMONG what landed, not the whole of it.** The expectation is
    that neither input was lost; it is not that these are the only two objects a step may
    produce. This used to compare the set of output bodies for equality, which quietly
    forbade an auxiliary output — an index, a manifest of what went where, a report — and
    would have turned a correct fix red for writing one. Nothing in the contract, and
    nothing in this test's own reasoning, says a step may not write more than it was given.
    """
    job = make_job(
        inputs=[
            InputSpec(relpath='data.csv', data=b'from the left port\n', port='left'),
            InputSpec(relpath='data.csv', data=b'from the right port\n', port='right'),
        ]
    )
    result = job.run()

    if result.exit_code != 0:
        pytest.skip(
            f'the step ended the run with exit {result.exit_code} instead of producing output for two '
            f'colliding names, so nothing was silently overwritten — which is the other legal answer'
        )
    bodies = {job.endpoint.body_of(key) for key in set(job.endpoint.keys_in_order()) if key.startswith('outputs/')}
    expected = {b'from the left port\n', b'from the right port\n'}
    assert expected <= bodies, (
        f'one input overwrote the other: {sorted(expected - bodies)} is in no output object. The store '
        f'holds {bodies}'
    )


@conforms_today
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
