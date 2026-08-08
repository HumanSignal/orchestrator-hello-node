"""Fixtures and the rule that makes a green run mean something.

Two things happen here that are worth reading before the tests.

**Every ``expected_red_until_fixed`` test is turned into a STRICT expected failure.**
CI is therefore green today, with the defects visible in the report as ``xfailed``. The
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

from conformance import docker
from conformance.job import InputSpec, Job
from conformance.markers import EXPECTED_RED_REASON, GROUPS

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def pytest_addoption(parser):
    parser.addoption(
        '--red-for-real',
        action='store_true',
        default=False,
        help='do not convert expected_red_until_fixed into xfail; report the true result',
    )


def pytest_collection_modifyitems(config, items):
    """Attach strict xfail to the expected-red group, and hold every test to one group."""
    unlabelled = []
    for item in items:
        groups = [name for name in GROUPS if item.get_closest_marker(name)]
        if len(groups) != 1:
            unlabelled.append(f'{item.nodeid} declares {groups or "no group"}')
            continue
        if groups[0] == 'expected_red_until_fixed' and not config.getoption('--red-for-real'):
            item.add_marker(pytest.mark.xfail(strict=True, reason=EXPECTED_RED_REASON))
    if unlabelled:
        raise pytest.UsageError(
            'every conformance test must declare exactly one of '
            + ', '.join(GROUPS)
            + ' — a green run proves nothing without knowing what each test was holding to account:\n  '
            + '\n  '.join(unlabelled)
        )


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
