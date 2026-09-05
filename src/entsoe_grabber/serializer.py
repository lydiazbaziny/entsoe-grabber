"""Turn one ENTSO-E XML market document into CSV.

A market document nests ``TimeSeries``, each holding periods, each holding the
``Point``s that carry the measurements -- but only the document itself is
promised. A series withdrawn before publication carries no period, a period the
platform opened but never filled carries no point, and a document type that
publishes attributes rather than a curve carries no series at all.

So the row is not fixed at one level. Every branch produces rows at the deepest
level it reaches on its own: a series that reaches its points contributes one
row per point, a series that stops at itself contributes a single row with the
period and point columns left empty. Siblings of differing depth land in the
same columns rather than in column families of their own, and no series is
repeated onto rows belonging to another -- the flattening a ``LEFT JOIN`` gives
a parent with no children.

An element is a record when it is named ``TimeSeries``, ``Period`` or
``Point``, or when it contains one. The second half matters: unavailability
documents nest their points inside ``Available_Period``, a name this module
does not know, and containment carries the rows through it anyway. A name that
earns record standing that way keeps it for the whole document, so an
``Available_Period`` holding no point still takes a row beside a sibling that
holds some, the way ``Period`` does on its name alone. A record contributes a
segment to the column name and is recursed into; everything else is off the
record path and collapses into columns wherever it sits.

Columns are named by their namespace-stripped XPath relative to the document
root -- ``TimeSeries/Period/Point/quantity``. The separator is ``/`` rather than
``.`` because ENTSO-E already spends the dot inside single element names, as in
``process.processType`` and ``inBiddingZone_Domain.mRID``; with both meanings on
one character a path could not be read back. ``@`` introduces an attribute and
``[n]`` numbers siblings, following the same XPath conventions. A tag that
repeats under any one element is numbered wherever else it appears in that
document too, so a field one series reports twice and the next reports once
stays in a single column.

Values are written exactly as the platform sent them. In particular the instant
a ``Point`` covers is left as its ``position``, which a reader resolves as
``Period/timeInterval/start + (position - 1) * resolution`` -- all three are
columns. Computing it here would mean guessing at calendar resolutions such as
P1Y, and at curve type A03, whose positions mark variable-sized blocks rather
than every interval.
"""

import csv
import io
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from xml.etree import ElementTree

# The element names this module knows without looking. :func:`_record_names`
# adds the ones a document proves to be records, such as ``Available_Period``.
_RECORD_TAGS = frozenset({"TimeSeries", "Period", "Point"})


def _local_name(tag: str) -> str:
    """Strip the XML namespace from a tag or attribute name."""
    return tag.rpartition("}")[2]


def _on_record_path(
    element: ElementTree.Element,
    record_names: frozenset[str],
) -> set[ElementTree.Element]:
    """Return every element lying on a path from ``element`` down to a record.

    An element qualifies when its name is in ``record_names`` or when it holds
    something that does; the result is empty when the subtree holds no record.
    :func:`_walk` uses it as a membership test: a child in the set becomes a
    segment of the column name and is recursed into, a child outside it
    collapses into columns.

    A record needs no record beneath it, which is what gives a withdrawn series
    a row of its own instead of columns repeated onto its live siblings' rows,
    and why the deepest level reached is settled per branch.

    Membership is by identity, since ``Element`` defines no ``__eq__``: two
    points carrying the same values stay two entries and produce two rows. A
    set, not a list, so the walk's per-child test stays O(1).
    """
    on_path: set[ElementTree.Element] = set()
    for child in element:
        on_path |= _on_record_path(child, record_names)
    if on_path or _local_name(element.tag) in record_names:
        on_path.add(element)
    return on_path


def _collect_record_names(
    element: ElementTree.Element,
    names: set[str],
) -> bool:
    """Add every name on a path from ``element`` down to a record to ``names``.

    Returns whether ``element`` is itself on such a path, which is how a parent
    learns it is on one too.

    This is :func:`_on_record_path`'s test run for names rather than elements.
    Keeping it separate is what stops the pass from building a set of every
    element in the document only to read the tags off it and drop it again --
    the names are a handful of strings, and the set of elements is rebuilt
    against the full name set immediately afterwards regardless.
    """
    on_path = False
    for child in element:
        # Every child is visited. `any` would stop at the first one that
        # qualifies and never see the names under its siblings.
        if _collect_record_names(child, names):
            on_path = True
    name = _local_name(element.tag)
    if on_path or name in _RECORD_TAGS:
        names.add(name)
        return True
    return False


def _record_names(root: ElementTree.Element) -> frozenset[str]:
    """Return the names that stand for a record in this document.

    The three the module knows, plus every name that earned the standing by
    holding a record -- ``Available_Period`` in an unavailability document,
    which the module has never heard of.

    Applying those names to the whole document is what makes the rule
    symmetric: an ``Available_Period`` holding no point still takes a row
    beside a sibling that holds some, instead of collapsing into columns that
    ride on the other period's rows.

    The names come from the structural pass alone, so an element promoted here
    does not lend its own name to a further round.
    """
    names: set[str] = set()
    _collect_record_names(root, names)
    return _RECORD_TAGS | frozenset(names)


def _repeated_tags(root: ElementTree.Element) -> frozenset[str]:
    """Return every tag name carried more than once by any one element.

    Numbering is settled for the document rather than parent by parent.
    Whether a tag repeats is a property of the schema -- ``Reason`` is
    ``0..*``, ``mRID`` is not -- and one parent is too small a sample to read it
    from: a series carrying two reasons beside one carrying a single reason says
    nothing about the field.

    It runs before the walk, since a name given on the way down cannot be
    revised by a repeat found later.
    """
    repeated: set[str] = set()
    for element in root.iter():
        counts = Counter(_local_name(child.tag) for child in element)
        repeated.update(name for name, count in counts.items() if count > 1)
    return frozenset(repeated)


def _numbered(
    element: ElementTree.Element,
    repeated_tags: frozenset[str],
) -> Iterator[tuple[str, ElementTree.Element]]:
    """Pair each child of ``element`` with its column name.

    A tag in ``repeated_tags`` is numbered -- ``Reason[1]``, ``Reason[2]`` --
    and every other keeps its bare name. Numbering a tag everywhere it appears,
    not only where it repeats, keeps one field in one column: a series with a
    single reason writes ``Reason[1]/code`` beside a series with two, rather
    than opening a ``Reason/code`` of its own.

    Every child is counted, including the ones on the record path that the
    caller names bare, so an off-path child keeps its true position among its
    siblings.

    XML forbids ``[`` in a name, so a numbered name cannot collide with a real
    element.
    """
    seen: Counter[str] = Counter()
    for child in element:
        name = _local_name(child.tag)
        seen[name] += 1
        yield (f"{name}[{seen[name]}]" if name in repeated_tags else name), child


def _join(path: str, name: str) -> str:
    """Extend ``path`` with ``name``.

    Empty only at the document root, which contributes no segment of its own:
    the id of a ``GL_MarketDocument`` is the column ``mRID``, not
    ``GL_MarketDocument/mRID`` and not ``/mRID``.
    """
    return f"{path}/{name}" if path else name


def _walk(
    element: ElementTree.Element,
    path: str,
    on_record_path: set[ElementTree.Element],
    repeated_tags: frozenset[str],
) -> Iterator[dict[str, str]]:
    """Yield one row per record beneath ``element``.

    A subtree holding no record counts as one record, which lets a single
    recursion cover both cases: an off-path element yields one set of values
    that its parent folds in, an on-path element yields a row per record below
    and its parent merges its own values onto each. Column names are built on
    the way down, values on the way up.

    ``values`` is what this element contributes to every row beneath it: its
    text, its attributes, and everything gathered from children leading to no
    record. With no children left on the record path, those values are the row.

    ``path`` is threaded down rather than rebuilt on the way up: growing a
    prefix costs one concatenation per element, while renaming rows as they
    surface would rewrite every key at every level.
    """
    values: dict[str, str] = {}
    text = (element.text or "").strip()
    if text:
        values[path] = text
    for attribute, value in element.attrib.items():
        values[f"{path}@{_local_name(attribute)}"] = value

    children_on_path: list[ElementTree.Element] = []
    for name, child in _numbered(element, repeated_tags):
        if child in on_record_path:
            # Named bare below, not by the number just computed: numbering
            # these would give each TimeSeries columns instead of rows.
            children_on_path.append(child)
            continue
        for row in _walk(child, _join(path, name), on_record_path, repeated_tags):
            values.update(row)

    if not children_on_path:
        yield values
        return
    for child in children_on_path:
        for row in _walk(
            child, _join(path, _local_name(child.tag)), on_record_path, repeated_tags
        ):
            yield values | row


def _render_csv(columns: Iterable[str], rows: Iterable[Mapping[str, str]]) -> bytes:
    """Render ``rows`` as a UTF-8 CSV body with ``columns`` as its header.

    A value a row does not carry is written as an empty cell rather than
    shifting the row along, which is what keeps records of differing shape in
    one rectangular table. No columns means nothing to write at all, so the
    result is empty rather than a stray newline.
    """
    fieldnames = list(columns)
    if not fieldnames:
        return b""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, restval="")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def to_csv(document: bytes) -> bytes:
    """Serialise one XML market document into a CSV body.

    One document per call, deliberately. A response can carry several -- a ZIP
    archive delivers one per member -- and they need not agree on shape, so one
    header for all of them would invent columns for records that never had
    them. It also keeps one document's rows in memory at a time.

    Parameters
    ----------
    document
        A well-formed XML document, as returned by
        :meth:`~entsoe_grabber.client.EntsoeClient.get`.

    Returns
    -------
    bytes
        UTF-8 CSV: a header row, then one row per record. A branch reaching
        its points gives a row per point; one stopping at a period or at the
        series gives a single row with the deeper columns empty; a document
        with no series is one collapsed row. Nothing goes unrecorded. The
        header is the union of the columns found, in first-seen order, with
        empty cells where a record said nothing.
    """
    root = ElementTree.fromstring(document)
    on_record_path = _on_record_path(root, _record_names(root))
    repeated_tags = _repeated_tags(root)

    columns: dict[str, None] = {}
    rows: list[dict[str, str]] = []
    for row in _walk(root, "", on_record_path, repeated_tags):
        columns.update(dict.fromkeys(row))
        rows.append(row)

    return _render_csv(columns, rows)
