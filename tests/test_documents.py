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
import logging

import pytest

from conformance import contract, platform_rules
from conformance.job import InputSpec
from conformance.markers import (
    conforms_today,
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


@conforms_today
@reference_quality(
    'Nothing obliges a node to write search facts, and a document without the key is an ordinary '
    'document: the orchestrator reads the optional "facts" list out of result.json when it accepts this '
    'attempt\'s marker, records the entries that are well formed and skips the rest, and a run that '
    'writes none is collected exactly as before. What makes it worth demonstrating is what the '
    'alternative costs an operator. A step is looked for by the name of a thing it touched — one input '
    'out of four hundred, a task id, a delivered file — and a node that records nothing is findable '
    'only by an execution number somebody would have to already have. This is docs/AUTHORING.md '
    'section 6, executed.'
)
def test_the_result_document_names_every_input_the_step_copied(make_job):
    """The run has to be findable afterwards by something a human would think to type.

    Setup:    three inputs, one of them under a nested relpath so the input's own name and
              the path its copy was written to are visibly different strings.
    Action:   run, and read ``result.json``.
    Validate: it carries one ``input`` fact per input, whose value is the name the JOB
              gave that object — and the harness has nothing to report about any of them.

    **The value is the input's name, not the output copy's path**, and that is the whole
    content of this test beyond "the key exists". Those two differ by more than a prefix:
    the step chooses ``outputs/<name>``, disambiguates a collision by port, and adds a
    counter after that, so the copy's path is a name only this step has ever seen. A run
    findable by ``input:outputs/nested/three.csv`` is findable by a string nobody else
    holds; one findable by ``input:nested/three.csv`` is findable by the name the pipeline
    upstream used for that artifact.

    The entries are also put through the harness's re-statement of the orchestrator's own
    recording rules, which reports rather than refuses (see ``conformance.contract``
    ``Recommendation``): a fact this node writes that the orchestrator would silently skip
    is a fact the reference node is teaching everyone who copies it to write.
    """
    inputs = [
        InputSpec(relpath='one.csv', data=b'a\n'),
        InputSpec(relpath='two.csv', data=b'b\n'),
        InputSpec(relpath='nested/three.csv', data=b'c\n'),
    ]
    job = make_job(inputs=inputs)
    result = job.run()
    assert result.exit_code == 0, result.output

    if contract.RESULT_FILENAME not in job.endpoint.keys_in_order():
        pytest.skip('the step wrote no result.json, so there is no document here to carry facts')
    raw = job.endpoint.body_of(contract.RESULT_FILENAME)
    findings: list = []
    document = contract.validate_result(json.loads(raw.decode('utf-8')), raw_bytes=raw,
                                        recommendations=findings)

    facts = document.get('facts')
    assert isinstance(facts, list), (
        f'the step copied {len(inputs)} inputs and its result document carries facts={facts!r}. '
        f'Nothing fails, and the run is then findable only by its execution number'
    )
    assert [fact.get('value') for fact in facts if fact.get('key') == 'input'] == [
        spec.relpath for spec in inputs
    ], (
        f'the input facts are {[fact.get("value") for fact in facts]}, and the inputs this job pinned '
        f'were {[spec.relpath for spec in inputs]} — a search for the name the pipeline uses finds this '
        f'run only if that is the name recorded'
    )
    assert not findings, 'the orchestrator would decline to record: ' + '; '.join(findings)


#: A relpath the platform is perfectly happy with and the search-fact table is not: three
#: path components, each well under any filesystem's limit, adding up past the 512
#: characters a fact VALUE may carry. Nothing refuses it anywhere on the way in.
A_RELPATH_TOO_LONG_TO_BE_A_FACT = 'a' * 250 + '/' + 'b' * 250 + '/' + 'c' * 60 + '.csv'


@conforms_today
@reference_quality(
    'The orchestrator applies its own ingest rules to every fact and skips the entries that break '
    'them, so a node that writes whatever it has is not punished — it is ignored. That is the whole '
    'problem: the rejection happens inside somebody else\'s worker log, the run is collected normally, '
    'and the only symptom is that the run answers to nothing when it is searched for weeks later. A '
    'reference node applies the rules at the point of writing, where its own author can still see the '
    'answer, and says in its own report what it could not claim. Nothing on the platform side checks '
    'any of this.'
)
def test_a_name_too_long_to_be_a_fact_is_not_claimed_as_one(make_job):
    """A value the orchestrator would drop is not written as though it had been kept.

    Setup:    two inputs — one ordinary, and one whose relpath runs past the 512
              characters a fact value may carry, in three components each well under any
              filesystem's limit. Every document on the way in accepts it.
    Action:   run, and read ``result.json``.
    Validate: the short name is claimed; the long one is NOT; and the summary says a value
              was left unclaimed, so the node's author is told rather than left guessing.

    The pair matters more than either half. Asserting only that the long name is absent
    would also pass for a node that had stopped writing facts altogether, and asserting
    only that something was noted would pass for a node that noted everything. What is
    being pinned is that the run stays findable by what it CAN be found by, and says what
    it cannot.
    """
    inputs = [
        InputSpec(relpath='short.csv', data=b'a\n'),
        InputSpec(relpath=A_RELPATH_TOO_LONG_TO_BE_A_FACT, data=b'b\n'),
    ]
    assert len(A_RELPATH_TOO_LONG_TO_BE_A_FACT) > contract.MAX_FACT_VALUE_CHARS, (
        'the long name is inside the limit, so this test would prove nothing'
    )
    job = make_job(inputs=inputs)
    result = job.run()
    assert result.exit_code == 0, result.output

    if contract.RESULT_FILENAME not in job.endpoint.keys_in_order():
        pytest.skip('the step wrote no result.json, so there is no document here to carry facts')
    raw = job.endpoint.body_of(contract.RESULT_FILENAME)
    findings: list = []
    document = contract.validate_result(json.loads(raw.decode('utf-8')), raw_bytes=raw,
                                        recommendations=findings)

    claimed = [fact.get('value') for fact in document.get('facts') or []]
    assert claimed == ['short.csv'], (
        f'the step claimed {claimed}. An over-long value is skipped by the orchestrator with a '
        f'warning nobody here will ever read, so writing it buys the run nothing and hides the loss'
    )
    assert not findings, 'the orchestrator would decline to record: ' + '; '.join(findings)
    note = (document.get('summary') or {}).get('facts_not_claimed')
    assert note, (
        f'one input could not be claimed as a fact and the summary says nothing about it: '
        f'{document.get("summary")!r}. Silence here is indistinguishable from a run that had '
        f'nothing else to report'
    )


@conforms_today
@reference_quality(
    'The 10,000-entry cap is the orchestrator\'s (external/contract.py MAX_RESULT_FACTS) and it drops '
    'the tail of a longer list silently. A node that keeps appending past it produces a document whose '
    'own contents disagree with what was recorded, and no error anywhere. Applying the cap in the '
    'producer is what turns that into a countable, reported outcome.'
)
def test_the_fact_cap_keeps_the_earliest_entries_and_says_what_it_dropped():
    """The one check here that reads ``node.py`` in this process instead of running it.

    Setup:    the module's own fact recorder, called one past its cap.
    Action:   claim 10,001 facts.
    Validate: 10,000 are kept, they are the FIRST 10,000, and the note the report carries
              names how many went past the cap.

    **Why this one is not black-box, and what that costs.** Every other test of the node
    judges a real container from the outside, and this file would rather keep it that way.
    The cap cannot be reached that way at a price worth paying: it needs a job with more
    than ten thousand inputs, which is twenty thousand round trips through the store and
    several minutes added to a four-minute suite, to observe one comparison. So the
    comparison is observed directly, and the honest statement of what that leaves out is
    that it proves the rule, not that the running container reaches it — the container is
    covered for everything it can be driven to do.

    The EARLIEST entries, not an arbitrary ten thousand: the orchestrator keeps the first
    of a longer list, so a node that kept a different subset would make a run findable by
    ids the platform threw away and unfindable by the ones it kept.
    """
    node = _the_node_module()
    node.FACTS.clear()
    node.FACTS_UNCLAIMED.update(unusable=0, bad_key=0, over_cap=0)

    for index in range(node.MAX_FACTS + 1):
        node.claim('input', f'file-{index}.csv')

    assert len(node.FACTS) == node.MAX_FACTS
    assert node.FACTS[0]['value'] == 'file-0.csv'
    assert node.FACTS[-1]['value'] == f'file-{node.MAX_FACTS - 1}.csv'
    assert node.FACTS_UNCLAIMED['over_cap'] == 1
    note = node._unclaimed_facts_note()
    assert note and str(node.MAX_FACTS) in note, note
    node.FACTS.clear()
    node.FACTS_UNCLAIMED.update(unusable=0, bad_key=0, over_cap=0)


@conforms_today
@reference_quality(
    'The recorder\'s docstring says it applies the orchestrator\'s rules, and a KEY is one of them '
    '(pipelines/facts.py KEY_RE). A key is chosen in the source rather than taken from the data, so a '
    'bad one is not one lost fact — it is every fact of that kind, on every run, missing from the '
    'search with nothing anywhere saying why. The value rules are checked here too because the '
    'orchestrator puts lone surrogates in the same class as control characters: half a UTF-16 '
    'surrogate pair has no UTF-8 encoding, so the row cannot be stored at all.'
)
def test_a_key_or_value_the_orchestrator_would_refuse_is_not_claimed_here_either():
    """The producer applies the orchestrator's rules to both halves of a fact, not one.

    Setup:    the module's own fact recorder, read in this process for the reason the cap
              test above gives — neither a bad key nor a surrogate-bearing name can be
              produced by driving the container, because the key is a literal in the source
              and the harness cannot put unpaired surrogates into a real filename.
    Action:   claim four facts: a good one, one under a key the grammar refuses, one under
              a key with a trailing newline, and one whose value carries half a surrogate
              pair.
    Validate: only the good one is kept; the two key refusals and the value refusal land in
              their own counters; and the report's sentence names both kinds, since a
              counter nobody reads is not a report.

    The two key cases are one rule and two failures of it. ``Input`` is the ordinary
    mistake — a capital letter, which the grammar has never allowed. The trailing newline is
    the one that decides how the grammar is ANCHORED: written with ``$`` it would be
    accepted here, stored with the newline attached, and findable by nothing.
    """
    node = _the_node_module()
    node.FACTS.clear()
    node.FACTS_UNCLAIMED.update(unusable=0, bad_key=0, over_cap=0)
    try:
        node.claim('input', 'rows.csv')
        node.claim('Input', 'rows.csv')
        node.claim('input\n', 'rows.csv')
        node.claim('input', 'photo_\udcff.png')

        assert [fact['value'] for fact in node.FACTS] == ['rows.csv'], node.FACTS
        assert node.FACTS_UNCLAIMED['bad_key'] == 2, node.FACTS_UNCLAIMED
        assert node.FACTS_UNCLAIMED['unusable'] == 1, node.FACTS_UNCLAIMED
        note = node._unclaimed_facts_note()
        assert note and 'key' in note and 'value' in note, note
    finally:
        node.FACTS.clear()
        node.FACTS_UNCLAIMED.update(unusable=0, bad_key=0, over_cap=0)


@conforms_today
@reference_quality(
    'A log line that names the wrong cause is worse than no line: it is read as a finding. The three '
    'ways out of the 1 MiB ceiling are tried in order — drop the params echo, then shorten the facts, '
    'then fail — and only the step that was actually taken may be reported. A document that is still '
    'too large with no facts in it at all was made too large by the echo, and saying "carried too many '
    'facts" there sends its author to look at a list that is empty.'
)
def test_a_document_the_params_made_too_large_does_not_blame_the_facts(caplog):
    """The report of a size failure has to name the thing that caused it.

    Setup:    params whose KEY NAMES alone run past a megabyte, and no facts at all — the
              shape a job with no inputs and a large configuration produces. Read in this
              process for the reason the cap test above gives: the check happens before the
              document is written, so no store and no container are involved in reaching it.
    Action:   compose the report document.
    Validate: it refuses to write one, exactly as before; it says the params were too large
              to echo; and it says NOTHING about facts, because there were none to drop.
    """
    node = _the_node_module()
    node.FACTS.clear()
    node.FACTS_UNCLAIMED.update(unusable=0, bad_key=0, over_cap=0)
    params = {f'{"k" * 20_000}-{index}': 'v' for index in range(70)}

    with caplog.at_level(logging.WARNING, logger='hello-node'):
        with pytest.raises(node.StepError):
            node._write_result(None, {'attempt': 1, 'params': params}, '/nonexistent')

    said = [record.getMessage() for record in caplog.records if record.name == 'hello-node']
    assert len(said) == 1 and 'params are too large' in said[0], said
    assert not any('facts' in line for line in said), (
        f'the document was made too large by the params echo and the step blamed the facts: {said}. '
        f'There were none — a reader following that line looks at an empty list and learns nothing'
    )


@conforms_today
@reference_quality(
    'The same rule as the test above, in the direction it was missing. The size branch fires on the '
    'DOCUMENT, and the params echo is only one of the things that can fill it: a job with tens of '
    'thousands of inputs reaches the ceiling on its facts alone, with params empty. "params are too '
    'large" is then a finding about a configuration nobody wrote, and whoever reads it goes to look at '
    'the pipeline instead of at the list that is actually the cause.'
)
def test_a_document_the_facts_made_too_large_does_not_blame_the_params(caplog, monkeypatch, tmp_path):
    """A line about size may not name a cause this run does not have.

    Setup:    no params at all and ten thousand facts at full length — the shape a job with
              tens of thousands of inputs produces. Read in this process for the reason the
              cap test above gives, with the upload stubbed out: the subject is what the
              step SAYS while composing the document, before a byte leaves.
    Action:   compose the report document.
    Validate: the first line states the size and does NOT claim the params were too large
              to echo, because there were none; the second names the facts it had to drop,
              which is the step that was actually taken.
    """
    node = _the_node_module()
    node.FACTS.clear()
    node.FACTS_UNCLAIMED.update(unusable=0, bad_key=0, over_cap=0)
    monkeypatch.setattr(node, 'publish', lambda *args, **kwargs: None)
    node.FACTS.extend({'key': 'input', 'value': f'{index:04d}-{"n" * 500}.csv'} for index in range(node.MAX_FACTS))
    try:
        with caplog.at_level(logging.WARNING, logger='hello-node'):
            node._write_result(None, {'attempt': 1, 'params': {}}, str(tmp_path))

        said = [record.getMessage() for record in caplog.records if record.name == 'hello-node']
        assert said and 'params are too large' not in said[0], (
            f'the facts filled the document and the step blamed the params: {said}. There were none — '
            f'a reader following that line goes to the pipeline\'s configuration for a cause that is '
            f'not there'
        )
        assert 'over its 1 MiB ceiling' in said[0], said
        assert any('carried too many facts' in line for line in said), said
    finally:
        node.FACTS.clear()
        node.FACTS_UNCLAIMED.update(unusable=0, bad_key=0, over_cap=0)
        node.RESULT_UPLOADED = False


def _the_node_module():
    """``node.py`` itself, imported. Used by exactly one test — see its docstring."""
    import importlib

    return importlib.import_module('node')


@conforms_today
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

    **The oracle is EVERY accepted upload of that name, not the one that survived it.**
    The rule bounds the write, so each write answers for itself, and the store's ledger
    keeps all of them. Asking instead what is readable at the end — which is what an object
    store serves, and what ``body_of`` deliberately returns — would let a step transfer a
    three-megabyte document, overwrite it with a small one, and pass: a ceiling a producer
    may step over and then tidy up after is not a ceiling. Those bytes really were sent,
    really were accepted, and anything reading between the two writes really would have
    found them.

    **Two conforming answers, neither of them required here.** Failing loudly rather than
    writing the document is what the rule cited above describes wanting — *"bounding the
    write makes a producer fail loudly at the point of the mistake"* — so the exit code is
    not asserted. And ``result.json`` is optional: the contract describes the document, it
    does not oblige anybody to produce one, so a run that writes none skips rather than
    passes or fails. An earlier version of this test required exit 0 and then required the
    document to exist, which would have turned either correct fix red.

    The step copies ``params`` into its summary without looking at their size, so a
    manifest the orchestrator was happy to write produces a result document the contract's
    own reader refuses. Worth knowing what this costs today, because it changed: collection
    now reads this document for its ``facts`` list, under the same 1 MiB bound, so an
    oversized one loses the run its search facts — it is read as carrying none. It does not
    fail the run, and nothing reads ``metrics`` or ``summary`` even now. The ceiling bounds
    writes as well as reads, so this is still a document the step is not allowed to
    produce; what has changed is that stepping over it has a price attached at last.
    """
    job = make_job(inputs=[sample_input], params={'payload': 'x' * BULKY_PARAM_BYTES})
    job.run()

    sizes = [upload.size for upload in job.endpoint.uploads if upload.relpath == contract.RESULT_FILENAME]
    if not sizes:
        pytest.skip(
            'the step wrote no result.json for this job. Nothing requires one, and a ceiling on a '
            'document constrains the document that gets written — there is nothing here to be over it'
        )
    oversized = [size for size in sizes if size > contract.MAX_RESULT_BYTES]
    assert not oversized, (
        f'the step uploaded {len(sizes)} result.json ({sizes} bytes), of which {oversized} are over the '
        f'{contract.MAX_RESULT_BYTES}-byte ceiling — the contract\'s own reader is forbidden to read '
        f'them, and the producer is the side that was supposed to find that out. A later, smaller write '
        f'of the same name does not unsend the ones before it'
    )


@conforms_today
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
