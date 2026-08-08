"""The completion marker: what it says, and — above all — WHEN it is written.

The marker is the terminal receipt. Its mere existence is the orchestrator's proof that
everything it inventories is already readable, so it is written strictly last, after
every output object. A marker written early makes a half-finished run indistinguishable
from a complete one, and nothing in production can tell the difference: real object
storage has no notion of "this file arrived before that one". This harness does, which
is the reason the ordering assertions live here and nowhere else.

**What the marker is NOT required to carry.** ``idempotency_key``, ``exit_code`` and
``error`` are all optional (``external/contract.py:528-533``, and the orchestrator's own
``test_idempotency_key_is_optional``), and collection cross-checks only
execution/attempt/generation. This file asserts each of them *conditionally* — a marker
that omits one is conformant, a marker that states one that disagrees with reality is
not.
"""

from __future__ import annotations

import pytest

from conformance import contract
from conformance.job import InputSpec
from conformance.markers import conforms_today, reference_quality, subject_is_platform, traces_to


@conforms_today
@traces_to(
    'external/contract.py CompletionMarker: "THE MARKER IS THE TERMINAL RECEIPT: the step writes it '
    'strictly last, after every output object, result.json, and the logs are durably in place. That '
    'ordering is the whole point — the marker\'s existence is the orchestrator\'s proof that everything '
    'it references is already readable."'
)
def test_the_marker_is_the_last_object_uploaded_and_is_written_once(make_job):
    """Everything the marker names must already be readable when it appears — and it
    appears exactly once.

    Setup:    two inputs, so there is more than one object to get the order wrong with.
    Action:   run.
    Validate: the marker is the last key the store accepted, it was accepted exactly
              ONCE, and every relpath it inventories arrived strictly before it.

    "Once" is not pedantry, and the orchestrator's own example test says so in the same
    words: a marker written twice means an earlier version of it was visible, which is
    the exact failure the rule exists to prevent — a reader that saw the first one would
    have treated an unfinished run as finished.
    """
    job = make_job(inputs=[InputSpec(relpath='a.csv', data=b'a\n'), InputSpec(relpath='b.csv', data=b'b\n')])
    result = job.run()
    assert result.exit_code == 0, result.output

    order = job.endpoint.keys_in_order()
    assert order[-1] == contract.MARKER_FILENAME, f'the marker was not written last: {order}'
    assert order.count(contract.MARKER_FILENAME) == 1, (
        f'the marker was written {order.count(contract.MARKER_FILENAME)} times: {order}. An earlier '
        f'version of it was visible to anyone reading, and that is a finished run as far as they know'
    )

    marker_position = len(order) - 1
    for obj in job.marker()['objects']:
        landed = job.endpoint.last_index(obj['relpath'])
        assert landed is not None and landed < marker_position, (
            f'{obj["relpath"]!r} is inventoried by a marker that was written before it'
        )


@conforms_today
@traces_to(
    'pipelines/external_finalize.py _read_and_check_marker: "expected = (attempt.execution_id, '
    'attempt.attempt, launch.generation); found = (marker.execution_id, marker.attempt, '
    'marker.generation); if found != expected: … A receipt from a superseded or unrelated run must '
    'never be collected as this one."'
)
def test_the_marker_identifies_the_launch_it_is_a_receipt_for(make_job, sample_input):
    """A receipt identifiable only by where it was found is one misconfiguration away
    from being attributed to the wrong run.

    Setup:    a job with distinctive execution / attempt / generation values.
    Action:   run.
    Validate: the three identity fields collection cross-checks are present and match the
              launch, and the status is ``succeeded``. The two OPTIONAL fields are checked
              only if the marker states them: an ``idempotency_key`` must be the one the
              job was launched with, and an ``exit_code`` must be the code the process
              really returned.
    """
    job = make_job(inputs=[sample_input], execution_id=8181, attempt=3, generation=2)
    result = job.run()
    assert result.exit_code == 0, result.output

    marker = job.marker()
    assert (marker['execution_id'], marker['attempt'], marker['generation']) == (8181, 3, 2)
    assert marker['status'] == 'succeeded'

    if marker.get('idempotency_key') is not None:
        assert marker['idempotency_key'] == job.idempotency_key
    if marker.get('exit_code') is not None:
        assert marker['exit_code'] == result.exit_code


@conforms_today
@traces_to(
    'pipelines/external_finalize.py _publish_and_verify: "Copy every promised object into the published '
    'area, then verify what LANDED there… a container that swaps an object between the two operations '
    'now fails verification instead of publishing unverified bytes under a hash it satisfied a moment '
    'earlier."'
)
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


@conforms_today
@traces_to(
    'external/contract.py CompletionMarker.objects: "Every object produced, with hash and size, relative '
    'to the staging prefix." — and pipelines/external_finalize.py _publish_and_verify iterates '
    '"for obj in marker.objects", so an object the marker does not inventory is never published at all.'
)
def test_everything_the_step_wrote_is_inventoried(make_job, sample_input):
    """An object nobody inventoried is an object nobody will ever see again.

    Setup:    one input, so the step writes one copy plus its own ``result.json``.
    Action:   run.
    Validate: every key the store accepted, other than the marker itself, appears in the
              marker's ``objects``.

    Note what this test does NOT say. Whether ``result.json`` is also claimed by an
    output PORT is a separate question, and the contract's answer is "either is fine":
    the collector turns every relpath in ``produced_ports`` into a downstream artifact
    without caring which files they are, and the orchestrator's own test for this node
    EXPECTS ``result.json`` among them. This harness asserted the opposite for one
    round; that was a preference of ours misfiled as a rule, and it is now recorded as a
    recommendation in ``CONFORMANCE-BASELINE.md`` and nowhere else.
    """
    job = make_job(inputs=[sample_input])
    result = job.run()
    assert result.exit_code == 0, result.output

    marker = job.marker()  # raises if the collector would refuse the document
    inventoried = {obj['relpath'] for obj in marker['objects']}
    written = {key for key in job.endpoint.keys_in_order() if key != contract.MARKER_FILENAME}
    assert written <= inventoried, f'{sorted(written - inventoried)} were written but never inventoried'


@subject_is_platform
@traces_to(
    'external/contract.py CompletionMarker._check_ports_reference_known_objects: "The reverse is '
    'deliberately allowed: an object claimed by no port at all is normal (logs, scratch output, anything '
    'the step wrote but does not deliver)."'
)
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
@traces_to(
    'external/contract.py CompletionMarker.produced_ports: "Within one port a relpath may appear only '
    'once; across two DIFFERENT ports the same file may legitimately be delivered twice."'
)
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
@traces_to(
    'external/contract.py CompletionMarker.produced_ports: "Every relpath listed here MUST also appear '
    'in objects: the inventory is what carries the hash and size, so a port pointing at something '
    'outside it would name a file nobody can verify."'
)
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
@traces_to(
    'external/contract.py CompletionMarker._check_inventory_is_unique: "duplicate relpath(s) … in the '
    'objects inventory — one file, one entry (two entries could otherwise carry two different hashes '
    'for the same path)."'
)
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
@traces_to(
    'external/contract.py check_relpath: "no . or .. components. Traversal is the one that really '
    'matters: ../../etc/x in a marker would otherwise ask a reader to fetch, or a writer to overwrite, '
    'a file outside the run." — plus the empty-component rule, because "two spellings of one path would '
    'be two inventory entries for one file".'
)
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


@subject_is_platform
@reference_quality(
    'Not a rule about the node at all: it pins the harness\'s reading of the contract\'s OPTIONAL marker '
    'fields, so that a future author cannot quietly re-introduce "the marker must state its exit code". '
    'external/contract.py declares idempotency_key, exit_code and error as "… | None = None", and the '
    'orchestrator\'s test_idempotency_key_is_optional deletes the key and still parses the marker.'
)
def test_a_marker_without_the_optional_fields_is_still_valid():
    """The three fields a marker may leave out entirely.

    Setup:    a marker carrying only what is required — version, identity, status,
              inventory, ports.
    Action:   validate.
    Validate: accepted.
    """
    minimal = {
        'schema_version': 1,
        'execution_id': 5,
        'attempt': 1,
        'generation': 1,
        'status': 'succeeded',
        'objects': [_object('outputs/a.csv')],
        'produced_ports': {'output': ['outputs/a.csv']},
    }
    assert contract.validate_marker(minimal) is minimal


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
