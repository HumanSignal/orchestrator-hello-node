"""Re-run every mutation `CONFORMANCE-BASELINE.md` claims, against the suite as it stands.

For each: patch the file, run the named test(s), record the outcome and the assertion
message, restore. A claim survives only if the named test FAILS under its mutation, and
the script exits non-zero if any does not.

**This exists because the document once claimed three guards that were not in the suite.**
The mutations behind them had really been run — and then an edit that replaced a slice of
the test file between two anchors deleted the tests, leaving the claims behind. Nothing
noticed, because a claim about a test is prose and prose is not executable. This makes it
executable: `python verify_mutations.py` from the repository root, and every row of every
mutation table in the baseline has to be a line in `MUTATIONS` below.

Two rules learned from its own first run:

* **Patch the path the behaviour would really take.** The "correct the receipt with a
  second document" mutation was first written against the success path, where in that
  scenario the first write has already timed out — so the mutation never executed and
  reported a green that said nothing about the guard.
* **A green here is a finding**, not a nuisance: either the guard is gone, the mutation is
  misplaced, or the claim was never true. All three have happened.
"""
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent
PYTEST = [str(REPO / '.venv-harness/bin/python'), '-m', 'pytest', '-q', '--no-header', '-p', 'no:cacheprovider']

MUTATIONS = [
    ('handler never installed', 'node.py',
     "    signal.signal(signal.SIGTERM, _on_stop)\n",
     "    pass  # MUTATION\n",
     'leaves_a_receipt or noticed_without_waiting'),

    ('shutdown() put back to close()', 'node.py',
     "            transport.shutdown(socket.SHUT_RDWR)",
     "            transport.close()  # MUTATION",
     'noticed_without_waiting'),

    ('the marker claims a file never written', 'node.py',
     "    sources = creds.get().get('inputs') or []\n    taken: set = set()",
     "    sources = creds.get().get('inputs') or []\n    record('outputs/ghost.csv', '0' * 64, 1, OUTPUT_PORT)  # MUTATION\n    taken: set = set()",
     'claims_no_object'),

    ('no marker on the stop path', 'node.py',
     "    global _ABANDONABLE\n    _ABANDONABLE = False\n",
     "    global _ABANDONABLE\n    _ABANDONABLE = False\n    if status == 'cancelled':\n        return  # MUTATION\n",
     'leaves_a_receipt'),

    ('the ledger forgets a will_close connection', 'node.py',
     "    def stop_now(self) -> None:",
     "    def close(self):  # MUTATION\n        if self in _IN_FLIGHT:\n            _IN_FLIGHT.remove(self)\n        super().close()\n\n    def stop_now(self) -> None:",
     'body_is_still_arriving'),

    ('one elapsed deadline replaced by per-address spend', 'node.py',
     "        self._create_connection = lambda address, timeout, source=None: _connect_within(\n            address, self._deadline, source\n        )",
     "        self._create_connection = lambda address, timeout, source=None: socket.create_connection(\n            address, CONNECT_DEADLINE_S, source)  # MUTATION",
     'every_address_a_name_has'),

    ('the name lookup left unbounded', 'node.py',
     "    thread = threading.Thread(target=look_up, daemon=True)\n    thread.start()\n    thread.join(max(0.0, deadline - time.monotonic()))",
     "    look_up()  # MUTATION",
     'name_lookup_that_hangs'),

    ('the deadline not recomputed after a proxy CONNECT', 'node.py',
     "        super()._tunnel()\n        deadline = getattr(self, '_deadline', None)",
     "        super()._tunnel()\n        deadline = None  # MUTATION\n        _unused = getattr(self, '_deadline', None)",
     'proxy_does_not_get'),

    # Placed on the path a correction would really take: in this scenario the first write
    # TIMES OUT, so a mutation on the success path alone never executes and reports a
    # green that says nothing. That is what the first run of this verifier did.
    ('the receipt corrected by a second document', 'node.py',
     "                  f'(it may or may not have been stored): {redact(marker_failure)}',\n"
     "                  file=sys.stderr, flush=True)\n            return code",
     "                  f'(it may or may not have been stored): {redact(marker_failure)}',\n"
     "                  file=sys.stderr, flush=True)\n"
     "            if CANCELLED and status != 'cancelled':  # MUTATION\n"
     "                with contextlib.suppress(Exception):\n"
     "                    write_marker(creds, manifest, status='cancelled', exit_code=EXIT_CANCELLED,\n"
     "                                 error='stopped while the receipt was in flight')\n"
     "                return EXIT_CANCELLED\n"
     "            return code",
     'exactly_one_receipt'),

    ('the exit code revised after the document was written', 'node.py',
     "            print(f'hello-node: the work finished but the {status} marker could not be confirmed '\n"
     "                  f'(it may or may not have been stored): {redact(marker_failure)}',\n"
     "                  file=sys.stderr, flush=True)\n            return code",
     "            print(f'hello-node: the work finished but the {status} marker could not be confirmed '\n"
     "                  f'(it may or may not have been stored): {redact(marker_failure)}',\n"
     "                  file=sys.stderr, flush=True)\n            return EXIT_TRANSIENT  # MUTATION",
     'exactly_one_receipt'),

    ("the receipt's elapsed deadline removed", 'node.py',
     "        with _within(RECEIPT_DEADLINE_S, 'the completion marker'):\n            write_object(",
     "        if True:  # MUTATION\n            write_object(",
     'deadline_of_its_own'),

    ('the listener announces on accept()', 'conformance/stalling.py',
     "            with self._lock:\n                self.accepted.append(connection)",
     "            with self._lock:\n                self.accepted.append(connection)\n            self._connected.set()  # MUTATION",
     'stalling_listener'),
]


def run(selector: str) -> tuple[bool, str]:
    done = subprocess.run(PYTEST + ['-k', selector], capture_output=True, text=True, cwd=REPO)
    failed = ' failed' in (done.stdout + done.stderr)
    message = ''
    for line in done.stdout.splitlines():
        if line.startswith('E       AssertionError') or line.startswith('E           AssertionError'):
            message = re.sub(r'\s+', ' ', line.split('AssertionError:', 1)[-1]).strip()[:150]
            break
    return failed, message


def main() -> int:
    target_files = {name for _, name, _, _, _ in MUTATIONS}
    backups = {name: (REPO / name).read_text() for name in target_files}
    results = []
    try:
        for label, filename, old, new, selector in MUTATIONS:
            path = REPO / filename
            source = backups[filename]
            # A target that appears twice is refused rather than resolved to the first
            # one. This exact hazard produced a meaningless green here: the "correct the
            # receipt with a second document" patch matched both the work-failure branch
            # and the receipt branch, landed in the first, and its condition was dead
            # there — so the battery reported a guard as unsupported when the guard was
            # fine and the mutation had gone somewhere harmless.
            found = source.count(old)
            if found != 1:
                problem = 'PATCH TARGET MISSING' if found == 0 else f'PATCH TARGET AMBIGUOUS ({found}x)'
                results.append((label, selector, problem, ''))
                print(f'!! {label}: {problem}', flush=True)
                continue
            path.write_text(source.replace(old, new, 1))
            reds, message = run(selector)
            path.write_text(source)
            results.append((label, selector, 'RED' if reds else 'GREEN — CLAIM UNSUPPORTED', message))
            print(f'{"RED " if reds else "GREEN"}  {label}\n        {message}', flush=True)
    finally:
        for name, text in backups.items():
            (REPO / name).write_text(text)
    unsupported = [row for row in results if row[2] != 'RED']
    print('\n' + '=' * 72)
    print(f'{len(results) - len(unsupported)}/{len(results)} claims verified red against the current suite')
    for label, selector, status, _ in unsupported:
        print(f'  UNSUPPORTED: {label}  ({selector}) -> {status}')
    return 1 if unsupported else 0


if __name__ == '__main__':
    sys.exit(main())
