import csv
import io

from entsoe_grabber.client import XmlDocuments
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


def read(documents: XmlDocuments) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(to_csv(documents).decode("utf-8")))
    rows = list(reader)
    assert reader.fieldnames is not None
    return list(reader.fieldnames), rows


def columns(documents: XmlDocuments) -> list[str]:
    return read(documents)[0]


def rows(documents: XmlDocuments) -> list[dict[str, str]]:
    return read(documents)[1]


# --- one row per point -------------------------------------------------------


def test_every_point_becomes_a_row() -> None:
    assert len(rows((FORECAST,))) == 2


def test_a_row_carries_its_own_point() -> None:
    positions = [row["TimeSeries/Period/Point/position"] for row in rows((FORECAST,))]
    quantities = [row["TimeSeries/Period/Point/quantity"] for row in rows((FORECAST,))]

    assert positions == ["1", "2"]
    assert quantities == ["2314", "2280"]


def test_a_row_repeats_the_context_above_it() -> None:
    for row in rows((FORECAST,)):
        assert row["mRID"] == "7b1f9c"
        assert row["TimeSeries/mRID"] == "1"
        assert row["TimeSeries/inBiddingZone_Domain.mRID"] == "10YSK-SEPS-----K"
        assert row["TimeSeries/Period/resolution"] == "PT30M"


def test_the_curve_type_says_how_to_read_the_positions() -> None:
    assert all(row["TimeSeries/curveType"] == "A01" for row in rows((FORECAST,)))


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

    resolutions = [row["TimeSeries/Period/resolution"] for row in rows((two_periods,))]

    assert resolutions == ["PT60M", "PT15M", "PT15M"]


# --- column names ------------------------------------------------------------


def test_a_column_is_the_path_to_its_element() -> None:
    assert "TimeSeries/Period/Point/quantity" in columns((FORECAST,))


def test_the_namespace_is_stripped() -> None:
    assert not any("}" in column for column in columns((FORECAST,)))


def test_a_dotted_element_name_survives_intact() -> None:
    # `/` separates nesting and `.` belongs to the element, so a name carrying
    # both stays readable: one segment, one dot, one slash.
    assert "process.processType" in columns((FORECAST,))
    assert "time_Period.timeInterval/start" in columns((FORECAST,))


def test_the_root_tag_is_not_part_of_the_path() -> None:
    assert "mRID" in columns((FORECAST,))
    assert "GL_MarketDocument/mRID" not in columns((FORECAST,))


def test_the_same_tag_at_two_depths_gets_two_columns() -> None:
    header = columns((PER_UNIT,))

    assert "mRID" in header
    assert "TimeSeries/mRID" in header
    assert "TimeSeries/MktPSRType/PowerSystemResources/mRID" in header


def test_attributes_become_their_own_columns() -> None:
    row = rows((PER_UNIT,))[0]

    assert row["TimeSeries/MktPSRType/PowerSystemResources/mRID@codingScheme"] == "A01"
    assert row["TimeSeries/MktPSRType/PowerSystemResources/nominalP@unit"] == "MAW"


def test_an_attribute_on_a_container_survives_too() -> None:
    # No current ENTSO-E schema puts an attribute on TimeSeries, Period or
    # Point. The claim is that everything on the path reaches the row, so a
    # document that did would not lose it.
    annotated = document(
        HEADER + '<TimeSeries origin="revised"><mRID>1</mRID>'
        "<Period><resolution>PT60M</resolution>"
        "<Point><position>1</position><quantity>1</quantity></Point>"
        "</Period></TimeSeries>"
    )

    assert rows((annotated,))[0]["TimeSeries@origin"] == "revised"


def test_repeated_collapsed_siblings_are_numbered() -> None:
    reasons = document(
        HEADER + "<Reason><code>A95</code></Reason><Reason><code>B18</code></Reason>"
        "<TimeSeries><mRID>1</mRID><Period>"
        "<resolution>PT60M</resolution>"
        "<Point><position>1</position><quantity>1</quantity></Point>"
        "</Period></TimeSeries>"
    )

    row = rows((reasons,))[0]

    assert row["Reason[1]/code"] == "A95"
    assert row["Reason[2]/code"] == "B18"


def test_a_series_is_not_numbered_because_it_gets_rows_not_columns() -> None:
    header = columns((PER_UNIT,))

    assert "TimeSeries/mRID" in header
    assert "TimeSeries[1]/mRID" not in header


# --- documents that are not the usual shape ----------------------------------


def test_a_document_without_points_still_records_itself() -> None:
    result = rows((CANCELLED,))

    assert len(result) == 1
    assert result[0]["TimeSeries/cancelledTS"] == "A09"


def test_no_documents_still_produces_a_header() -> None:
    assert to_csv(()).decode("utf-8").splitlines() == ["document"]


# --- more than one document per response -------------------------------------


def test_rows_name_the_document_they_came_from() -> None:
    indexes = [row["document"] for row in rows((FORECAST, PER_UNIT))]

    assert indexes == ["0", "0", "1", "1"]


def test_the_header_is_the_union_of_every_document() -> None:
    header = columns((FORECAST, PER_UNIT))

    assert "TimeSeries/curveType" in header
    assert "TimeSeries/MktPSRType/psrType" in header


def test_a_column_one_document_lacks_is_left_empty() -> None:
    from_forecast, _, from_per_unit, _ = rows((FORECAST, PER_UNIT))

    assert from_forecast["TimeSeries/MktPSRType/psrType"] == ""
    assert from_per_unit["TimeSeries/curveType"] == ""


def test_the_document_column_comes_first() -> None:
    assert columns((FORECAST,))[0] == "document"
