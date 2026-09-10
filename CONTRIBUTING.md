# Making this your own node

Read [docs/AUTHORING.md](docs/AUTHORING.md) first: it has the recipe, a correct skeleton
and a checklist. This page is the two-minute version.

1. **Edit `node.py`.** The parts that matter are the work itself and the marker written at
   the end. Everything else is plumbing.
2. **Keep the marker last.** If you restructure the writes, preserve that ordering: the
   orchestrator treats the marker's existence as proof that everything it names is already
   readable.
3. **Hash what you wrote, not what you meant to write.** Collection re-reads every object
   and refuses the whole run on any disagreement.
4. **Test locally before involving the orchestrator.** Point `LSPO_CREDENTIALS_FILE` at a
   JSON file describing local paths:

   ```json
   {"schema_version": 1, "scheme": "local",
    "credentials_file": "/tmp/job/creds.json",
    "expires_at": "2099-01-01T00:00:00+00:00",
    "manifest_path": "/tmp/job/invocation.json",
    "inputs": [{"port": "input", "name": "sample.csv", "relpath": "sample.csv",
                "sha256": "<the real digest>", "size": 12,
                "local_path": "/tmp/job/in/sample.csv"}],
    "staging": {"mode": "local_path", "path": "/tmp/job/out"}}
   ```

   Then run the program and inspect `/tmp/job/out`. The full worked example, including the
   job description that goes with this file, is in
   [docs/CONFORMANCE.md](docs/CONFORMANCE.md#level-1-run-it-with-a-hand-written-envelope).

   Note the variable name, because two of them exist and the platform sets only one. It
   sets `LSPO_CREDENTIALS_FILE`, and its value is the path the orchestrator chose, so that
   is the name your node should read and the only one you need to set — here and in your
   own node. `LSPO_CREDENTIALS` is a name older images from this repository baked in
   themselves; `node.py` honours it only when it is the only one present, so that those
   images keep working, and the `Dockerfile` no longer bakes it. Reading the name nothing
   sets is still worth understanding even though this file no longer does it: such a node
   works for exactly as long as its own image hardcodes the path, then dies on a file that
   is not there the day the platform mounts the credentials somewhere else. That is the
   first defect in
   [CONFORMANCE-BASELINE.md](CONFORMANCE-BASELINE.md#1-it-reads-a-credentials-variable-that-nothing-sets),
   which measured what it cost; what `node.py` does today is described in
   [docs/AUTHORING.md](docs/AUTHORING.md#nodepy-and-this-skeleton).

5. **Run the existing harness against your image.** Follow
   [conformance/README.md](conformance/README.md). Read each failing test's label:
   copying every input is reference behavior, and a transforming node may differ legally.
6. **Publish a new image revision.** For customer-run mode, rebuild, push and register
   the digest. For hosted mode, request a rebuild of the approved repository and wait
   for a successful revision. Both paths are in [docs/OPERATIONS.md](docs/OPERATIONS.md).
   Existing runs retain the revision they were launched with.
