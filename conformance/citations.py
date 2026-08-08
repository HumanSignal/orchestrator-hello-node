"""Checking that a ``basis_contract`` citation quotes something that is really there.

The label ``basis_contract`` claims a test restates a rule the platform has written
down. Until this module existed, the only thing enforced was that the citation mentioned
one of the authoritative FILES — which proves nothing at all about the rule. A citation
could name ``external/contract.py`` and then paraphrase, or quote a sentence that has
since been reworded, or quote nothing whatsoever, and collection was perfectly happy.

Two checks live here, and they are deliberately different in kind.

**Structural — always on, no sources needed.** A ``basis_contract`` citation must name an
authoritative file AND contain at least one quoted fragment long enough to be a rule
rather than an identifier. This is what stops "the rule is about this area" from being
dressed up as a citation, and it costs nothing to run.

**Verbatim — opt-in, needs the orchestrator's sources.** Every quoted fragment must
actually occur in one of the files the citation names. Set ``LSPO_ORCHESTRATOR_SRC`` to a
checkout of the orchestrator (optionally with ``LSPO_ORCHESTRATOR_REF`` to read a git ref
instead of the working tree) and the harness checks all of them. Without it the check
skips and says so — this repository is standalone and does not vendor the platform, and a
check that silently passed because it had nothing to read would be worse than none.

**What neither check can do, and it is the important half.** They establish that the
sentence exists, not that it SUPPORTS the assertion. "The step writes the marker strictly
last" is genuinely in ``external/contract.py``; whether it also makes a marker's ABSENCE
a violation is a question about meaning, and no string search answers it. That judgement
stays with whoever reviews the test, and it is exactly where this harness has been wrong
before — every over-claim corrected so far cited a real file and quoted a real sentence.

Matching rules, and each is there for a measured reason:

* whitespace is collapsed, because a rule wrapped across two source lines and re-wrapped
  inside a citation is the same rule;
* comparison is case-insensitive, because a sentence quoted mid-paragraph often starts
  with the other case than it does in the source;
* reStructuredText markup is stripped from the SOURCE (``os.replace``d becomes
  ``os.replaced``, ``:class:`Foo``` becomes ``Foo``), because a citation quotes the rule
  as a human reads it, not as Sphinx marks it up;
* a fragment may contain ``…`` for elided text; each side of it is then required
  separately, in order.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path
from typing import Callable

from conformance.markers import AUTHORITATIVE_SOURCES

#: The decorator whose argument is a contract citation.
CITING_DECORATOR = 'traces_to'

#: Shortest quoted fragment that counts as quoting a RULE. Below this a citation is
#: quoting a name — ``"objects"``, ``"_classify"`` — which is a pointer, not a rule.
MIN_FRAGMENT_CHARS = 12

#: A quoted span inside a citation. Citations use double quotes for quoted source text
#: and apostrophes inside them, so a double-quoted span is unambiguous.
_QUOTED = re.compile(r'"([^"]+)"')

#: Sphinx roles (``:class:`X```, ``:func:`X``…) and inline literals in the SOURCE. Both
#: are removed before matching so a quotation can read like prose.
_ROLE = re.compile(r':[a-z]+:`~?([^`]*)`')
_LITERAL = re.compile(r'`+|\*\*')

#: A leading comment marker. A rule stated in a ``#`` block reads as prose to a human and
#: as a column of hashes to a string search, so the markers come off first.
_COMMENT_LEAD = re.compile(r'^[ \t]*#[ \t]?', re.MULTILINE)

#: The seam between two adjacent Python string literals — ``… entry '`` newline ``f'(two
#: entries …``. Most of the rules worth citing are error messages a source file builds
#: out of several literals, and the seam is invisible to the human reading the message
#: and fatal to a search for it. Removed from the SOURCE only.
#:
#: **The newline is required, and that is not cosmetic.** Without it this also matched an
#: empty string literal, so ``str(payload.get('phase') or '')`` had its ``''`` deleted and
#: a citation quoting that line correctly was reported as wrong. A concatenation seam in
#: these sources always crosses a line; an empty string never does.
#: The two literals need not use the same quote character — a message often switches to
#: double quotes for the line that contains an apostrophe — so this does not backreference.
_STRING_SEAM = re.compile(r"""['"][ \t]*\n\s*[fFrRbBuU]{0,2}['"]""")

#: Every character used to open or close a quotation, folded onto one. A citation is
#: itself delimited by double quotes, so a rule containing them gets re-spelled with
#: apostrophes on the way in; that is a transcription choice, not a difference in the rule.
_QUOTE_CHARS = str.maketrans({'"': "'", '“': "'", '”': "'", '‘': "'", '’': "'"})

#: The environment variables that turn the verbatim check on.
SRC_ENV = 'LSPO_ORCHESTRATOR_SRC'
REF_ENV = 'LSPO_ORCHESTRATOR_REF'


def sources_named(citation: str) -> list[str]:
    """Which authoritative files this citation claims to be quoting."""
    return [source for source in AUTHORITATIVE_SOURCES if source in citation]


def fragments(citation: str) -> list[str]:
    """The quoted rule-length fragments in a citation, with elisions split apart."""
    found: list[str] = []
    for span in _QUOTED.findall(citation):
        for piece in span.split('…'):
            text = piece.strip().strip('.,;:—- ')
            if len(text) >= MIN_FRAGMENT_CHARS:
                found.append(text)
    return found


def structural_problem(citation: str) -> str | None:
    """Why this citation cannot support a ``basis_contract`` claim — or ``None``.

    Runs at collection time on every test, with no access to the platform's sources.
    """
    if not sources_named(citation):
        return (
            f'it names none of the authoritative sources ({", ".join(AUTHORITATIVE_SOURCES)}); '
            f'a rule nobody can look up is not a rule this suite may enforce'
        )
    if not fragments(citation):
        return (
            f'it quotes nothing at least {MIN_FRAGMENT_CHARS} characters long. Naming the area a rule '
            f'lives in is not citing the rule — put the sentence itself in double quotes, so a reader '
            f'can decide whether it really makes the observed behaviour a violation'
        )
    return None


def citations_in(paths) -> list[tuple[str, str]]:
    """Every ``traces_to`` citation written in these files, as ``(where, citation)``.

    Read out of the SOURCE with ``ast`` rather than off the collected tests on purpose.
    A version of this check that walked ``session.items`` measured only the tests the
    current run happened to select, so running it on its own — the natural thing to do —
    checked exactly one citation and passed. Reading the files means the answer does not
    depend on how pytest was invoked.

    A citation that is not a plain string literal is returned as an empty citation
    against its location, so it shows up as a problem rather than being skipped: hiding
    the text behind a name would put it out of reach of this check.
    """
    found: list[tuple[str, str]] = []
    for path in sorted(Path(p) for p in paths):
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _called_name(node.func) != CITING_DECORATOR:
                continue
            where = f'{path.name}:{node.lineno}'
            try:
                text = ast.literal_eval(node.args[0]) if node.args else ''
            except (ValueError, SyntaxError, IndexError):
                text = ''
            found.append((where, text if isinstance(text, str) else ''))
    return found


def _called_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ''


def normalise(text: str) -> str:
    """One comparable form: markup off, quotes folded, whitespace gone, lower-cased.

    Whitespace is REMOVED rather than collapsed. The three things that legitimately
    differ between a rule in a source file and the same rule inside a citation are line
    wrapping, indentation and code formatting, and all three are whitespace; a rule that
    matches once whitespace is ignored is present, and the alternative — failing a
    correct citation because the source put a space after an opening brace — trains
    people to loosen the check rather than fix the quotation.
    """
    plain = _COMMENT_LEAD.sub('', text)
    plain = _LITERAL.sub('', _ROLE.sub(r'\1', plain))
    return ''.join(plain.translate(_QUOTE_CHARS).split()).lower()


def verbatim_problems(citation: str, read_source: Callable[[str], str]) -> list[str]:
    """Every quoted fragment that is not in any file this citation names.

    Args:
        citation: The citation text.
        read_source: Maps an authoritative path to its content. May raise; a source that
            cannot be read is reported as such rather than passing quietly.
    """
    bodies: dict[str, str] = {}
    for source in sources_named(citation):
        try:
            bodies[source] = normalise(_STRING_SEAM.sub('', read_source(source)))
        except Exception as exc:  # a source we cannot read proves nothing either way
            return [f'{source} could not be read: {exc}']
    problems: list[str] = []
    for fragment in fragments(citation):
        wanted = normalise(fragment)
        if not any(wanted in body for body in bodies.values()):
            problems.append(f'{fragment!r} appears in none of {sorted(bodies)}')
    return problems


def source_reader() -> Callable[[str], str] | None:
    """How to read the platform's sources, or ``None`` if this machine has none.

    Honours ``LSPO_ORCHESTRATOR_REF``: with it set, files are read from that git ref
    rather than from the working tree, so the check measures the citations against the
    commit the node is actually deployed against rather than against somebody's
    uncommitted edits.
    """
    root = os.environ.get(SRC_ENV)
    if not root:
        return None
    ref = os.environ.get(REF_ENV)
    if ref:
        def read(path: str) -> str:
            done = subprocess.run(
                ['git', '-C', root, 'show', f'{ref}:{path}'], capture_output=True, text=True
            )
            if done.returncode != 0:
                raise FileNotFoundError(f'git show {ref}:{path} failed: {done.stderr.strip()}')
            return done.stdout

        return read

    def read_file(path: str) -> str:
        return (Path(root) / path).read_text(encoding='utf-8')

    return read_file
