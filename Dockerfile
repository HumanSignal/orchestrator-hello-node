# The smallest image that is a real LSPO external step.
#
# Nothing from the orchestrator is installed: the contract is four JSON documents and a
# directory, so a step needs a Python interpreter, an HTTP client, and node.py.
FROM python:3.12-slim

# Non-root: the step reads its credentials file and writes its outputs, and needs no
# more than that. A container that runs as root is a container that can do more damage
# than the job it was given.
RUN useradd --create-home --uid 10001 step
RUN pip install --no-cache-dir requests==2.32.3

WORKDIR /app
COPY node.py /app/node.py
USER step

# The agent mounts the credentials file and sets LSPO_CREDENTIALS to its path.
ENV LSPO_CREDENTIALS=/lspo/creds/creds.json
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "/app/node.py"]
