import csv
import json
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self

import boto3
import pytest
from moto import mock_aws
from mypy_boto3_s3.client import S3Client
from mypy_boto3_s3.literals import BucketLocationConstraintType

from entsoe_grabber import handler as handler_module
from entsoe_grabber.client import (
    DEFAULT_BASE_URL,
    EntsoeAuthError,
    NoMatchingDataError,
    XmlDocuments,
)
from entsoe_grabber.handler import handler
from entsoe_grabber.serializer import to_csv

BUCKET = "entsoe-grabber-dev-123456789012-eu-central-1"
PREFIX = "entsoe"
REGION: BucketLocationConstraintType = "eu-central-1"

TOKEN = "s3cr3t-token-value"
TOKEN_PARAMETER = "/entsoe-grabber/dev/api-token"
BASE_URL = "https://web-api.tp-iop.entsoe.eu/api"
# CEST in the pinned September, so a market day starts at 22:00 UTC the day before.
MARKET_TIMEZONE = "Europe/Bratislava"

# Pinned so the target dates below are fixed. The negative offset deliberately
# lands in the previous month, which the run date is not in.
NOW = datetime(2026, 9, 1, 13, 45, 0, 123456, tzinfo=UTC)

PARAMS = {
    "documentType": "A71",
    "processType": "A01",
    "in_Domain": "10YSK-SEPS-----K",
}

FORECAST = "generation_forecast_day_ahead"
ACTUAL = "generation_actual_per_unit"

DATASETS = {
    FORECAST: {"date_offset_days": 1, "params": PARAMS},
    ACTUAL: {
        "date_offset_days": -1,
        "params": {"documentType": "A73", "in_Domain": "10YSK-SEPS-----K"},
    },
}

FORECAST_RAW = f"{PREFIX}/raw/2026/09/{FORECAST}-20260902-00.xml"
FORECAST_CSV = f"{PREFIX}/data/2026/09/{FORECAST}-20260902-00.csv"
ACTUAL_RAW = f"{PREFIX}/raw/2026/08/{ACTUAL}-20260831-00.xml"
ACTUAL_CSV = f"{PREFIX}/data/2026/08/{ACTUAL}-20260831-00.csv"

NS = "urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0"
# Trimmed to the elements that shape the CSV: two points, so a run writes more
# than one row, and enough context above them to tell the two documents apart.
DOCUMENT = (
    f'<?xml version="1.0" encoding="UTF-8"?>'
    f'<GL_MarketDocument xmlns="{NS}">'
    f"<mRID>abc</mRID>"
    f"<TimeSeries><mRID>1</mRID>"
    f"<Period>"
    f"<timeInterval><start>2026-09-02T00:00Z</start>"
    f"<end>2026-09-02T01:00Z</end></timeInterval>"
    f"<resolution>PT30M</resolution>"
    f"<Point><position>1</position><quantity>2314</quantity></Point>"
    f"<Point><position>2</position><quantity>2280</quantity></Point>"
    f"</Period></TimeSeries>"
    f"</GL_MarketDocument>"
).encode()
SECOND_DOCUMENT = DOCUMENT.replace(b"<mRID>abc</mRID>", b"<mRID>def</mRID>")


class StubClient:
    """Stands in for :class:`EntsoeClient`, recording queries and replaying.

    One instance serves as both the class the handler constructs and the client
    it gets back, so a test can inspect the constructor arguments and the
    queries through the same object. ``outcomes`` is consumed one query at a
    time, and an ``Exception`` in it is raised rather than returned; once it is
    empty, every further query answers with ``documents``.
    """

    def __init__(self) -> None:
        self.token: str | None = None
        self.base_url: str | None = None
        self.socket_timeouts: tuple[float, float] | None = None
        self.queries: list[dict[str, str]] = []
        self.documents: XmlDocuments = (DOCUMENT,)
        self.outcomes: list[XmlDocuments | Exception] = []
        self.closed = False

    def __call__(
        self,
        token: str,
        base_url: str,
        *,
        connect_timeout: float,
        read_timeout: float,
    ) -> Self:
        self.token = token
        self.base_url = base_url
        self.socket_timeouts = (connect_timeout, read_timeout)
        return self

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.closed = True

    def get(self, params: Mapping[str, str]) -> XmlDocuments:
        self.queries.append(dict(params))
        outcome = self.outcomes.pop(0) if self.outcomes else self.documents
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def entsoe(monkeypatch: pytest.MonkeyPatch) -> StubClient:
    stub = StubClient()
    monkeypatch.setattr(handler_module, "EntsoeClient", stub)
    return stub


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch, entsoe: StubClient) -> Iterator[S3Client]:
    monkeypatch.setattr(handler_module, "_utcnow", lambda: NOW)
    monkeypatch.setenv("OUTPUT_BUCKET", BUCKET)
    monkeypatch.setenv("OUTPUT_PREFIX", PREFIX)
    monkeypatch.setenv("DATASETS_JSON", json.dumps(DATASETS))
    monkeypatch.setenv("ENTSOE_TOKEN_SSM_PARAMETER", TOKEN_PARAMETER)
    monkeypatch.setenv("ENTSOE_BASE_URL", BASE_URL)
    monkeypatch.setenv("MARKET_TIMEZONE", MARKET_TIMEZONE)
    monkeypatch.delenv("STORE_RAW_XML", raising=False)
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
        boto3.client("ssm", region_name=REGION).put_parameter(
            Name=TOKEN_PARAMETER, Value=TOKEN, Type="SecureString"
        )
        yield client


def keys(s3: S3Client) -> list[str]:
    listing = s3.list_objects_v2(Bucket=BUCKET)
    return sorted(item["Key"] for item in listing.get("Contents", []))


def body(s3: S3Client, key: str) -> bytes:
    return s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()


def rows(s3: S3Client, key: str) -> list[dict[str, str]]:
    return list(csv.DictReader(body(s3, key).decode("utf-8").splitlines()))


# --- talking to the platform -------------------------------------------------


def test_the_client_is_built_from_the_configured_token_and_endpoint(
    s3: S3Client, entsoe: StubClient
) -> None:
    handler({}, None)

    assert entsoe.token == TOKEN
    assert entsoe.base_url == BASE_URL
    assert entsoe.socket_timeouts == (5.0, 60.0)


def test_the_session_is_closed_after_the_run(s3: S3Client, entsoe: StubClient) -> None:
    handler({}, None)

    assert entsoe.closed


def test_every_configured_dataset_is_queried_in_order(
    s3: S3Client, entsoe: StubClient
) -> None:
    handler({}, None)

    assert [query["documentType"] for query in entsoe.queries] == ["A71", "A73"]


def test_dataset_params_reach_the_query_verbatim(
    s3: S3Client, entsoe: StubClient
) -> None:
    handler({}, None)

    assert entsoe.queries[0].items() >= PARAMS.items()


def test_the_query_window_covers_the_target_market_day(
    s3: S3Client, entsoe: StubClient
) -> None:
    handler({}, None)

    # Forecast offset is +1 from the pinned 2026-09-01.
    assert entsoe.queries[0]["periodStart"] == "202609012200"
    assert entsoe.queries[0]["periodEnd"] == "202609022200"


def test_a_negative_offset_asks_for_the_earlier_day(
    s3: S3Client, entsoe: StubClient
) -> None:
    handler({}, None)

    assert entsoe.queries[1]["periodStart"] == "202608302200"
    assert entsoe.queries[1]["periodEnd"] == "202608312200"


def test_a_daylight_saving_change_gives_a_25_hour_window(
    s3: S3Client, entsoe: StubClient
) -> None:
    # Forecast target 2026-10-25, the day clocks go back from CEST to CET.
    handler({"time": "2026-10-24T13:30:00Z"}, None)

    assert entsoe.queries[0]["periodStart"] == "202610242200"
    assert entsoe.queries[0]["periodEnd"] == "202610252300"


def test_the_default_endpoint_is_used_when_none_is_configured(
    s3: S3Client, entsoe: StubClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENTSOE_BASE_URL")

    handler({}, None)

    assert entsoe.base_url == DEFAULT_BASE_URL


# --- event dates -------------------------------------------------------------


def test_scheduled_retry_after_midnight_keeps_query_dates_and_object_keys(
    s3: S3Client, entsoe: StubClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = {
        "source": "aws.events",
        "detail-type": "Scheduled Event",
        "time": "2026-09-01T13:30:00Z",
        "detail": {},
    }
    first = handler(event, None)
    first_queries = list(entsoe.queries)

    monkeypatch.setattr(handler_module, "_utcnow", lambda: NOW + timedelta(days=1))
    second = handler(event, None)

    assert (
        second
        == first
        == {
            "written": [FORECAST_RAW, FORECAST_CSV, ACTUAL_RAW, ACTUAL_CSV],
            "skipped": [],
        }
    )
    assert entsoe.queries[len(first_queries) :] == first_queries
    assert first_queries[0]["periodStart"] == "202609012200"
    assert first_queries[1]["periodStart"] == "202608302200"
    assert keys(s3) == sorted(first["written"])


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-09-01T13:30:00Z",
        "2026-09-01T23:59:59.123456+00:00",
        "2026-09-02T01:30:00+02:00",
        "2026-08-31T23:30:00-02:00",
    ],
)
def test_manual_timestamp_uses_its_utc_date_before_applying_offsets(
    s3: S3Client, entsoe: StubClient, monkeypatch: pytest.MonkeyPatch, timestamp: str
) -> None:
    monkeypatch.setattr(handler_module, "_utcnow", lambda: NOW + timedelta(days=4))

    result = handler({"time": timestamp}, None)

    assert result["written"] == [FORECAST_RAW, FORECAST_CSV, ACTUAL_RAW, ACTUAL_CSV]
    assert entsoe.queries[0]["periodStart"] == "202609012200"
    assert entsoe.queries[0]["periodEnd"] == "202609022200"
    assert entsoe.queries[1]["periodStart"] == "202608302200"
    assert entsoe.queries[1]["periodEnd"] == "202608312200"


@pytest.mark.parametrize(
    "event",
    [
        {"source": "aws.events", "detail-type": "Scheduled Event"},
        {"detail-type": "Scheduled Event"},
        {"source": "aws.events", "time": None},
        {"time": 123},
        {"time": ""},
        {"time": "not-a-timestamp"},
        {"time": "2026-02-30T00:00:00Z"},
        {"time": "2026-09-01"},
        {"time": "2026-09-01T13:30:00"},
    ],
)
def test_invalid_event_time_fails_before_external_work(
    s3: S3Client,
    entsoe: StubClient,
    monkeypatch: pytest.MonkeyPatch,
    event: dict[str, Any],
) -> None:
    def unexpected_token_read() -> str:
        pytest.fail("invalid event triggered a token read")

    monkeypatch.setattr(handler_module, "_security_token", unexpected_token_read)

    with pytest.raises(ValueError, match="time"):
        handler(event, None)

    assert entsoe.queries == []
    assert keys(s3) == []


# --- what gets written -------------------------------------------------------


def test_each_dataset_writes_its_raw_document_and_a_csv(s3: S3Client) -> None:
    handler({}, None)

    assert keys(s3) == sorted([FORECAST_RAW, FORECAST_CSV, ACTUAL_RAW, ACTUAL_CSV])


def test_the_raw_object_is_the_document_the_platform_returned(s3: S3Client) -> None:
    handler({}, None)

    assert body(s3, FORECAST_RAW) == DOCUMENT


def test_every_archive_member_is_stored_under_its_own_key(
    s3: S3Client, entsoe: StubClient
) -> None:
    # A ZIP response reaches the handler as several documents for one query.
    # Each is serialized on its own, so each gets a raw object and a CSV,
    # paired by the index in their names.
    entsoe.documents = (DOCUMENT, SECOND_DOCUMENT)

    result = handler({}, None)

    assert result["written"][:4] == [
        f"{PREFIX}/raw/2026/09/{FORECAST}-20260902-00.xml",
        f"{PREFIX}/raw/2026/09/{FORECAST}-20260902-01.xml",
        f"{PREFIX}/data/2026/09/{FORECAST}-20260902-00.csv",
        f"{PREFIX}/data/2026/09/{FORECAST}-20260902-01.csv",
    ]
    assert body(s3, result["written"][1]) == SECOND_DOCUMENT


def test_content_types_distinguish_the_two_artefacts(s3: S3Client) -> None:
    handler({}, None)

    assert s3.head_object(Bucket=BUCKET, Key=FORECAST_RAW)["ContentType"] == (
        "application/xml"
    )
    assert s3.head_object(Bucket=BUCKET, Key=FORECAST_CSV)["ContentType"] == "text/csv"


def test_utcnow_is_timezone_aware() -> None:
    # Pinned in every other test, so assert the real thing keeps its tzinfo.
    assert handler_module._utcnow().tzinfo is UTC


def test_missing_output_bucket_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OUTPUT_BUCKET", raising=False)

    with pytest.raises(KeyError):
        handler({}, None)


# --- the raw flag ------------------------------------------------------------


def test_raw_documents_are_kept_by_default(s3: S3Client) -> None:
    handler({}, None)

    assert FORECAST_RAW in keys(s3)


def test_disabling_the_flag_writes_the_csv_only(
    s3: S3Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STORE_RAW_XML", "false")

    result = handler({}, None)

    assert result["written"] == [FORECAST_CSV, ACTUAL_CSV]
    assert keys(s3) == sorted([FORECAST_CSV, ACTUAL_CSV])


def test_the_flag_accepts_the_usual_spellings(
    s3: S3Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STORE_RAW_XML", " TRUE ")

    assert FORECAST_RAW in handler({}, None)["written"]


def test_an_unrecognized_flag_value_keeps_no_raw_documents(
    s3: S3Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STORE_RAW_XML", "")

    assert handler({}, None)["written"] == [FORECAST_CSV, ACTUAL_CSV]


# --- where it gets written ---------------------------------------------------


def test_positive_offset_partitions_on_the_day_ahead(s3: S3Client) -> None:
    handler({}, None)

    assert FORECAST_CSV in keys(s3)


def test_negative_offset_partitions_into_the_previous_month(s3: S3Client) -> None:
    handler({}, None)

    # Run date is 2026-09-01; the data belongs to August.
    assert ACTUAL_CSV in keys(s3)


def test_empty_prefix_leaves_no_leading_slash(
    s3: S3Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OUTPUT_PREFIX", "")

    result = handler({}, None)

    assert result["written"][0].startswith("raw/")


# --- empty and failed datasets -----------------------------------------------


def test_a_dataset_with_no_data_is_skipped_rather_than_failing(
    s3: S3Client, entsoe: StubClient
) -> None:
    entsoe.outcomes = [NoMatchingDataError("no data", "999", "No matching data found")]

    result = handler({}, None)

    # The first dataset wrote nothing; the second one still ran.
    assert result["written"] == [ACTUAL_RAW, ACTUAL_CSV]
    # Named in the result, so an empty day is distinguishable from no run.
    assert result["skipped"] == [FORECAST]


def test_any_other_failure_stops_the_run(s3: S3Client, entsoe: StubClient) -> None:
    entsoe.outcomes = [EntsoeAuthError("token rejected")]

    with pytest.raises(EntsoeAuthError):
        handler({}, None)

    assert keys(s3) == []


# --- idempotency -------------------------------------------------------------


def test_rerunning_the_same_day_overwrites_rather_than_duplicates(
    s3: S3Client,
) -> None:
    first = handler({}, None)
    second = handler({}, None)

    assert first["written"] == second["written"]
    assert len(keys(s3)) == len(DATASETS) * 2


def test_a_rerun_leaves_the_content_unchanged(s3: S3Client) -> None:
    handler({}, None)
    before = body(s3, FORECAST_CSV)
    handler({}, None)

    assert body(s3, FORECAST_CSV) == before


# --- the parsed csv ----------------------------------------------------------


def test_the_csv_is_what_the_serializer_produced(s3: S3Client) -> None:
    handler({}, None)

    assert body(s3, FORECAST_CSV) == to_csv(DOCUMENT)


def test_the_csv_holds_a_row_per_point(s3: S3Client) -> None:
    handler({}, None)

    quantities = [
        row["TimeSeries/Period/Point/quantity"] for row in rows(s3, FORECAST_CSV)
    ]

    assert quantities == ["2314", "2280"]


def test_each_document_of_the_query_is_serialized_into_its_own_csv(
    s3: S3Client, entsoe: StubClient
) -> None:
    # Documents of one response need not agree on shape, so they are never
    # squeezed into one header -- and only one of them is held at a time.
    entsoe.documents = (DOCUMENT, SECOND_DOCUMENT)

    handler({}, None)

    first = rows(s3, FORECAST_CSV)
    second = rows(s3, FORECAST_CSV.replace("-00.csv", "-01.csv"))

    assert [row["mRID"] for row in first] == ["abc", "abc"]
    assert [row["mRID"] for row in second] == ["def", "def"]
