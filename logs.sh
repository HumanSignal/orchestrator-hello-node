#!/usr/bin/env bash
# Follow, in real time, what this machine is doing for the orchestrator.
#
#   ./logs.sh agent    the runner agent: claims, heartbeats, container lifecycle
#   ./logs.sh node     every job container the agent starts, from its first line
#   ./logs.sh both     the two interleaved and tagged (the default)
#
# WHY THIS EXISTS, rather than `docker logs <the job's container>`. The agent removes a
# job's container when the job ends, so by the time anybody thinks to look there is
# nothing left to look at — and the runs worth reading are exactly the short ones that
# went wrong. This attaches at the moment a container STARTS, by watching the daemon's
# event stream, so it catches the first line.
#
# The same node output also appears live in the orchestrator's run view, which is the
# only option when the agent runs on somebody else's machine. The two differ in
# completeness and it matters: the platform keeps a tail of the last 1000 lines and
# drops the oldest when a run is chattier than that (docs/PROTOCOL.md section 3.3).
# What this shows is the whole of it.
#
# Nothing here is part of the contract. It is an operator's convenience, it reads and
# never writes, and a node that never runs it is in no way worse off.
set -uo pipefail

AGENT_CONTAINER="${LSPO_AGENT_CONTAINER:-lspo-agent}"
MODE="${1:-both}"

case "$MODE" in
  agent|node|both) ;;
  -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "usage: $0 [agent|node|both]" >&2; exit 2 ;;
esac

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is not on PATH; this script talks to the daemon the agent talks to." >&2
  exit 1
fi

if ! docker inspect "$AGENT_CONTAINER" >/dev/null 2>&1; then
  echo "no container called '$AGENT_CONTAINER' on this machine." >&2
  echo "the agent is started by the docker run line the Connect reply prints; see" >&2
  echo "docs/OPERATIONS.md. If yours is named differently, set LSPO_AGENT_CONTAINER." >&2
  exit 1
fi

if [ "$(docker inspect -f '{{.State.Running}}' "$AGENT_CONTAINER")" != "true" ]; then
  echo "WARNING: '$AGENT_CONTAINER' is not running, so no job will ever start." >&2
  echo "         start it with: docker start $AGENT_CONTAINER" >&2
  echo >&2
fi

# The name the agent enrolled under. The agent puts it on every job container as the
# lspo.agent label (agent/executors/docker_exec.py, labels_for), so filtering on it
# follows only the work THIS agent started, on a machine that runs more than one.
AGENT_NAME="$(docker inspect -f \
  '{{range .Config.Env}}{{if eq (index (split . "=") 0) "LSPO_AGENT_NAME"}}{{index (split . "=") 1}}{{end}}{{end}}' \
  "$AGENT_CONTAINER" 2>/dev/null)"
AGENT_NAME="${AGENT_NAME:-$(hostname)}"

CHILDREN=()
cleanup() { for pid in "${CHILDREN[@]:-}"; do kill "$pid" 2>/dev/null; done; }
trap cleanup EXIT INT TERM

if [ "$MODE" != "node" ]; then
  ( docker logs -f --tail 20 "$AGENT_CONTAINER" 2>&1 | sed -u 's/^/[agent] /' ) &
  CHILDREN+=($!)
fi

if [ "$MODE" != "agent" ]; then
  echo "[watch] following job containers started by agent '$AGENT_NAME'"
  echo "[watch] they are named lspo-<execution>-g<generation>; nothing appears until a run starts"
  (
    docker events --filter 'type=container' --filter 'event=start' \
                  --filter "label=lspo.agent=$AGENT_NAME" \
                  --format '{{.Actor.Attributes.name}}' 2>/dev/null |
    while read -r container; do
      [ -n "$container" ] || continue
      echo "[watch] job container started: $container"
      ( docker logs -f "$container" 2>&1 | sed -u "s|^|[$container] |"
        echo "[watch] $container finished" ) &
    done
  ) &
  CHILDREN+=($!)
fi

wait
