"""One synthetic job: the credential envelope, the manifest, and the container run.

This is the harness's model of what the orchestrator does to launch one attempt. It
mirrors ``runners/credentials.py`` (the envelope), ``external/contract.py`` (the
manifest) and ``agent/runner.py`` (the nine injected variables) closely enough that a
node which satisfies this one satisfies those — and every place where it deliberately
differs is called out in a comment, because an undocumented difference between the
harness and production is a test that proves the wrong thing.

The credentials file is written into a per-job DIRECTORY on the host and that directory
is what gets bind-mounted, read-only. Rotation replaces the file the way the agent does:
a new file beside it, then ``os.replace``. A reader inside the container sees either the
whole old document or the whole new one, never half of either — and it sees the new one
at all only because the directory, not the file, is the mount.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from conformance import contract, docker
from conformance.fakes3 import Blob, Endpoint

#: The bucket and layout the orchestrator would use. Only the shape matters here: the
#: staging prefix must end with ``attempts/<n>/gen-<g>``, which is the structural fence
#: that keeps a superseded runner out of the live attempt's directory.
BUCKET = 'humansignal-orchestrator-conformance'


@dataclass
class InputSpec:
    """One pinned input, with every knob a defect test needs to turn.

    ``data`` is what the job PINS. ``served`` is what the store actually hands over —
    different only when a test is proving that the node checks what it read against
    what was pinned for it.

    **A pin override lands in BOTH documents.** In production the manifest's input
    objects and the credential envelope's entries are copied from one validated list, so
    they cannot disagree; a harness that overrode the pin in only one of them would be
    handing the node a contradictory pair of platform documents and calling the node's
    reaction a defect. ``sha256`` and ``size`` therefore change what the JOB claims,
    everywhere it claims it, and ``served`` changes what the store hands back — which is
    the real-world shape of "the object under that key is not the object we hashed".

    ``drop_sha256`` is the one exception and cannot be otherwise: the manifest's schema
    requires a digest on every object, so an unpinned input is expressible only in the
    envelope. The test that uses it says so.
    """

    relpath: str
    data: bytes = b''
    port: str = 'input'
    served: bytes | None = None
    synthetic_size: int | None = None
    sha256: str | None = None  #: override the pin, in the manifest AND the envelope
    size: int | None = None  #: override the pinned size, in the manifest AND the envelope
    drop_sha256: bool = False  #: envelope only — the manifest cannot express it
    extra: dict = field(default_factory=dict)

    def blob(self) -> Blob:
        if self.synthetic_size is not None:
            return Blob.synthetic(self.synthetic_size)
        return Blob.of(self.served if self.served is not None else self.data)

    def pin(self) -> Blob:
        """What the JOB claims about this object, which may not be what is served."""
        if self.synthetic_size is not None:
            base = Blob.synthetic(self.synthetic_size)
        else:
            base = Blob.of(self.data)
        return Blob(
            sha256=self.sha256 if self.sha256 is not None else base.sha256,
            size=self.size if self.size is not None else base.size,
            data=None,
        )

    @property
    def store_key(self) -> str:
        return f'{self.port}/{self.relpath}'


class Job:
    """A synthetic external job, from credential envelope to collected container."""

    def __init__(
        self,
        *,
        image: str,
        workdir: Path,
        inputs: list[InputSpec] | None = None,
        params: dict | None = None,
        execution_id: int = 4242,
        attempt: int = 1,
        generation: int = 1,
        job_id: int = 77,
        credentials_file: str = contract.DEFAULT_CREDENTIALS_FILE,
        envelope_extra: dict | None = None,
        manifest_extra: dict | None = None,
        input_extra: dict | None = None,
        max_object_bytes: int = 256 * 1024 * 1024,
        creds_ttl_s: float | None = None,
    ):
        self.image = image
        self.workdir = workdir
        self.inputs = inputs or []
        self.params = params if params is not None else {'greeting': 'hello'}
        self.execution_id = execution_id
        self.attempt = attempt
        self.generation = generation
        self.job_id = job_id
        self.credentials_file = credentials_file
        self.envelope_extra = envelope_extra or {}
        self.manifest_extra = manifest_extra or {}
        self.input_extra = input_extra or {}
        # How long the FIRST envelope is good for. ``None`` — no expiry — is what every
        # test that is not about credential lifetime wants: production credentials do
        # expire, but fifteen minutes is longer than any test here runs, so an unbounded
        # one models the ordinary case exactly and only the expiry tests shorten it.
        self.creds_ttl_s = creds_ttl_s
        self.idempotency_key = f'exec-{execution_id}-att-{attempt}'

        self.key_prefix = (
            f'conformance/pipelines/9/executions/{execution_id}/attempts/{attempt}/gen-{generation}/'
        )
        self.staging_prefix = f's3://{BUCKET}/{self.key_prefix}'
        self.invocation_uri = f'{self.staging_prefix}{contract.MANIFEST_FILENAME}'

        self.creds_dir = workdir / 'creds'
        self.creds_dir.mkdir(parents=True, exist_ok=True)
        # 0755/0644, not the agent's 0700/0600: this image runs as uid 10001 and the
        # harness runs as whoever invoked pytest, so the agent's own permissions would
        # make the file unreadable here for a reason that has nothing to do with the
        # node. See CONFORMANCE-BASELINE.md — whether the real agent hits the same wall
        # is a platform question, not a node one.
        os.chmod(self.creds_dir, 0o755)

        self.endpoint = Endpoint(max_object_bytes=max_object_bytes)
        self.endpoint.on_rotate = self._write_creds
        self.container: docker.Container | None = None

    # ------------------------------------------------------------------ documents

    def manifest(self) -> dict:
        """``invocation.json`` — the job description, exactly as the launcher writes it."""
        ports: dict[str, dict] = {}
        for spec in self.inputs:
            pin = spec.pin()
            port = ports.setdefault(
                spec.port,
                {
                    'name': spec.port,
                    'payload_kind': 'conformance_bytes',
                    'layout': 'file',
                    'cardinality': 'many',
                    'objects': [],
                },
            )
            port['objects'].append(
                {
                    'uri': f's3://{BUCKET}/inputs/{spec.store_key}',
                    'sha256': pin.sha256,
                    'size': pin.size,
                    'relpath': spec.relpath,
                }
            )
        document = {
            'schema_version': 1,
            'execution_id': self.execution_id,
            'pipeline_id': 9,
            'deployment_id': 3,
            'revision_id': 5,
            'image_digest': self.image,
            'params': self.params,
            'env_names': [],
            'inputs': list(ports.values()),
            'staging_prefix': self.staging_prefix,
            'credentials_file': self.credentials_file,
            'timeout_seconds': 600,
            'idempotency_key': self.idempotency_key,
            'attempt': self.attempt,
            'generation': self.generation,
        }
        document.update(self.manifest_extra)
        return document

    def envelope(self, token: str) -> dict:
        """The credential document for one generation, shaped like ``_s3_envelope``."""
        host = docker.HOST_ALIAS
        entries = []
        for spec in self.inputs:
            pin = spec.pin()
            entry = {
                'port': spec.port,
                'relpath': spec.relpath,
                'sha256': pin.sha256,
                'size': pin.size,
                'name': spec.relpath,
                'get_url': self.endpoint.get_url(host, spec.store_key, token),
            }
            if spec.drop_sha256:
                entry.pop('sha256')
            entry.update(self.input_extra)
            entry.update(spec.extra)
            entries.append(entry)
        # The envelope states the expiry of the credential it carries, which is what
        # ``runners/credentials.py`` puts there ("'expires_at': budget.signed_until") and
        # what a step is entitled to act on. When the endpoint issued a credential with no
        # expiry we still declare the production default, because an envelope with no
        # stated expiry is a shape the orchestrator never writes.
        stated = self.endpoint.stated_expiry(token) or (datetime.now(timezone.utc) + timedelta(minutes=15))
        document = {
            'schema_version': 1,
            'scheme': 's3',
            'credentials_file': self.credentials_file,
            'expires_at': stated.isoformat(),
            'manifest_get': self.endpoint.get_url(host, contract.MANIFEST_FILENAME, token),
            'inputs': entries,
            'staging': {
                'mode': 'presigned_post',
                'post': self.endpoint.post_policy(host, token, self.key_prefix),
            },
        }
        document.update(self.envelope_extra)
        return document

    def injected_env(self) -> dict[str, str]:
        """The nine variables the agent injects. Nine, no more, no fewer."""
        return {
            'LSPO_JOB_ID': str(self.job_id),
            'LSPO_EXECUTION_ID': str(self.execution_id),
            'LSPO_ATTEMPT': str(self.attempt),
            'LSPO_GENERATION': str(self.generation),
            'LSPO_INVOCATION_URI': self.invocation_uri,
            'LSPO_STAGING_PREFIX': self.staging_prefix,
            'LSPO_CREDENTIALS_FILE': self.credentials_file,
            'LSPO_IDEMPOTENCY_KEY': self.idempotency_key,
            'LSPO_CONTRACT_VERSION': '1',
        }

    # ------------------------------------------------------------------- lifecycle

    def _creds_path(self) -> Path:
        return self.creds_dir / Path(self.credentials_file).name

    def _write_creds(self, token: str) -> None:
        """Atomically publish the envelope for ``token``, the way ``agent/creds.py`` does.

        Temp file in the SAME directory, fsync, then ``os.replace``. A truncate-and-write
        would give a reader an empty or half-written document at exactly the wrong moment;
        the container sees the swap only because the directory is what is mounted.
        """
        data = json.dumps(self.envelope(token), sort_keys=True, indent=2).encode('utf-8')
        handle, tmp = tempfile.mkstemp(dir=str(self.creds_dir), prefix='.creds-', suffix='.json')
        try:
            os.fchmod(handle, 0o644)
            with os.fdopen(handle, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self._creds_path())
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def setup(self) -> 'Job':
        """Start the store, publish the inputs and the manifest, mint credentials."""
        self.endpoint.start()
        for spec in self.inputs:
            self.endpoint.blobs[spec.store_key] = spec.blob()
        manifest_bytes = json.dumps(self.manifest(), sort_keys=True, indent=2).encode('utf-8')
        contract.validate_manifest(json.loads(manifest_bytes), raw_bytes=manifest_bytes)
        self.endpoint.blobs[contract.MANIFEST_FILENAME] = Blob.of(manifest_bytes)
        self._write_creds(self.endpoint.mint(self.creds_ttl_s))
        return self

    def write_envelope_file(self, filename: str, token: str) -> Path:
        """Publish an envelope for ``token`` under another name in the same directory.

        Used to stage a SECOND, stale credentials file, so a test can point the legacy
        variable at one document and the current variable at another and see which the
        node believes.
        """
        path = self.creds_dir / filename
        path.write_bytes(json.dumps(self.envelope(token), sort_keys=True, indent=2).encode('utf-8'))
        os.chmod(path, 0o644)
        return path

    def start(
        self,
        *,
        env: dict[str, str] | None = None,
        unset_env: tuple[str, ...] = (),
        omit_env: tuple[str, ...] = (),
        **kwargs,
    ):
        """Launch the container. ``env`` overrides/extends the nine injected variables.

        ``omit_env`` drops one of the nine before launch AND strips any value the image
        baked in for it — the only honest way to ask "what does this node do when the
        variable is genuinely absent?".

        A job with a credential TTL gets a FRESH envelope here, immediately before the
        container starts, so that the whole lifetime belongs to the step instead of being
        spent on ``docker run`` and an image start. Otherwise a six-second credential
        could be half gone before the step's first line executes, and a test about
        expiry would sometimes be a test about how busy the machine was.
        """
        if self.creds_ttl_s is not None:
            self._write_creds(self.endpoint.mint(self.creds_ttl_s))
        environment = self.injected_env()
        environment.update(env or {})
        for name in omit_env:
            environment.pop(name, None)
        unset_env = tuple(dict.fromkeys(unset_env + omit_env))
        self.container = docker.start(
            self.image,
            name=f'lspo-conformance-{uuid.uuid4().hex[:10]}',
            env=environment,
            unset_env=unset_env,
            creds_dir=self.creds_dir,
            creds_mount=str(Path(self.credentials_file).parent),
            **kwargs,
        )
        return self.container

    def run(self, *, timeout: float = 180.0, **kwargs) -> docker.RunResult:
        """Launch and wait for a real exit. The ordinary case.

        ``Container.wait`` raises :class:`docker.ContainerDidNotExit` rather than
        returning on a timeout, so a step that hangs is reported as a hang. It used to
        return ``None`` here and be ignored, and the result then carried docker's
        ``State.ExitCode`` — which is ``0`` for a container that is still running. Every
        test that checks only the exit code and the logs would have passed.
        """
        container = self.start(**kwargs)
        container.wait(timeout=timeout)
        return container.collect()

    def teardown(self) -> None:
        if self.container is not None:
            self.container.remove()
        self.endpoint.stop()

    def __enter__(self) -> 'Job':
        return self.setup()

    def __exit__(self, *exc) -> None:
        self.teardown()

    # ----------------------------------------------------------------- inspection

    def uploaded_json(self, relpath: str) -> dict:
        return json.loads(self.endpoint.body_of(relpath).decode('utf-8'))

    def marker(self) -> dict:
        """The completion marker as uploaded, validated against the contract."""
        raw = self.endpoint.body_of(contract.MARKER_FILENAME)
        return contract.validate_marker(json.loads(raw.decode('utf-8')), raw_bytes=raw)

    def raw_marker(self) -> tuple[dict, bytes]:
        """The marker WITHOUT validating it — for tests whose subject is the validation."""
        raw = self.endpoint.body_of(contract.MARKER_FILENAME)
        return json.loads(raw.decode('utf-8')), raw
