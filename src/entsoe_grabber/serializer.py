"""Turn ENTSO-E XML market documents into CSV.

Every transparency document nests the same way: a market document holds
``TimeSeries``, each holding ``Period``s, each holding the ``Point``s that carry
the measurements. This module writes one row per ``Point`` and turns everything
on the path down to it into columns, so a document type it has never seen still
produces a usable table, and a field the platform adds appears on its own.

``Point`` is the only element name the module knows. An element that is a
``Point``, or contains one, lies on the record path: it contributes a segment to
the column name and is recursed into. Everything else is off that path, and
collapses into columns wherever it sits.

Columns are named by their namespace-stripped XPath relative to the document
root -- ``TimeSeries/Period/Point/quantity``. The separator is ``/`` rather than
``.`` because ENTSO-E already spends the dot inside single element names, as in
``process.processType`` and ``inBiddingZone_Domain.mRID``; with both meanings on
one character a path could not be read back. ``@`` introduces an attribute and
``[n]`` numbers repeated siblings, following the same XPath conventions.

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

from entsoe_grabber.client import XmlDocuments

# The one element name this module knows. Every document family in the ENTSO-E
# schema guide that carries measurements ends in Point.
_RECORD_TAG = "Point"


def _local_name(tag: str) -> str:
    """Strip the XML namespace from a tag or attribute name."""
    return tag.rpartition("}")[2]


def _record_path(element: ElementTree.Element) -> set[ElementTree.Element]:
    """Return the elements lying on a path from ``element`` down to a ``Point``.

    That is every element of this subtree that is, or contains, a ``Point``,
    ``element`` included when it qualifies -- and the empty set when the subtree
    holds no ``Point`` at all. :func:`_walk` reads it as a membership test: a
    child on the path becomes a segment of the column name and is recursed into,
    a child off it is collapsed into columns.

    Identity, not equality, decides membership: ``Element`` defines neither
    ``__eq__`` nor ``__hash__``, so two points that happen to carry the same
    position and quantity stay two entries and produce two rows. A set rather
    than a list because the walk tests every child against it, which on a list
    would make the walk quadratic.

    One post-order pass. A non-empty result from a child is itself the answer to
    "does this element hold a ``Point``", which is why nothing else is tracked.
    """
    on_path: set[ElementTree.Element] = set()
    for child in element:
        on_path |= _record_path(child)
    if on_path or _local_name(element.tag) == _RECORD_TAG:
        on_path.add(element)
    return on_path


def _numbered(
    element: ElementTree.Element,
) -> Iterator[tuple[str, ElementTree.Element]]:
    """Pair each child of ``element`` with its column name.

    A tag carried by one child keeps its bare name, the normal case; repeats
    become ``Reason[1]`` and ``Reason[2]`` rather than overwriting each other.

    Every child is counted, including the ones on the record path that the
    caller names bare. A withdrawn ``TimeSeries`` carries no ``Period``, so it
    sits off the path beside a live sibling that is on it; counting only the
    off-path children would hand it the bare name the live one's rows use.

    XML forbids ``[`` in a name, so a numbered name cannot collide with a real
    element.
    """
    names = [_local_name(child.tag) for child in element]
    counts = Counter(names)
    seen: Counter[str] = Counter()
    for name, child in zip(names, element, strict=True):
        seen[name] += 1
        yield (name if counts[name] == 1 else f"{name}[{seen[name]}]"), child


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
    record_path: set[ElementTree.Element],
) -> Iterator[dict[str, str]]:
    """Yield one row per ``Point`` beneath ``element``.

    A subtree holding no ``Point`` is itself one record. That is what lets a
    single recursion do all the work: an element off the record path yields
    exactly one set of values, which the parent folds into its own, while an
    element on the path yields a row per ``Point`` below and the parent merges
    its values onto each. Both cases are the same descent, building column names
    on the way down and values on the way up.

    ``own_values`` is what this element contributes to every row beneath it: its
    own text, its attributes, and everything gathered from the children that
    lead nowhere. An element with no children left on the record path is the
    record itself, and its own values are the row.

    ``path`` is threaded down rather than reconstructed on the way up because
    naming a column is a prefix operation: growing the prefix as the walk
    descends costs one concatenation per element, while renaming rows as they
    surface would rewrite every key at every level.
    """
    own_values: dict[str, str] = {}
    text = (element.text or "").strip()
    if text:
        own_values[path] = text
    for attribute, value in element.attrib.items():
        own_values[f"{path}@{_local_name(attribute)}"] = value

    on_path: list[ElementTree.Element] = []
    for name, child in _numbered(element):
        if child in record_path:
            # Named bare below, not by the number just computed: numbering these
            # would give each TimeSeries its own columns instead of its own rows.
            on_path.append(child)
            continue
        for row in _walk(child, _join(path, name), record_path):
            own_values.update(row)

    if not on_path:
        yield own_values
        return
    for child in on_path:
        for row in _walk(child, _join(path, _local_name(child.tag)), record_path):
            yield own_values | row


def _render_csv(columns: Iterable[str], rows: Iterable[Mapping[str, str]]) -> bytes:
    """Render ``rows`` as a UTF-8 CSV body with ``columns`` as its header.

    A row saying nothing about a column leaves that cell empty rather than
    shifting the row, which is what keeps documents of differing shape in one
    rectangular table. With no columns there is nothing to name, so the body is
    empty rather than a lone blank line.
    """
    fieldnames = list(columns)
    if not fieldnames:
        return b""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, restval="")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def to_csv(documents: XmlDocuments) -> bytes:
    """Serialise ``documents`` into a single CSV body.

    Parameters
    ----------
    documents
        Well-formed XML documents, as returned by
        :meth:`~entsoe_grabber.client.EntsoeClient.get`.

    Returns
    -------
    bytes
        UTF-8 CSV: a header row followed by one row per ``Point``. The header is
        the union of the columns found, in the order they first appeared, so a
        response whose documents differ still produces one rectangular table
        with empty cells where a document said nothing. A document carrying no
        ``Point`` at all -- a cancelled time series, a registry document --
        still contributes one fully collapsed row, so nothing goes unrecorded.
        No documents produces an empty body.

        Rows from several documents -- a ZIP archive delivers one per member --
        follow each other in response order, and the document's own required
        ``mRID`` column says which one a row came from.
    """
    columns: dict[str, None] = {}
    rows: list[dict[str, str]] = []

    for document in documents:
        root = ElementTree.fromstring(document)
        for row in _walk(root, "", _record_path(root)):
            columns.update(dict.fromkeys(row))
            rows.append(row)

    return _render_csv(columns, rows)
