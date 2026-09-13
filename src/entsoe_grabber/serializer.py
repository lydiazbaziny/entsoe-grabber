"""Turn one ENTSO-E XML market document into CSV.

A market document holds ``TimeSeries``. A series holds periods, and a period
holds the ``Point`` elements that carry the measurements. Only the document
itself is guaranteed to be there. A series withdrawn before publication has no
period, a period the platform opened but never filled has no point, and some
document types carry no series at all.

The row is therefore not fixed at one level. Each branch produces rows at the
deepest level it reaches. A series that reaches its points gives one row per
point. A series that stops at itself gives one row, with the period and point
columns left empty. Siblings of different depth share the same columns, and no
series is copied onto rows that belong to another series. The result is what a
``LEFT JOIN`` gives for a parent with no children.

An element is a record when it is named ``TimeSeries``, ``Period`` or
``Point``, or when it contains such an element. The second rule matters:
unavailability documents keep their points inside ``Available_Period``, a name
this module does not know, and the containment rule carries the rows through it
anyway. A name that becomes a record this way stays a record for the whole
document. An ``Available_Period`` with no point therefore still gets a row of
its own, next to a sibling that has points, in the same way ``Period`` does. A
record adds a segment to the column name and is recursed into. Every other
element becomes columns where it sits.

Column names are XPath paths relative to the document root, with the namespace
removed: ``TimeSeries/Period/Point/quantity``. The separator is ``/`` and not
``.`` because ENTSO-E already uses the dot inside single element names, as in
``process.processType`` and ``inBiddingZone_Domain.mRID``. With two meanings on
one character the path could not be read back. ``@`` marks an attribute and
``[n]`` numbers repeated siblings, both as XPath writes them. A tag that
repeats under any one element is numbered everywhere else in that document too,
so a field that one series reports twice and another reports once stays in a
single column.

Values are written exactly as the platform sent them. In particular, the time
an interval starts is left as its ``position``. A reader computes it as
``Period/timeInterval/start + (position - 1) * resolution``, and all three are
columns. Computing it here would mean guessing: calendar resolutions such as
P1Y are not fixed spans, and under curve type A03 a position marks a block of
variable size rather than every interval.
"""

import csv
import io
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from xml.etree import ElementTree

# The element names this module treats as records by default. :func:`_record_names`
# adds any other name that a document shows to be a record, such as
# ``Available_Period``.
_RECORD_TAGS = frozenset({"TimeSeries", "Period", "Point"})


def _local_name(tag: str) -> str:
    """Strip the XML namespace from a tag or attribute name."""
    return tag.rpartition("}")[2]


def _on_record_path(
    element: ElementTree.Element,
    record_names: frozenset[str],
) -> set[ElementTree.Element]:
    """Return every element that lies on a path from ``element`` down to a record.

    An element qualifies when its name is in ``record_names``, or when it
    contains an element that qualifies. The result is empty when the subtree
    contains no record.

    :func:`_walk` uses the result as a membership test. A child in the set adds
    a segment to the column name and is recursed into. A child outside the set
    becomes columns.

    A record does not need a record below it. That is what gives a withdrawn
    series a row of its own, instead of columns repeated onto the rows of its
    live siblings, and why the deepest level is decided for each branch
    separately.

    Membership is by identity, because ``Element`` defines no ``__eq__``. Two
    points carrying the same values stay two entries and produce two rows. A
    set rather than a list keeps the per-child test in :func:`_walk` at O(1).
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

    Returns whether ``element`` is itself on such a path, which is how its
    parent learns that it is on one too.

    This is the same test as :func:`_on_record_path`, run over names instead of
    elements. It is kept separate so that the pass does not build a set of
    every element in the document only to read the tags from it and throw it
    away. The names are a few strings, and the set of elements is rebuilt
    against the complete name set immediately afterwards in any case.
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
    """Return the names that count as a record in this document.

    The three names the module knows, plus every name that became a record by
    containing one -- ``Available_Period`` in an unavailability document, which
    the module has never seen before.

    Applying those names to the whole document is what makes the rule
    symmetric. An ``Available_Period`` with no point still gets a row of its
    own, next to a sibling that has points, instead of becoming columns that
    are repeated onto the other period's rows.

    The names come from the structural pass only. An element promoted here does
    not lend its own name to a further round.
    """
    names: set[str] = set()
    _collect_record_names(root, names)
    return _RECORD_TAGS | frozenset(names)


def _repeated_tags(root: ElementTree.Element) -> frozenset[str]:
    """Return every tag name that any one element carries more than once.

    Numbering is decided for the whole document, not for each parent. Whether a
    tag can repeat is a property of the schema -- ``Reason`` is ``0..*``,
    ``mRID`` is not -- and one parent is not enough evidence. A series carrying
    two reasons, next to a series carrying one, says nothing about the field
    itself.

    This runs before the walk, because a name given on the way down cannot be
    corrected by a repeat found later.
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

    A tag in ``repeated_tags`` is numbered -- ``Reason[1]``, ``Reason[2]``.
    Every other tag keeps its plain name. Numbering a tag everywhere it
    appears, and not only where it actually repeats, keeps one field in one
    column: a series with a single reason writes ``Reason[1]/code`` next to a
    series with two, instead of opening a separate ``Reason/code``.

    Every child is counted, including the children on the record path that the
    caller names without a number. That way a child off the path keeps its real
    position among its siblings.

    XML does not allow ``[`` in a name, so a numbered name cannot collide with
    a real element.
    """
    seen: Counter[str] = Counter()
    for child in element:
        name = _local_name(child.tag)
        seen[name] += 1
        yield (f"{name}[{seen[name]}]" if name in repeated_tags else name), child


def _join(path: str, name: str) -> str:
    """Extend ``path`` with ``name``.

    ``path`` is empty only at the document root, which adds no segment of its
    own. The id of a ``GL_MarketDocument`` is the column ``mRID``, not
    ``GL_MarketDocument/mRID`` and not ``/mRID``.
    """
    return f"{path}/{name}" if path else name


def _walk(
    element: ElementTree.Element,
    path: str,
    on_record_path: set[ElementTree.Element],
    repeated_tags: frozenset[str],
) -> Iterator[dict[str, str]]:
    """Yield one row per record below ``element``.

    A subtree that holds no record counts as one record. That lets a single
    recursion cover both cases: an element off the record path yields one set
    of values, which its parent merges in, and an element on the path yields
    one row per record below it, onto which its parent merges its own values.
    Column names are built on the way down, values on the way up.

    ``values`` is what this element contributes to every row below it: its
    text, its attributes, and everything collected from children that lead to
    no record. When no child is left on the record path, those values are the
    row.

    ``path`` is passed down instead of being rebuilt on the way up. Growing a
    prefix costs one concatenation per element, while renaming rows as they
    come back would rewrite every key at every level.
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
            # Named without a number below, not by the number just computed.
            # Numbering these would turn each TimeSeries into columns
            # instead of rows.
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

    A value a row does not carry is written as an empty cell, so the row does
    not shift. That is what keeps records of different shape in one rectangular
    table. With no columns there is nothing to write, so the result is empty
    rather than a single stray newline.
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
    """Serialize one XML market document into a CSV body.

    One document per call, on purpose. A response can carry several -- a ZIP
    archive delivers one per member -- and they do not have to agree on shape.
    One shared header would invent columns for records that never had them.
    Taking one at a time also keeps the rows of a single document in memory.

    Parameters
    ----------
    document
        A well-formed XML document, as returned by
        :meth:`~entsoe_grabber.client.EntsoeClient.get`.

    Returns
    -------
    bytes
        UTF-8 CSV: a header row, then one row per record. A branch that
        reaches its points gives one row per point. A branch that stops at a
        period or at the series gives one row, with the deeper columns empty.
        A document with no series gives one row. Nothing is left out. The
        header is the union of the columns found, in the order they first
        appear, with empty cells where a record had no value.
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
