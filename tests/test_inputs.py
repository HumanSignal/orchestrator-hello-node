"""What the step reads, and what it is allowed to assume about it.

Every input arrives pinned: the orchestrator hashed the object when it built the job,
and that hash is its statement about what this step is supposed to be reading. Verifying
it is not ceremony — the URL points at a bucket, and "the bytes at that key today" is not
automatically "the bytes that were there when the job was created". A step that skips the
check can process the wrong version of its input and produce output that passes every
check on the way back, because those checks are about what it WROTE.
"""

from __future__ import annotations

import pytest

from conformance import contract, docker
from conformance.job import InputSpec
from conformance.markers import conforms_today, expected_red_until_fixed

#: One eighth of a gigabyte of input against a 64 MiB container. Big enough that no
#: amount of interpreter overhead explains the difference, small enough to stay quick.
OVERSIZED_INPUT_BYTES = 128 * 1024 * 1024
TIGHT_MEMORY = '64m'


@conforms_today
def test_a_job_with_no_inputs_still_completes(make_job):
    """A step with nothing to read still has something to say.

    Setup:    a job with an empty input list.
    Action:   run.
    Validate: ``result.json`` and the marker are written, in that order, and the marker
              inventories the result.
    """
    job = make_job(inputs=[])
    result = job.run()

    assert result.exit_code == 0, result.output
    assert job.endpoint.keys_in_order() == [contract.RESULT_FILENAME, contract.MARKER_FILENAME]
    assert job.marker()['status'] == 'succeeded'


@conforms_today
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
def test_a_nested_relpath_survives_the_round_trip(make_job):
    """Setup:    an input whose relpath has directory components.
    Action:   run.
    Validate: the object lands under the same nested path, and the marker's relpath is
              canonical (no leading slash, no empty component).
    """
    job = make_job(inputs=[InputSpec(relpath='year=2026/month=08/rows.csv', data=b'nested\n')])
    result = job.run()

    assert result.exit_code == 0, result.output
    assert job.endpoint.body_of('outputs/year=2026/month=08/rows.csv') == b'nested\n'
    assert {obj['relpath'] for obj in job.marker()['objects']} >= {'outputs/year=2026/month=08/rows.csv'}


@conforms_today
def test_awkward_characters_in_a_name_are_not_mangled(make_job):
    """Percent signs, spaces and non-ASCII are ordinary object keys, not escapes.

    Setup:    three inputs whose names contain a literal ``%``, a space, and non-Latin
              characters.
    Action:   run.
    Validate: each lands under exactly the key the step declared, byte for byte.

    A step that percent-encodes a key on the way out and reports the un-encoded name in
    its marker publishes an inventory that does not match the store — and collection
    fails on a missing object with no hint that the name was the problem.
    """
    inputs = [
        InputSpec(relpath='100%25-done.csv', data=b'percent\n'),
        InputSpec(relpath='with space.csv', data=b'space\n'),
        InputSpec(relpath='ünïcode-日本.csv', data=b'unicode\n'),
    ]
    job = make_job(inputs=inputs)
    result = job.run()

    assert result.exit_code == 0, result.output
    uploaded = set(job.endpoint.keys_in_order())
    for spec in inputs:
        assert f'outputs/{spec.relpath}' in uploaded, f'{spec.relpath!r} landed under a different key: {uploaded}'
        assert job.endpoint.body_of(f'outputs/{spec.relpath}') == spec.data


@conforms_today
def test_an_input_whose_bytes_changed_is_refused(make_job):
    """Setup:    an input pinned to one digest, served as different bytes of the same length.
    Action:   run.
    Validate: the step refuses it, exits permanent (10), and says which object and both
              digests.
    """
    job = make_job(inputs=[InputSpec(relpath='swapped.csv', data=b'pinned', served=b'pinnEd')])
    result = job.run()

    assert result.exit_code == contract.EXIT_PERMANENT, f'expected a permanent refusal:\n{result.output}'
    marker = job.marker()
    assert marker['status'] == 'failed'
    assert 'swapped.csv' in (marker['error'] or ''), marker['error']


@conforms_today
def test_an_input_of_the_wrong_size_is_refused(make_job):
    """Setup:    an input pinned at a size the served bytes do not have.
    Action:   run.
    Validate: refused, permanently, naming the two sizes.
    """
    job = make_job(inputs=[InputSpec(relpath='short.csv', data=b'abc', size=999)])
    result = job.run()

    assert result.exit_code == contract.EXIT_PERMANENT, result.output
    assert '999' in (job.marker()['error'] or ''), job.marker()['error']


@conforms_today
def test_an_input_with_no_hash_pin_is_refused(make_job):
    """An input nobody can verify is an input nobody should act on.

    Setup:    an envelope entry with no ``sha256``.
    Action:   run.
    Validate: refused, permanently.
    """
    job = make_job(inputs=[InputSpec(relpath='unpinned.csv', data=b'anything', drop_sha256=True)])
    result = job.run()

    assert result.exit_code == contract.EXIT_PERMANENT, result.output
    assert job.marker()['status'] == 'failed'


@expected_red_until_fixed
def test_two_ports_delivering_the_same_name_do_not_collapse(make_job):
    """Two ports may legitimately carry a file of the same name. They are different files.

    Setup:    two input ports, each with an object called ``data.csv``, holding
              different bytes.
    Action:   run.
    Validate: both sets of bytes survive under distinct relpaths, and the marker is a
              document the collector would accept.

    Today both are written to ``outputs/data.csv``: the second upload overwrites the
    first, and the marker inventories that one path twice — with two different hashes.
    The collector refuses the whole marker at parse time, so a run that looked
    successful publishes nothing at all, and one of the two inputs is simply gone.
    """
    job = make_job(
        inputs=[
            InputSpec(relpath='data.csv', data=b'from the left port\n', port='left'),
            InputSpec(relpath='data.csv', data=b'from the right port\n', port='right'),
        ]
    )
    result = job.run()
    assert result.exit_code == 0, result.output

    marker = job.marker()  # raises if the collector would refuse this document
    delivered = [obj['relpath'] for obj in marker['objects'] if obj['relpath'] != contract.RESULT_FILENAME]
    assert len(set(delivered)) == 2, f'the two inputs collapsed onto {delivered}'

    bodies = {job.endpoint.body_of(relpath) for relpath in delivered}
    assert bodies == {b'from the left port\n', b'from the right port\n'}, (
        f'one input overwrote the other; the store holds {bodies}'
    )


@expected_red_until_fixed
def test_an_input_larger_than_the_container_is_not_fatal(make_job):
    """Memory is a ceiling the operator sets, not a promise about input size.

    Setup:    a 128 MiB input, a container limited to 64 MiB of memory and no swap.
    Action:   run.
    Validate: the container is not OOM-killed, and the object is copied through.

    The step reads the whole object into a variable, hashes that variable, and then
    hands the same bytes to an HTTP client that builds a second copy for the request
    body. Peak usage is therefore at least twice the input, and the input size is chosen
    by the pipeline, not by the step. A step that streams — read a block, hash the block,
    write the block — has a memory profile that does not depend on its input at all.
    An OOM kill also exits 137, which the contract classifies as TRANSIENT, so the
    orchestrator faithfully retries a job that cannot ever succeed.
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
