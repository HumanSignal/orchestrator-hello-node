"""A black-box conformance harness for this node.

Nothing in here is imported by ``node.py`` and nothing in here is installed into the
node's image. The harness builds the image from the repository's own ``Dockerfile``,
runs it as a container the way the orchestrator's agent would, and judges it purely
from the outside: the environment it was given, the HTTP traffic it made, the objects
it uploaded, the order it uploaded them in, and the exit code it died with.

The rule the whole thing exists to enforce: a test here must be red against the node
as it is today, and green only once the node is fixed. See ``conformance/README.md``.
"""
