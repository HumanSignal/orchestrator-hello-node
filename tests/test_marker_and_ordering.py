"""The completion marker: what it says, and — above all — WHEN it is written.

The marker is the terminal receipt. Its mere existence is the orchestrator's proof that
everything it inventories is already readable, so it is written strictly last, after
every output object. A marker written early makes a half-finished run indistinguishable
from a complete one, and nothing in production can tell the difference: real object
storage has no notion of "this file arrived before that one". This harness does, which
is the reason the ordering assertions live here and nowhere else.
"""

from __future__ import annotations

import pytest

from conformance import contract
from conformance.job import InputSpec
from conformance.markers import conforms_today, expected_red_until_fixed, subject_is_platform


@conforms_today
def test_the_marker_is_the_last_object_uploaded(make_job):
    """Everything the marker names must already be readable when it appears.

    Setup:    two inputs, so there is more than one object to get the order wrong with.
    Action:   run.
    Validate: the marker is the last key the store accepted, and every relpath it
              inventories arrived strictly before it.
    """
    job = make_job(inputs=[InputSpec(relpath='a.csv', data=b'a\n'), InputSpec(relpath='b.csv', data=b'b\n')])
    result = job.run()
    assert result.exit_code == 0, result.output

    order = job.endpoint.keys_in_order()
    assert order[-1] == contract.MARKER_FILENAME, f'the marker was not written last: {order}'

    marker_position = order.index(contract.MARKER_FILENAME)
    for obj in job.marker()['objects']:
        assert order.index(obj['relpath']) < marker_position, (
            f'{obj["relpath"]!r} is inventoried by a marker that was written before it'
        )


@conforms_today
def test_the_marker_identifies_the_launch_it_is_a_receipt_for(make_job, sample_input):
    """A receipt identifiable only by where it was found is one misconfiguration away
    from being attributed to the wrong run.

    Setup:    a job with distinctive execution / attempt / generation values.
    Action:   run.
    Validate: the marker carries all four identity fields, matching the launch.
    """
    job = make_job(inputs=[sample_input], execution_id=8181, attempt=3, generation=2)
    result = job.run()
    assert result.exit_code == 0, result.output

    marker = job.marker()
    assert marker['execution_id'] == 8181
    assert marker['attempt'] == 3
    assert marker['generation'] == 2
    assert marker['idempotency_key'] == job.idempotency_key
    assert marker['status'] == 'succeeded'
    assert marker['exit_code'] == result.exit_code


@conforms_today
def test_every_inventoried_object_carries_the_hash_of_what_was_really_uploaded(make_job):
    """Collection re-reads every object and refuses to publish if one hash disagrees.

    Setup:    two inputs of different sizes.
    Action:   run.
    Validate: for each inventoried object, the hash and size in the marker equal the
              hash and size of the bytes the store actually received.
    """
    job = make_job(inputs=[InputSpec(relpath='short.csv', data=b'x\n'), InputSpec(relpath='long.csv', data=b'y' * 5000)])
    result = job.run()
    assert result.exit_code == 0, result.output

    for obj in job.marker()['objects']:
        upload = job.endpoint.uploaded(obj['relpath'])
        assert upload is not None, f'{obj["relpath"]!r} is inventoried but was never uploaded'
        assert upload.sha256 == obj['sha256'], f'{obj["relpath"]!r}: marker hash disagrees with the bytes received'
        assert upload.size == obj['size'], f'{obj["relpath"]!r}: marker size disagrees with the bytes received'


@expected_red_until_fixed
def test_contract_documents_are_not_delivered_as_output(make_job, sample_input):
    """``result.json`` is a report about the work, not a product of it.

    Setup:    one input, so the step produces exactly one real output object.
    Action:   run.
    Validate: ``result.json`` is inventoried in ``objects`` (so it is published and
              verifiable) but claimed by no output PORT.

    ``produced_ports`` is what feeds the next step in the pipeline. Claiming a contract
    document there means every downstream node receives the step's own metrics document
    as though it were data — silently, because it is a perfectly valid JSON file with a
    correct hash. An object claimed by no port is explicitly normal in the contract, and
    that is what a contract document should be.
    """
    job = make_job(inputs=[sample_input])
    result = job.run()
    assert result.exit_code == 0, result.output

    marker = job.marker()
    inventoried = {obj['relpath'] for obj in marker['objects']}
    assert contract.RESULT_FILENAME in inventoried, 'result.json should still be inventoried and published'

    claimed = {relpath for relpaths in marker['produced_ports'].values() for relpath in relpaths}
    assert contract.RESULT_FILENAME not in claimed, (
        f'result.json is delivered on a port: {marker["produced_ports"]}'
    )


@subject_is_platform
def test_an_object_claimed_by_no_port_is_legal():
    """Logs, scratch files and reports are produced but not delivered.

    Setup:    a marker inventorying two objects and delivering only one.
    Action:   validate it against the contract.
    Validate: it is accepted.
    """
    marker = _marker(
        objects=[_object('outputs/a.csv'), _object('result.json')],
        produced_ports={'output': ['outputs/a.csv']},
    )
    assert contract.validate_marker(marker) is marker


@subject_is_platform
def test_one_file_may_be_delivered_on_two_ports_but_not_twice_on_one():
    """Two ports delivering the same file is meaningful; one port doing it twice is not.

    Setup:    two markers — one delivering ``a.csv`` on two ports, one listing it twice
              on a single port.
    Action:   validate both.
    Validate: the first is accepted, the second is refused by name.
    """
    both_ports = _marker(
        objects=[_object('outputs/a.csv')],
        produced_ports={'primary': ['outputs/a.csv'], 'archive': ['outputs/a.csv']},
    )
    assert contract.validate_marker(both_ports) is both_ports

    twice = _marker(objects=[_object('outputs/a.csv')], produced_ports={'primary': ['outputs/a.csv'] * 2})
    with pytest.raises(contract.ContractViolation, match='more than once'):
        contract.validate_marker(twice)


@subject_is_platform
def test_a_port_may_not_name_an_object_that_is_not_inventoried():
    """The inventory is what carries the hash; a port pointing outside it names a file
    nobody can verify.

    Setup:    a marker delivering a relpath absent from ``objects``.
    Action:   validate.
    Validate: refused, naming the relpath.
    """
    marker = _marker(objects=[_object('outputs/a.csv')], produced_ports={'output': ['outputs/ghost.csv']})
    with pytest.raises(contract.ContractViolation, match='absent from the objects inventory'):
        contract.validate_marker(marker)


@subject_is_platform
def test_one_relpath_cannot_be_inventoried_twice():
    """Two entries for one path could carry two different hashes for the same file.

    Setup:    a marker inventorying ``outputs/a.csv`` twice.
    Action:   validate.
    Validate: refused.
    """
    marker = _marker(objects=[_object('outputs/a.csv'), _object('outputs/a.csv', data=b'different')])
    with pytest.raises(contract.ContractViolation, match='duplicate relpath'):
        contract.validate_marker(marker)


@subject_is_platform
@pytest.mark.parametrize(
    'relpath',
    ['/absolute.csv', 'a\\b.csv', 'a//b.csv', 'trailing/', '../escape.csv', './here.csv', 'line\nbreak.csv', ''],
)
def test_relpaths_that_cannot_be_resolved_are_refused(relpath):
    """A relpath is resolved against a staging prefix by whoever reads it.

    Setup:    each spelling that is ambiguous, non-relative, or able to point outside
              the prefix.
    Action:   validate it.
    Validate: refused.
    """
    with pytest.raises(contract.ContractViolation):
        contract.check_relpath(relpath, 'test')


def _object(relpath: str, data: bytes = b'x') -> dict:
    import hashlib

    return {'relpath': relpath, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}


def _marker(**overrides) -> dict:
    document = {
        'schema_version': 1,
        'execution_id': 1,
        'attempt': 1,
        'generation': 1,
        'idempotency_key': 'k',
        'status': 'succeeded',
        'exit_code': 0,
        'objects': [],
        'produced_ports': {},
        'error': None,
    }
    document.update(overrides)
    return document
