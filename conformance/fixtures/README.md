# Golden wire-format documents

Copied **byte for byte** from the orchestrator's `external/fixtures/` at commit
`d8a78355`. They are the frozen version-1 wire format: the orchestrator's own golden
tests compare its parser's output against these files, so any change to the shape of a
contract document turns those tests red before it can reach anybody.

They are here for one reason. `conformance/contract.py` re-implements the contract
instead of importing it — deliberately, because a harness that shares code with the
system it judges inherits that system's bugs and stops being able to see them. The cost
of that choice is that "my re-implementation agrees with the real one" is a claim rather
than a measurement. These three documents turn it back into a measurement:
`tests/test_harness_self.py` validates all three, and a re-implementation that drifted
away from the real parser fails on the real parser's own reference documents.

What they exercise that this harness's own traffic does not: a `layout='prefix'` input
port with a real `prefix_digest`, `organization_id`, `upstream_execution_id`, a `null`
`prefix_digest` on a file port, and a marker delivering two different ports.

Do not edit them. If the orchestrator's goldens change, re-copy them and read what
changed — that is the signal, not a merge conflict to resolve.
