"""Fixtures, and the two labels that make a green run mean something.

Three things happen here that are worth reading before the tests.

**Every test declares one GROUP and one BASIS**, and collection fails otherwise. The
group says whose behaviour is on trial; the basis says by what authority. A
``basis_contract`` citation must name a file in ``conformance.markers``'s list of
authoritative sources AND quote a sentence out of it — an assertion nobody can trace to a
written rule does not get to call itself conformance, and naming the area a rule lives in
is not the same as citing the rule. ``conformance/citations.py`` owns both checks, and
its module docstring is honest about the one they cannot make: that the quoted sentence
actually SUPPORTS the assertion is a judgement about meaning, not a string search. Run
``--print-labels`` to see the whole table.

**Every ``expected_red_until_fixed`` test is turned into a STRICT expected failure.**
CI is therefore green today, with the gaps visible in the report as ``xfailed``. The
moment somebody fixes one, that test PASSES unexpectedly and strict xfail turns the run
red — which is exactly the signal we want, because it is the prompt to move the test out
of the expected-failure group and into ``conforms_today``. Run with ``--red-for-real``
to see the true pass/fail (that is how ``CONFORMANCE-BASELINE.md`` was measured).

**The image is built once, from this repository's own Dockerfile, and never patched.**
The harness has no way to change what runs — no volume over ``/app/node.py``, no
entrypoint override. If it could, none of its verdicts would mean anything.
"""

from __future__ import annotations

import pathlib
import uuid

import pytest

from conformance import citations, docker
from conformance.job import InputSpec, Job
from conformance.markers import BASES, EXPECTED_RED_REASON, GROUPS

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def pytest_addoption(parser):
    parser.addoption(
        '--red-for-real',
        action='store_true',
        default=False,
        help='do not convert expected_red_until_fixed into xfail; report the true result',
    )
    parser.addoption(
        '--print-labels',
        action='store_true',
        default=False,
        help='print each test\'s group, basis and citation (use with --collect-only -q)',
    )


def _label(item, names):
    """The one marker from ``names`` this test carries, with its argument."""
    found = [(name, item.get_closest_marker(name)) for name in names if item.get_closest_marker(name)]
    return found


def pytest_collection_modifyitems(config, items):
    """Hold every test to one group and one traceable basis, then arm the strict xfails."""
    problems: list[str] = []
    for item in items:
        groups = _label(item, GROUPS)
        bases = _label(item, BASES)
        if len(groups) != 1:
            problems.append(f'{item.nodeid} declares group(s) {[name for name, _ in groups] or "none"}')
            continue
        if len(bases) != 1:
            problems.append(f'{item.nodeid} declares basis {[name for name, _ in bases] or "none"}')
            continue
        basis_name, basis_marker = bases[0]
        citation = (basis_marker.args[0] if basis_marker.args else '').strip()
        if not citation:
            problems.append(f'{item.nodeid} carries {basis_name} with no citation or rationale')
            continue
        if basis_name == 'basis_contract':
            flaw = citations.structural_problem(citation)
            if flaw:
                problems.append(f'{item.nodeid} claims basis_contract but {flaw}. Citation: {citation!r}')
                continue
        if groups[0][0] == 'expected_red_until_fixed' and not config.getoption('--red-for-real'):
            item.add_marker(pytest.mark.xfail(strict=True, reason=EXPECTED_RED_REASON))

    if problems:
        raise pytest.UsageError(
            'every conformance test must declare exactly one group ('
            + ', '.join(GROUPS)
            + ') and exactly one basis ('
            + ', '.join(BASES)
            + ') — a green run proves nothing without knowing what each test held to account, and an '
            'assertion nobody can trace to a written rule is not conformance:\n  ' + '\n  '.join(problems)
        )

    if config.getoption('--print-labels'):
        _print_labels(items)


def _print_labels(items) -> None:
    """Dump the whole label table. This is what the baseline document is built from."""
    print('\n')
    for item in items:
        group = next(name for name in GROUPS if item.get_closest_marker(name))
        basis_name = next(name for name in BASES if item.get_closest_marker(name))
        citation = item.get_closest_marker(basis_name).args[0]
        print(f'{group}\t{basis_name}\t{item.nodeid}\t{" ".join(citation.split())}')


@pytest.fixture(scope='session')
def docker_available():
    try:
        docker.require_docker()
    except docker.DockerUnavailable as exc:
        pytest.skip(f'{exc} — this harness runs the real image or nothing; a faked container proves nothing')


@pytest.fixture(scope='session')
def image(docker_available) -> str:
    """The node's real image, built from the repository's own Dockerfile."""
    return docker.build_image(REPO_ROOT)


@pytest.fixture
def workdir(tmp_path) -> pathlib.Path:
    path = tmp_path / f'job-{uuid.uuid4().hex[:8]}'
    path.mkdir(parents=True)
    return path


@pytest.fixture
def make_job(image, workdir):
    """Build a job, set it up, and guarantee its container and store are torn down."""
    created: list[Job] = []

    def factory(**kwargs) -> Job:
        job = Job(image=image, workdir=workdir / f'j{len(created)}', **kwargs)
        created.append(job)
        return job.setup()

    yield factory
    for job in created:
        try:
            job.teardown()
        except Exception:  # teardown must never mask the real failure
            pass


@pytest.fixture
def sample_input() -> InputSpec:
    return InputSpec(relpath='data.csv', data=b'id,value\n1,alpha\n2,beta\n')
