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
# The NUMBER does not matter, and this file used to say the opposite. It instructed you
# to build as uid 10001 because the agent bind-mounted each job's credentials directory
# mode 0700 with the file inside it 0600, owned by the uid the agent ran as — so an image
# declaring any other user could not open its own credentials. That was a real defect and
# it has been fixed on the platform, in both of its halves: the mounted directory is now
# 0711 and the file inside it 0444, re-applied on every write, so any uid can open a path
# it has been told the name of; and the agent is started as the operator's own account
# (`docker run --user "$(id -u):$(id -g)"`) rather than as the uid its own image declares,
# so "the agent is 10001" is not true of a deployed agent either.
#
# So 4242 here is arbitrary, and deliberately NOT the agent image's 10001 — a number this
# file shares with the agent is a number the next reader will assume has to match. Pick
# whatever suits you, or set --build-arg STEP_UID=<n>. The one thing that is still true of
# the mode bits: the credentials directory is traversable but not LISTABLE by you, so open
# the exact path in LSPO_CREDENTIALS_FILE and never enumerate the directory it sits in.
ARG STEP_UID=4242
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
