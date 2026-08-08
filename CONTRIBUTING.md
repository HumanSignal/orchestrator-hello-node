# Making this your own node

1. **Edit `node.py`.** The only parts that matter are `process()` — what your step
   actually does — and the marker it writes at the end. Everything else is plumbing you
   can keep as it is.
2. **Keep the marker last.** If you restructure the writes, preserve that ordering: the
   orchestrator treats the marker's existence as proof that everything it names is
   already readable.
3. **Hash what you wrote, not what you meant to write.** Collection re-reads every object
   and refuses the whole run on any disagreement.
4. **Test locally before involving the orchestrator.** Point `LSPO_CREDENTIALS` at a JSON
   file describing local paths — note that this is the variable *this file currently
   reads*, not the one the orchestrator sets (`LSPO_CREDENTIALS_FILE`); see the first
   contract defect in `CONFORMANCE-BASELINE.md`:

   ```json
   {"schema_version": 1, "scheme": "local",
    "manifest_path": "/tmp/job/invocation.json",
    "inputs": [{"name": "sample.csv", "relpath": "sample.csv", "local_path": "/tmp/job/in/sample.csv"}],
    "staging": {"mode": "local_path", "path": "/tmp/job/out"}}
   ```

   Then `python node.py` and inspect `/tmp/job/out` — the same shapes the orchestrator
   produces, with no network and no orchestrator involved.
5. **Rebuild, push, take the new digest, register a new revision.** A node's identity is
   its digest; changing the code means registering the new one, which is also what makes
   an old run reproducible.
