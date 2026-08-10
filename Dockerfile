# The smallest image that is a real LSPO external step.
#
# Nothing from the orchestrator is installed, and nothing from PyPI either: the contract
# is four JSON documents and a directory, so a step needs a Python interpreter and
# node.py. The empty dependency list is deliberate rather than minimalist — `requests`
# builds a multipart upload body in memory, which contradicts the one recommendation
# that matters most here (stream, because a single object may legally be 1 GiB against a
# 2 GiB container). node.py streams uploads with the standard library instead.
FROM python:3.12-slim

# Non-root: the step reads its credentials file and writes its outputs, and needs no
# more than that. A container that runs as root is a container that can do more damage
# than the job it was given.
#
# The NUMBER matters, not just the fact of being non-root. The agent bind-mounts each
# job's credentials directory mode 0700, owned by the uid the AGENT process runs as, and
# a 0700 directory owned by uid A is unreadable by a process running as uid B. Nothing in
# the platform compares the two or warns; it surfaces as "permission denied" on the
# step's own credentials file and looks like a broken node. 10001 is what the shipped
# agent image uses. An agent started directly on a host instead runs as the invoking
# user, usually 1000 — rebuild with `--build-arg STEP_UID=1000` if that is yours.
ARG STEP_UID=10001
RUN useradd --create-home --uid ${STEP_UID} step

WORKDIR /app
COPY node.py /app/node.py
USER ${STEP_UID}

# Without this a buffered stdout means the step's log lines arrive only when the process
# ends, which is exactly when nobody needs them any more.
ENV PYTHONUNBUFFERED=1

# Deliberately NOT set here: any credentials path. The agent sets LSPO_CREDENTIALS_FILE
# for every job and its value is the path the ORCHESTRATOR chose, so it is the only
# authority on where the file is. This image used to bake
# `ENV LSPO_CREDENTIALS=/lspo/creds/creds.json`, which made a node that read the wrong
# variable appear to work — right up until the platform mounted the credentials
# somewhere else, at which point it would have died on a file that was not there.
# node.py still honours LSPO_CREDENTIALS when it is the only one set, for images in the
# field that bake it, and falls back to the contract's default path when neither is.

# Exec form, so this process really is PID 1 and receives SIGTERM directly rather than
# through a shell that would not forward it. Linux gives process 1 no default signal
# handling at all, which is why node.py installs a handler of its own.
ENTRYPOINT ["python", "/app/node.py"]
