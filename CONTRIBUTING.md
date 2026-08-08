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

   Note the variable name, because the two are not the same one. The platform sets
   `LSPO_CREDENTIALS_FILE` and that is what your node should read; the `node.py` in this
   repository still reads `LSPO_CREDENTIALS`, and survives only because the `Dockerfile`
   hardcodes that name. Outside the image it dies on a traceback, so to run *this* file
   locally set both names, as
   [docs/CONFORMANCE.md](docs/CONFORMANCE.md#level-1-run-it-with-a-hand-written-envelope)
   does — and set only the correct one in your own. Why it matters is the first row of
   [docs/AUTHORING.md](docs/AUTHORING.md#known-gaps-in-nodepy); the run that measured it is
   the first contract defect in
   [CONFORMANCE-BASELINE.md](CONFORMANCE-BASELINE.md#1-it-reads-a-credentials-variable-that-nothing-sets).

5. **Rebuild, push, take the new digest, register a new revision.** A node's identity is
   its digest; changing the code means registering the new one, which is also what makes
   an old run reproducible.
