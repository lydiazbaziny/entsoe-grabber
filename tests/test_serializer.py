import csv
import io

from entsoe_grabber.serializer import to_csv

NS = "urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0"


def document(body: str) -> bytes:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<GL_MarketDocument xmlns="{NS}">{body}</GL_MarketDocument>'
    ).encode()


HEADER = (
    "<mRID>7b1f9c</mRID>"
    "<revisionNumber>1</revisionNumber>"
    "<type>A71</type>"
    "<process.processType>A01</process.processType>"
    '<sender_MarketParticipant.mRID codingScheme="A01">10X1001A1001A450'
    "</sender_MarketParticipant.mRID>"
    "<createdDateTime>2025-12-31T12:17:33Z</createdDateTime>"
    "<time_Period.timeInterval>"
    "<start>2025-12-31T23:00Z</start><end>2026-01-01T23:00Z</end>"
    "</time_Period.timeInterval>"
)

# 14.1.C day-ahead aggregated generation: one series, two half-hours.
FORECAST = document(
    HEADER + "<TimeSeries>"
    "<mRID>1</mRID>"
    "<businessType>A01</businessType>"
    "<objectAggregation>A01</objectAggregation>"
    '<inBiddingZone_Domain.mRID codingScheme="A01">10YSK-SEPS-----K'
    "</inBiddingZone_Domain.mRID>"
    "<quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name>"
    "<curveType>A01</curveType>"
    "<Period>"
    "<timeInterval><start>2025-12-31T23:00Z</start><end>2026-01-01T00:00Z</end>"
    "</timeInterval>"
    "<resolution>PT30M</resolution>"
    "<Point><position>1</position><quantity>2314</quantity></Point>"
    "<Point><position>2</position><quantity>2280</quantity></Point>"
    "</Period>"
    "</TimeSeries>"
)

# 16.1.A actual generation per unit: a series per generating unit, each naming
# the unit through MktPSRType, which holds no Point and so collapses.
PER_UNIT = document(
    "<mRID>abc</mRID><type>A73</type>"
    "<TimeSeries>"
    "<mRID>1</mRID>"
    "<MktPSRType><psrType>B14</psrType><PowerSystemResources>"
    '<mRID codingScheme="A01">27W-GU-EBO---01</mRID>'
    "<name>Bohunice 1</name>"
    '<nominalP unit="MAW">505</nominalP>'
    "</PowerSystemResources></MktPSRType>"
    "<Period><timeInterval><start>2026-01-01T23:00Z</start>"
    "<end>2026-01-02T00:00Z</end></timeInterval>"
    "<resolution>PT60M</resolution>"
    "<Point><position>1</position><quantity>498</quantity></Point>"
    "</Period>"
    "</TimeSeries>"
    "<TimeSeries>"
    "<mRID>2</mRID>"
    "<MktPSRType><psrType>B19</psrType><PowerSystemResources>"
    '<mRID codingScheme="A01">27W-GU-VET---01</mRID>'
    "<name>Veterny park</name>"
    "</PowerSystemResources></MktPSRType>"
    "<Period><timeInterval><start>2026-01-01T23:00Z</start>"
    "<end>2026-01-02T00:00Z</end></timeInterval>"
    "<resolution>PT60M</resolution>"
    "<Point><position>1</position><quantity>3</quantity>"
    "<secondaryQuantity>1</secondaryQuantity></Point>"
    "</Period>"
    "</TimeSeries>"
)

# A time series withdrawn before publication carries no Period, so no Point.
CANCELLED = document(
    HEADER + "<TimeSeries>"
    "<mRID>1</mRID><businessType>A01</businessType><cancelledTS>A09</cancelledTS>"
    "</TimeSeries>"
)


def read(document: bytes) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(to_csv(document).decode("utf-8")))
    rows = list(reader)
    assert reader.fieldnames is not None
    return list(reader.fieldnames), rows


def columns(document: bytes) -> list[str]:
    return read(document)[0]


def rows(document: bytes) -> list[dict[str, str]]:
    return read(document)[1]


# --- a row is the deepest level its own branch reaches -----------------------


def test_a_document_with_points_gets_a_row_per_point() -> None:
    assert len(rows(FORECAST)) == 2


def test_a_document_that_stops_at_period_gets_a_row_per_period() -> None:
    # Periods the platform opened but published nothing into: no Point exists,
    # so the period is the deepest level and becomes the record.
    empty_periods = document(
        HEADER + "<TimeSeries><mRID>1</mRID>"
        "<Period><timeInterval><start>2025-12-31T23:00Z</start>"
        "<end>2026-01-01T00:00Z</end></timeInterval>"
        "<resolution>PT60M</resolution></Period>"
        "<Period><timeInterval><start>2026-01-01T00:00Z</start>"
        "<end>2026-01-01T01:00Z</end></timeInterval>"
        "<resolution>PT15M</resolution></Period>"
        "</TimeSeries>"
    )

    resolutions = [row["TimeSeries/Period/resolution"] for row in rows(empty_periods)]

    assert resolutions == ["PT60M", "PT15M"]


def test_a_document_that_stops_at_time_series_gets_a_row_per_series() -> None:
    # 16.1.A outage documents and withdrawn series carry no Period at all.
    no_periods = document(
        HEADER + "<TimeSeries><mRID>1</mRID><cancelledTS>A09</cancelledTS></TimeSeries>"
        "<TimeSeries><mRID>2</mRID><cancelledTS>A09</cancelledTS></TimeSeries>"
    )

    ids = [row["TimeSeries/mRID"] for row in rows(no_periods)]

    assert ids == ["1", "2"]


def test_a_document_with_no_series_is_a_single_row() -> None:
    header_only = document(HEADER)

    result = rows(header_only)

    assert len(result) == 1
    assert result[0]["mRID"] == "7b1f9c"


def test_a_branch_that_stops_early_gets_its_own_row() -> None:
    # One series reaches a Point, the other stops at its Period. The shallower
    # one is a record too, so it gets a row of its own with the point columns
    # empty, rather than being repeated onto the deeper series' rows.
    mixed = document(
        HEADER + "<TimeSeries><mRID>live</mRID>"
        "<Period><resolution>PT60M</resolution>"
        "<Point><position>1</position><quantity>111</quantity></Point>"
        "</Period></TimeSeries>"
        "<TimeSeries><mRID>empty</mRID>"
        "<Period><resolution>PT15M</resolution></Period></TimeSeries>"
    )

    deep, shallow = rows(mixed)

    assert deep["TimeSeries/mRID"] == "live"
    assert deep["TimeSeries/Period/Point/quantity"] == "111"
    assert shallow["TimeSeries/mRID"] == "empty"
    assert shallow["TimeSeries/Period/resolution"] == "PT15M"
    assert shallow["TimeSeries/Period/Point/quantity"] == ""


def test_branches_of_differing_depth_share_one_set_of_columns() -> None:
    # Three series, three depths: a curve, a series with nothing below it, and
    # a period that was never filled. Each lands in the same columns as the
    # others, and says nothing under the levels it never reached.
    uneven = document(
        HEADER + "<TimeSeries><mRID>A</mRID>"
        "<Period><resolution>PT60M</resolution>"
        "<Point><position>1</position><quantity>11</quantity></Point>"
        "<Point><position>2</position><quantity>12</quantity></Point>"
        "</Period></TimeSeries>"
        "<TimeSeries><mRID>B</mRID><cancelledTS>A09</cancelledTS></TimeSeries>"
        "<TimeSeries><mRID>C</mRID>"
        "<Period><resolution>PT15M</resolution></Period></TimeSeries>"
    )

    result = rows(uneven)

    assert [row["TimeSeries/mRID"] for row in result] == ["A", "A", "B", "C"]
    assert [row["TimeSeries/Period/Point/position"] for row in result] == [
        "1",
        "2",
        "",
        "",
    ]
    assert [row["TimeSeries/Period/resolution"] for row in result] == [
        "PT60M",
        "PT60M",
        "",
        "PT15M",
    ]
    assert result[2]["TimeSeries/cancelledTS"] == "A09"


# --- what a row carries ------------------------------------------------------


def test_a_row_carries_its_own_point() -> None:
    positions = [row["TimeSeries/Period/Point/position"] for row in rows(FORECAST)]
    quantities = [row["TimeSeries/Period/Point/quantity"] for row in rows(FORECAST)]

    assert positions == ["1", "2"]
    assert quantities == ["2314", "2280"]


def test_a_row_repeats_the_context_above_it() -> None:
    for row in rows(FORECAST):
        assert row["mRID"] == "7b1f9c"
        assert row["TimeSeries/mRID"] == "1"
        assert row["TimeSeries/inBiddingZone_Domain.mRID"] == "10YSK-SEPS-----K"
        assert row["TimeSeries/Period/resolution"] == "PT30M"


def test_the_curve_type_says_how_to_read_the_positions() -> None:
    assert all(row["TimeSeries/curveType"] == "A01" for row in rows(FORECAST))


def test_every_period_of_a_series_contributes_its_points() -> None:
    two_periods = document(
        HEADER + "<TimeSeries><mRID>1</mRID>"
        "<Period><timeInterval><start>2025-12-31T23:00Z</start>"
        "<end>2026-01-01T00:00Z</end></timeInterval>"
        "<resolution>PT60M</resolution>"
        "<Point><position>1</position><quantity>10</quantity></Point></Period>"
        "<Period><timeInterval><start>2026-01-01T00:00Z</start>"
        "<end>2026-01-01T00:30Z</end></timeInterval>"
        "<resolution>PT15M</resolution>"
        "<Point><position>1</position><quantity>20</quantity></Point>"
        "<Point><position>2</position><quantity>30</quantity></Point></Period>"
        "</TimeSeries>"
    )

    resolutions = [row["TimeSeries/Period/resolution"] for row in rows(two_periods)]

    assert resolutions == ["PT60M", "PT15M", "PT15M"]


def test_every_series_of_a_document_contributes_its_points() -> None:
    ids = [row["TimeSeries/mRID"] for row in rows(PER_UNIT)]

    assert ids == ["1", "2"]


# --- column names ------------------------------------------------------------


def test_a_column_is_the_path_to_its_element() -> None:
    assert "TimeSeries/Period/Point/quantity" in columns(FORECAST)


def test_the_namespace_is_stripped() -> None:
    assert not any("}" in column for column in columns(FORECAST))


def test_a_dotted_element_name_survives_intact() -> None:
    # `/` separates nesting and `.` belongs to the element, so a name carrying
    # both stays readable: one segment, one dot, one slash.
    assert "process.processType" in columns(FORECAST)
    assert "time_Period.timeInterval/start" in columns(FORECAST)


def test_the_root_tag_is_not_part_of_the_path() -> None:
    assert "mRID" in columns(FORECAST)
    assert "GL_MarketDocument/mRID" not in columns(FORECAST)


def test_the_same_tag_at_two_depths_gets_two_columns() -> None:
    header = columns(PER_UNIT)

    assert "mRID" in header
    assert "TimeSeries/mRID" in header
    assert "TimeSeries/MktPSRType/PowerSystemResources/mRID" in header


def test_attributes_become_their_own_columns() -> None:
    row = rows(PER_UNIT)[0]

    assert row["TimeSeries/MktPSRType/PowerSystemResources/mRID@codingScheme"] == "A01"
    assert row["TimeSeries/MktPSRType/PowerSystemResources/nominalP@unit"] == "MAW"


def test_an_attribute_on_the_record_path_survives_too() -> None:
    # No current ENTSO-E schema puts an attribute on TimeSeries, Period or
    # Point. The claim is that everything on the path reaches the row, so a
    # document that did would not lose it.
    annotated = document(
        HEADER + '<TimeSeries origin="revised"><mRID>1</mRID>'
        "<Period><resolution>PT60M</resolution>"
        "<Point><position>1</position><quantity>1</quantity></Point>"
        "</Period></TimeSeries>"
    )

    assert rows(annotated)[0]["TimeSeries@origin"] == "revised"


def test_repeated_collapsed_siblings_are_numbered() -> None:
    reasons = document(
        HEADER + "<Reason><code>A95</code></Reason><Reason><code>B18</code></Reason>"
        "<TimeSeries><mRID>1</mRID><Period>"
        "<resolution>PT60M</resolution>"
        "<Point><position>1</position><quantity>1</quantity></Point>"
        "</Period></TimeSeries>"
    )

    row = rows(reasons)[0]

    assert row["Reason[1]/code"] == "A95"
    assert row["Reason[2]/code"] == "B18"


def test_a_lone_sibling_is_numbered_when_the_tag_repeats_elsewhere() -> None:
    # Two reasons on one series and one on the next is a difference in what the
    # sender had to report, not in the field. Numbering only where the repeat
    # falls would file the second series' reason in a column of its own.
    reasons = document(
        HEADER + "<TimeSeries><mRID>1</mRID>"
        "<Reason><code>B18</code></Reason><Reason><code>A95</code></Reason>"
        "</TimeSeries>"
        "<TimeSeries><mRID>2</mRID><Reason><code>B19</code></Reason></TimeSeries>"
    )

    two, one = rows(reasons)

    assert two["TimeSeries/Reason[1]/code"] == "B18"
    assert two["TimeSeries/Reason[2]/code"] == "A95"
    assert one["TimeSeries/Reason[1]/code"] == "B19"
    assert "TimeSeries/Reason/code" not in columns(reasons)


def test_a_withdrawn_series_lands_in_the_same_columns_as_a_live_one() -> None:
    # MoP figure 16: a withdrawn series carries no Period. It is still a record,
    # so it gets its own row under the same TimeSeries columns rather than a
    # numbered column family repeated onto every row of its live sibling.
    mixed = document(
        HEADER + "<TimeSeries><mRID>live</mRID>"
        "<Period><resolution>PT60M</resolution>"
        "<Point><position>1</position><quantity>111</quantity></Point>"
        "</Period></TimeSeries>"
        "<TimeSeries><mRID>withdrawn</mRID><cancelledTS>A09</cancelledTS>"
        "</TimeSeries>"
    )

    live, withdrawn = rows(mixed)

    assert live["TimeSeries/mRID"] == "live"
    assert live["TimeSeries/cancelledTS"] == ""
    assert withdrawn["TimeSeries/mRID"] == "withdrawn"
    assert withdrawn["TimeSeries/cancelledTS"] == "A09"
    assert not any(column.startswith("TimeSeries[") for column in columns(mixed))


def test_points_under_an_unfamiliar_period_element_still_get_a_row_each() -> None:
    # Unavailability documents call the period Available_Period. The name is
    # not one of the three, so only containment keeps its points on the record
    # path -- without it they would collapse and the last one would win.
    outage = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<Unavailability_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-6'
        b':outagedocument:4:0"><mRID>out</mRID>'
        b"<TimeSeries><mRID>T1</mRID>"
        b"<Available_Period><resolution>PT60M</resolution>"
        b"<Point><position>1</position><quantity>0</quantity></Point>"
        b"<Point><position>2</position><quantity>250</quantity></Point>"
        b"<Point><position>3</position><quantity>500</quantity></Point>"
        b"</Available_Period></TimeSeries>"
        b"</Unavailability_MarketDocument>"
    )

    quantities = [
        row["TimeSeries/Available_Period/Point/quantity"] for row in rows(outage)
    ]

    assert quantities == ["0", "250", "500"]


def test_a_series_is_not_numbered_because_it_gets_rows_not_columns() -> None:
    header = columns(PER_UNIT)

    assert "TimeSeries/mRID" in header
    assert "TimeSeries[1]/mRID" not in header


def test_a_column_a_record_lacks_is_left_empty() -> None:
    # Only the second unit reports a secondary quantity, and the row for the
    # first keeps its place in the table rather than shifting.
    first, second = rows(PER_UNIT)

    assert first["TimeSeries/Period/Point/secondaryQuantity"] == ""
    assert second["TimeSeries/Period/Point/secondaryQuantity"] == "1"


def test_series_with_different_optional_elements_share_one_table() -> None:
    # Most of a TimeSeries is optional, so two of them in one document need not
    # carry the same elements -- in either direction. The header is the union
    # of every record's columns, so both still land in one rectangular table.
    uneven = document(
        "<mRID>doc</mRID>"
        "<TimeSeries><mRID>A</mRID><businessType>A01</businessType>"
        "<Period><resolution>PT60M</resolution>"
        "<Point><position>1</position><quantity>11</quantity></Point>"
        "</Period></TimeSeries>"
        "<TimeSeries><mRID>B</mRID><curveType>A01</curveType>"
        "<Period><resolution>PT15M</resolution>"
        "<Point><position>1</position><quantity>22</quantity>"
        "<secondaryQuantity>3</secondaryQuantity></Point>"
        "</Period></TimeSeries>"
    )

    header, (first, second) = read(uneven)

    assert set(header) == set(first) == set(second)
    assert first["TimeSeries/businessType"] == "A01"
    assert first["TimeSeries/curveType"] == ""
    assert first["TimeSeries/Period/Point/secondaryQuantity"] == ""
    assert second["TimeSeries/businessType"] == ""
    assert second["TimeSeries/curveType"] == "A01"
    assert second["TimeSeries/Period/Point/secondaryQuantity"] == "3"


def test_columns_keep_the_order_the_document_used() -> None:
    assert columns(FORECAST)[:3] == ["mRID", "revisionNumber", "type"]


# --- documents that are not the usual shape ----------------------------------


def test_a_cancelled_series_still_records_itself() -> None:
    result = rows(CANCELLED)

    assert len(result) == 1
    assert result[0]["TimeSeries/cancelledTS"] == "A09"


def test_an_unfilled_period_gets_a_row_beside_a_filled_one_of_its_name() -> None:
    # Available_Period earns its record standing from the points it holds, and
    # keeps it document-wide: the sibling holding none gets a row of its own
    # rather than a numbered column family riding on the filled one's rows.
    outage = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b"<Unavailability_MarketDocument><mRID>out</mRID>"
        b"<TimeSeries><mRID>1</mRID>"
        b"<Available_Period><resolution>PT60M</resolution>"
        b"<Point><position>1</position></Point></Available_Period>"
        b"<Available_Period><resolution>PT15M</resolution></Available_Period>"
        b"</TimeSeries></Unavailability_MarketDocument>"
    )

    filled, unfilled = rows(outage)

    assert filled["TimeSeries/Available_Period/resolution"] == "PT60M"
    assert filled["TimeSeries/Available_Period/Point/position"] == "1"
    assert unfilled["TimeSeries/Available_Period/resolution"] == "PT15M"
    assert unfilled["TimeSeries/Available_Period/Point/position"] == ""
    assert not any(
        column.startswith("TimeSeries/Available_Period[") for column in columns(outage)
    )
