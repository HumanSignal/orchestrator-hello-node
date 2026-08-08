"""Running the node's real image, the way the agent runs it — and nothing else.

Everything here goes through the ``docker`` CLI as a subprocess. That is deliberate:
the harness must not share a code path with the thing it is judging, and the CLI is the
one interface that is unarguably outside the node. It also means the harness has no
Python dependency on the docker SDK, so ``pip install -r requirements-dev.txt`` on a
fresh machine is pytest and requests and nothing more.

The options mirror ``agent/executors/docker_exec.py``: a per-job name, no auto-remove
(the exit code is the only signal when a step dies before writing its marker, and an
auto-removed container takes it away), CPU/memory/pid ceilings, ``no-new-privileges``,
and exactly ONE host path bind-mounted read-only — the job's credentials DIRECTORY.
Never the credentials file: the file is atomically replaced while the container runs,
and a bind-mounted file pins the old inode, so the container would never see a rotation.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

#: Docker resolves this to the host's gateway address, which is how a container on the
#: default bridge network reaches a server bound on the host. Requires Docker 20.10+.
HOST_ALIAS = 'host.docker.internal'

#: How long the agent gives a container to shut down after SIGTERM before SIGKILL.
#: ``DockerExecutor.stop`` defaults to exactly this.
STOP_GRACE_SECONDS = 30


class DockerUnavailable(RuntimeError):
    """No usable docker daemon. The harness refuses to run rather than simulate one."""


class ContainerDidNotExit(AssertionError):
    """The container was still running when the harness stopped waiting for it.

    This is an ``AssertionError`` on purpose: it is a test FAILURE, never a quietly
    tolerated condition. Docker reports ``State.ExitCode`` as ``0`` for a container that
    has not exited yet, so a harness that shrugged at a timeout would read a hung
    container as a step that finished successfully — and every test that inspects only
    the exit code and the logs would pass vacuously. That is the single most dangerous
    bug this harness can have, because it turns the whole baseline into fiction in the
    direction of "the node is fine".
    """


class DockerCommandFailed(AssertionError):
    """A ``docker`` command the harness depends on did not succeed.

    Also an ``AssertionError``, and for the same reason as :class:`ContainerDidNotExit`:
    an infrastructure fault must never be readable as an ordinary result. ``docker stop``
    failing outright looks, to any test that ignores it, exactly like a step that shut
    down instantly and quietly — short elapsed time, no marker, no further work. That is
    a false GREEN on the cancellation tests, which is the direction that matters.
    """


class ContainerVanished(DockerCommandFailed):
    """The container the harness started is no longer known to the daemon.

    Only ever raised on a CONFIRMED "no such container" answer. The harness removes a
    container exactly once, after it has collected it, so absence beforehand means
    something outside these tests removed it — and everything the run would have said
    about the node (its exit code, its logs) is gone with it. Reporting that as
    ``exit_code=None`` would let it masquerade as an ordinary expected-red failure.
    """


#: How the docker CLI says "I have never heard of that container". Matched rather than
#: assumed from the exit status, because every other failure — daemon down, permission
#: denied, an unparseable answer — is a fault the harness must NOT treat as an outcome.
_NO_SUCH_CONTAINER = re.compile(r'no such (?:object|container)', re.IGNORECASE)


def require_docker() -> None:
    """Fail loudly and early if there is no daemon. A faked container proves nothing."""
    if shutil.which('docker') is None:
        raise DockerUnavailable('the docker CLI is not on PATH; this harness runs the real image or nothing')
    probe = subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'], capture_output=True, text=True)
    if probe.returncode != 0:
        raise DockerUnavailable(f'the docker daemon is not reachable: {probe.stderr.strip()}')


def swap_limit_supported() -> bool:
    """Whether this kernel lets docker cap swap as well as RAM.

    It matters for exactly one test. Without swap accounting the daemon ignores
    ``--memory-swap`` and a container that exceeds its RAM ceiling pages out instead of
    being OOM-killed, so "does an oversized input kill this step?" would answer "no" for
    a reason that has nothing to do with the step. Many CI kernels ship without it, and
    a test that quietly changes meaning there is worse than one that says it cannot run.
    """
    out = subprocess.run(['docker', 'info', '--format', '{{json .Warnings}}'], capture_output=True, text=True)
    try:
        warnings = json.loads(out.stdout.strip() or 'null') or []
    except json.JSONDecodeError:
        return False
    return not any('swap limit' in str(warning).lower() for warning in warnings)


def build_image(context: Path, *, dockerfile: str = 'Dockerfile') -> str:
    """Build the repository's own image and return its digest-pinned id.

    The returned reference is a bare ``sha256:<64 hex>`` image id, which is what the
    contract calls a locally-built image: immutable, and resolvable only on this host.
    That is the same reference the orchestrator would be registered with for a
    single-machine deployment.
    """
    require_docker()
    tag = f'orchestrator-hello-node:conformance-{uuid.uuid4().hex[:8]}'
    build = subprocess.run(
        ['docker', 'build', '-f', str(context / dockerfile), '-t', tag, str(context)],
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        raise DockerUnavailable(f'docker build failed:\n{build.stdout}\n{build.stderr}')
    inspect = subprocess.run(['docker', 'image', 'inspect', tag, '--format', '{{.Id}}'], capture_output=True, text=True)
    if inspect.returncode != 0:
        raise DockerUnavailable(f'docker image inspect failed: {inspect.stderr}')
    return inspect.stdout.strip()


#: Docker's own words for "this container has not finished". ``ExitCode`` is meaningless
#: — and usually ``0`` — while the state is one of these.
UNFINISHED_STATES = ('created', 'running', 'restarting', 'paused', 'removing')


@dataclass
class RunResult:
    """Everything the harness is allowed to know about a finished container.

    ``exit_code`` is ``None`` when the container had not exited when this was collected.
    It is never a number in that case: docker would say ``0``, and a zero that means
    "still working" is indistinguishable from a zero that means "finished cleanly".
    """

    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float
    oom_killed: bool = False
    stopped_after_s: float | None = None
    still_running: bool = False

    @property
    def output(self) -> str:
        return self.stdout + self.stderr

    @property
    def lines(self) -> list[str]:
        return [line for line in self.output.splitlines() if line.strip()]


@dataclass
class Container:
    """A running workload. Started detached so a test can signal it mid-flight."""

    name: str
    started_at: float
    _stopped_after: float | None = field(default=None, init=False)

    def wait(self, timeout: float = 180.0) -> int:
        """Block until the container exits and return its exit code.

        Raises:
            ContainerDidNotExit: It was still running when ``timeout`` ran out. Never
                returns in that case — see the exception's own docstring for why a
                tolerated timeout is the most dangerous bug this harness could have.
            ContainerVanished: Something removed the container. Also never returns: the
                only honest thing to say about a container nobody can inspect is that
                its result is unknown, and ``None`` is not that — it is a value tests
                compare against.
        """
        deadline = time.monotonic() + timeout
        while True:
            state = self.inspect()
            if state['Status'] not in UNFINISHED_STATES:
                return state['ExitCode']
            if time.monotonic() >= deadline:
                raise ContainerDidNotExit(
                    f'container {self.name!r} was still {state["Status"]!r} after {timeout:.0f}s. '
                    f'Docker reports ExitCode {state.get("ExitCode")!r} for a container in that state, '
                    f'which is why this is an error rather than a result. Its output so far:\n'
                    f'{self._logs_or_note()[-4000:]}'
                )
            time.sleep(0.1)

    def inspect(self) -> dict:
        """The daemon's own account of this container's state.

        Every failure raises. A previous version mapped EVERY non-zero ``docker inspect``
        onto ``{'Status': 'gone', 'ExitCode': None}``, which meant a daemon that had
        fallen over, a permission problem or an unparseable answer all arrived at the
        tests as "the step produced no exit code" — indistinguishable from an ordinary
        expected-red failure, and green for anything that only asked whether the exit
        code was non-zero.
        """
        out = subprocess.run(
            ['docker', 'inspect', self.name, '--format', '{{json .State}}'], capture_output=True, text=True
        )
        if out.returncode != 0:
            raise self._command_failure('inspect', out)
        try:
            return json.loads(out.stdout)
        except json.JSONDecodeError as exc:
            raise DockerCommandFailed(
                f'docker inspect {self.name!r} answered something that is not JSON ({exc}): {out.stdout!r}'
            ) from exc

    def stop(self, grace: int = STOP_GRACE_SECONDS) -> float:
        """SIGTERM, then SIGKILL after ``grace`` — exactly what the agent's stop does.

        Raises on a failed ``docker stop``. That return code used to be discarded, and
        the cost of discarding it was specific: ``docker stop`` failing immediately
        returns in a fraction of a second, having done nothing, which reads to a
        cancellation test as "the step shut down promptly and did no further work" — the
        exact shape of the behaviour those tests are looking for.
        """
        began = time.monotonic()
        out = subprocess.run(
            ['docker', 'stop', '--time', str(grace), self.name], capture_output=True, text=True
        )
        self._stopped_after = time.monotonic() - began
        if out.returncode != 0:
            raise self._command_failure('stop', out, extra=f'after {self._stopped_after:.1f}s')
        return self._stopped_after

    def logs(self) -> tuple[str, str]:
        """Everything the container wrote. A failed ``docker logs`` raises rather than
        returning empty strings, which several tests would read as "the step said
        nothing"."""
        out = subprocess.run(['docker', 'logs', self.name], capture_output=True, text=True)
        if out.returncode != 0:
            raise self._command_failure('logs', out)
        return out.stdout, out.stderr

    def _logs_or_note(self) -> str:
        """The container's output for an error message, never raising over it.

        Used only while composing a failure that has already been decided; letting a
        second fault in here would replace a precise diagnosis with a vaguer one.
        """
        try:
            stdout, stderr = self.logs()
        except DockerCommandFailed as exc:
            return f'(its output could not be read: {exc})'
        return stdout + stderr

    def _command_failure(self, verb: str, out: subprocess.CompletedProcess, *, extra: str = '') -> DockerCommandFailed:
        message = (out.stderr or out.stdout).strip()
        where = f'docker {verb} {self.name!r}{" " + extra if extra else ""}'
        if _NO_SUCH_CONTAINER.search(message):
            return ContainerVanished(
                f'{where}: the daemon no longer knows this container ({message}). This harness removes a '
                f'container only after collecting it, so its exit code and its logs are gone and nothing '
                f'about the node can be concluded from this run'
            )
        return DockerCommandFailed(f'{where} failed with exit {out.returncode}: {message}')

    def collect(self) -> RunResult:
        """Everything observable about the container, right now.

        A container that has NOT exited yields ``exit_code=None`` and
        ``still_running=True``. Docker's ``State.ExitCode`` is ``0`` for a running
        container, so reporting it verbatim would let a hung step be recorded as a
        successful one — which is what this method used to do.
        """
        state = self.inspect()
        stdout, stderr = self.logs()
        running = state.get('Status') in UNFINISHED_STATES
        return RunResult(
            exit_code=None if running else state.get('ExitCode'),
            stdout=stdout,
            stderr=stderr,
            duration_s=time.monotonic() - self.started_at,
            oom_killed=bool(state.get('OOMKilled')),
            stopped_after_s=self._stopped_after,
            still_running=running,
        )

    def remove(self) -> None:
        subprocess.run(['docker', 'rm', '-f', self.name], capture_output=True, text=True)


def start(
    image: str,
    *,
    name: str,
    env: dict[str, str],
    unset_env: tuple[str, ...] = (),
    creds_dir: Path | None = None,
    creds_mount: str = '/lspo/creds',
    mounts: tuple[tuple[Path, str, str], ...] = (),
    memory: str = '512m',
    cpus: float = 1.0,
    pids_limit: int = 256,
    user: str | None = None,
    entrypoint: str | None = None,
    command: tuple[str, ...] = (),
) -> Container:
    """Start one workload container, detached.

    ``unset_env`` names variables to REMOVE from the container's environment even if the
    image baked them in. ``docker run -e NAME`` with no ``=`` and no value on the host
    does exactly that — it drops the image's own ``ENV`` for that name. It is the only
    black-box way to ask "what does this node do when the variable really is absent?",
    which matters here because this image bakes ``LSPO_CREDENTIALS`` into itself.
    """
    argv = [
        'docker', 'run', '--detach',
        '--name', name,
        '--add-host', f'{HOST_ALIAS}:host-gateway',
        '--security-opt', 'no-new-privileges',
        '--pids-limit', str(pids_limit),
        '--cpus', str(cpus),
        '--memory', memory,
        '--memory-swap', memory,  # a hard ceiling: without this the kernel may use swap
        '--log-driver', 'json-file',
        '--log-opt', 'max-size=10m',
        '--log-opt', 'max-file=3',
    ]
    for key, value in env.items():
        argv += ['--env', f'{key}={value}']
    for key in unset_env:
        argv += ['--env', key]  # no "=", no host value => docker drops the image's own ENV
    if creds_dir is not None:
        argv += ['--volume', f'{creds_dir}:{creds_mount}:ro']
    for host_path, container_path, mode in mounts:
        argv += ['--volume', f'{host_path}:{container_path}:{mode}']
    if user:
        argv += ['--user', user]
    if entrypoint is not None:
        argv += ['--entrypoint', entrypoint]
    argv.append(image)
    argv += list(command)

    scrubbed = {key: value for key, value in os.environ.items() if key not in unset_env}
    out = subprocess.run(argv, capture_output=True, text=True, env=scrubbed)
    if out.returncode != 0:
        raise DockerUnavailable(f'docker run failed: {out.stderr.strip()}')
    return Container(name=name, started_at=time.monotonic())
