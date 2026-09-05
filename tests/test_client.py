import logging
import struct
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from io import BytesIO
from typing import Any
from xml.etree import ElementTree
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest
import requests
import responses

from entsoe_grabber import client
from entsoe_grabber.client import (
    DEFAULT_BASE_URL,
    EntsoeAuthError,
    EntsoeClient,
    EntsoeConnectionError,
    EntsoeRateLimitError,
    EntsoeRequestError,
    EntsoeServerError,
    NoMatchingDataError,
)

TOKEN = "s3cr3t-token-value"
PARAMS = {
    "documentType": "A44",
    "in_Domain": "10YSK-SEPS-----K",
    "out_Domain": "10YSK-SEPS-----K",
    "periodStart": "202601010000",
    "periodEnd": "202601020000",
}

DOCUMENT = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:'
    b'publicationdocument:7:0"><mRID>abc</mRID></Publication_MarketDocument>'
)
SECOND_DOCUMENT = DOCUMENT.replace(b"<mRID>abc</mRID>", b"<mRID>def</mRID>")


def acknowledgement(code: str, text: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Acknowledgement_MarketDocument xmlns="urn:iec62325.351:tc57wg16:'
        '451-1:acknowledgementdocument:8:0"><mRID>xyz</mRID><Reason>'
        f"<code>{code}</code><text>{text}</text>"
        "</Reason></Acknowledgement_MarketDocument>"
    ).encode()


def zip_body(files: dict[str, bytes], *, compression: int = ZIP_STORED) -> bytes:
    """Build an in-memory ZIP response for a test."""
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=compression) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def corrupt_deflate_zip() -> bytes:
    """Keep the ZIP directory intact but corrupt the member's DEFLATE stream."""
    content = bytearray(zip_body({"document.xml": DOCUMENT}, compression=ZIP_DEFLATED))
    name_length, extra_length = struct.unpack_from("<HH", content, 26)
    # The local header has 30 fixed bytes, then the filename and extra fields.
    # 0b111 is a final DEFLATE block with the reserved (invalid) block type.
    content[30 + name_length + extra_length] = 0b111
    return bytes(content)


# Trimmed from the live response of web-api.tp.entsoe.eu on 2026-09-01: a 5xx
# whose body is an HTML page, not a market document.
MAINTENANCE_PAGE = (
    b'<!doctype html><html lang="en"><head><title>Transparency Platform'
    b"</title></head><body><h1>Service Temporarily Unavailable</h1>"
    b"<p>Scheduled maintenance is currently underway. Please check back soon."
    b"</p></body></html>"
)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every sleep the client asks for, without actually waiting."""
    recorded: list[float] = []
    monkeypatch.setattr(client, "sleep", recorded.append)
    return recorded


def make_client(**kwargs: Any) -> EntsoeClient:
    kwargs.setdefault("max_attempts", 3)
    return EntsoeClient(TOKEN, **kwargs)


def stub(
    body: bytes | Exception = DOCUMENT,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> None:
    responses.add(
        responses.GET, DEFAULT_BASE_URL, body=body, status=status, headers=headers
    )


# --- success -----------------------------------------------------------------


@responses.activate
def test_returns_direct_xml_as_one_document(sleeps: list[float]) -> None:
    stub()
    assert make_client().get(PARAMS) == (DOCUMENT,)
    assert sleeps == []


@responses.activate
def test_sends_the_token_as_a_query_parameter(sleeps: list[float]) -> None:
    stub()
    make_client().get(PARAMS)
    url = responses.calls[0].request.url
    assert url is not None
    assert f"securityToken={TOKEN}" in url
    assert "documentType=A44" in url


@responses.activate
@pytest.mark.parametrize("compression", [ZIP_STORED, ZIP_DEFLATED])
def test_zip_payload_is_unpacked_into_xml_documents(
    sleeps: list[float], compression: int
) -> None:
    stub(
        body=zip_body(
            {"first.xml": DOCUMENT, "nested/second.XML": SECOND_DOCUMENT},
            compression=compression,
        )
    )
    assert make_client().get(PARAMS) == (DOCUMENT, SECOND_DOCUMENT)


@responses.activate
def test_archive_member_name_does_not_determine_its_document_type(
    sleeps: list[float],
) -> None:
    stub(
        body=zip_body(
            {"Acknowledgement_MarketDocument.xml": DOCUMENT}, compression=ZIP_DEFLATED
        )
    )

    assert make_client().get(PARAMS) == (DOCUMENT,)


@responses.activate
def test_corrupt_deflate_is_retried_then_succeeds(sleeps: list[float]) -> None:
    stub(body=corrupt_deflate_zip())
    stub()

    assert make_client().get(PARAMS) == (DOCUMENT,)
    assert len(responses.calls) == 2
    assert len(sleeps) == 1


@responses.activate
def test_corrupt_deflate_raises_server_error_after_retries(sleeps: list[float]) -> None:
    stub(body=corrupt_deflate_zip())

    with pytest.raises(EntsoeServerError, match="unusable ZIP archive"):
        make_client().get(PARAMS)

    assert len(responses.calls) == 3
    assert len(sleeps) == 2


@responses.activate
def test_non_xml_http_200_is_retried_as_a_server_failure(
    sleeps: list[float],
) -> None:
    stub(body=b'{"status":"ok"}', headers={"Content-Type": "application/json"})
    with pytest.raises(EntsoeServerError, match="not well-formed XML"):
        make_client(max_attempts=1).get(PARAMS)


@responses.activate
def test_zip_with_non_xml_content_is_rejected(sleeps: list[float]) -> None:
    stub(body=zip_body({"document.xml": DOCUMENT, "readme.txt": b"not XML"}))
    with pytest.raises(EntsoeServerError, match="non-XML content"):
        make_client(max_attempts=1).get(PARAMS)


@responses.activate
def test_zip_with_malformed_xml_is_rejected(sleeps: list[float]) -> None:
    stub(body=zip_body({"broken.xml": b"<Publication_MarketDocument>"}))
    with pytest.raises(EntsoeServerError, match="not well-formed XML"):
        make_client(max_attempts=1).get(PARAMS)


@responses.activate
def test_empty_zip_is_rejected(sleeps: list[float]) -> None:
    stub(body=zip_body({}))
    with pytest.raises(EntsoeServerError, match="contains no XML documents"):
        make_client(max_attempts=1).get(PARAMS)


# --- acknowledgement documents ------------------------------------------------


@responses.activate
def test_reason_999_on_http_200_raises_no_matching_data(
    sleeps: list[float],
) -> None:
    stub(body=acknowledgement("999", "No matching data found"), status=200)
    with pytest.raises(NoMatchingDataError) as excinfo:
        make_client().get(PARAMS)
    assert excinfo.value.code == "999"
    assert excinfo.value.text == "No matching data found"
    assert len(responses.calls) == 1


@responses.activate
@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize("representation", ["utf16", "leading_comment", "prefix"])
def test_acknowledgement_detection_uses_parsed_xml(
    sleeps: list[float], archived: bool, representation: str
) -> None:
    body = acknowledgement("999", "No matching data found")
    if representation == "utf16":
        body = body.decode().replace("UTF-8", "UTF-16").encode("utf-16")
    elif representation == "leading_comment":
        body = body.replace(b"?>", b"?>\n<!--" + b"x" * 4096 + b"-->", 1)
    else:
        body = ElementTree.tostring(ElementTree.fromstring(body))
    if archived:
        body = zip_body({"acknowledgement.xml": body}, compression=ZIP_DEFLATED)
    stub(body=body)

    with pytest.raises(NoMatchingDataError) as excinfo:
        make_client().get(PARAMS)

    assert excinfo.value.code == "999"
    assert excinfo.value.text == "No matching data found"
    assert len(responses.calls) == 1
    assert sleeps == []


@responses.activate
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_reason_999_on_http_400_is_an_invalid_request(
    sleeps: list[float], encoding: str
) -> None:
    body = acknowledgement("999", "Invalid query attributes")
    body = body.decode().replace("UTF-8", encoding).encode(encoding)
    stub(body=body, status=400)
    with pytest.raises(EntsoeRequestError) as excinfo:
        make_client().get(PARAMS)
    assert not isinstance(excinfo.value, NoMatchingDataError)
    assert excinfo.value.code == "999"
    assert excinfo.value.text == "Invalid query attributes"
    assert len(responses.calls) == 1


@responses.activate
@pytest.mark.parametrize("status", [200, 400])
def test_other_reason_raises_request_error(sleeps: list[float], status: int) -> None:
    stub(
        body=acknowledgement("999999", "Check request against dependency"),
        status=status,
    )
    with pytest.raises(EntsoeRequestError) as excinfo:
        make_client().get(PARAMS)
    assert not isinstance(excinfo.value, NoMatchingDataError)
    assert excinfo.value.code == "999999"
    assert len(responses.calls) == 1


@responses.activate
def test_bad_request_without_an_acknowledgement_still_raises_request_error(
    sleeps: list[float],
) -> None:
    stub(body=b"nonsense", status=400)
    with pytest.raises(EntsoeRequestError):
        make_client().get(PARAMS)
    assert len(responses.calls) == 1


# --- non-retryable statuses ---------------------------------------------------


@responses.activate
@pytest.mark.parametrize("status", [401, 403])
def test_rejected_token_is_not_retried(sleeps: list[float], status: int) -> None:
    stub(body=b"", status=status)
    with pytest.raises(EntsoeAuthError):
        make_client().get(PARAMS)
    assert len(responses.calls) == 1
    assert sleeps == []


@responses.activate
def test_not_found_is_not_retried(sleeps: list[float]) -> None:
    stub(body=b"", status=404)
    with pytest.raises(EntsoeRequestError):
        make_client().get(PARAMS)
    assert len(responses.calls) == 1


@responses.activate
@pytest.mark.parametrize("status", [204, 304])
def test_unexpected_non_200_response_is_not_accepted(
    sleeps: list[float], status: int
) -> None:
    stub(body=b"", status=status)
    with pytest.raises(EntsoeRequestError):
        make_client().get(PARAMS)
    assert len(responses.calls) == 1
    assert sleeps == []


# --- retryable statuses -------------------------------------------------------


@responses.activate
def test_rate_limit_with_retry_after_is_retried_then_succeeds(
    sleeps: list[float],
) -> None:
    stub(body=b"", status=429, headers={"Retry-After": "5"})
    stub()
    assert make_client().get(PARAMS) == (DOCUMENT,)
    assert len(responses.calls) == 2
    assert sleeps == [5.0]


@responses.activate
def test_rate_limit_without_retry_after_gives_up_at_once(sleeps: list[float]) -> None:
    # A bare 429 most likely means the token is banned for about ten minutes.
    # Backing off for seconds cannot outlast that, and each retry adds to the
    # per-token count that caused it.
    for _ in range(3):
        stub(body=b"", status=429)
    with pytest.raises(EntsoeRateLimitError):
        make_client(max_attempts=3).get(PARAMS)
    assert len(responses.calls) == 1
    assert sleeps == []


@responses.activate
def test_backoff_grows_and_stays_jittered(sleeps: list[float]) -> None:
    for _ in range(4):
        stub(body=b"", status=503)
    with pytest.raises(EntsoeServerError):
        make_client(max_attempts=4, backoff_base=2.0).get(PARAMS)
    # Full jitter over base * 2**(attempt - 1): [1, 2], [2, 4], [4, 8].
    assert len(sleeps) == 3
    assert 1.0 <= sleeps[0] <= 2.0
    assert 2.0 <= sleeps[1] <= 4.0
    assert 4.0 <= sleeps[2] <= 8.0


@responses.activate
def test_retry_after_header_overrides_backoff(sleeps: list[float]) -> None:
    stub(body=b"", status=429, headers={"Retry-After": "7"})
    stub()
    make_client().get(PARAMS)
    assert sleeps == [7.0]


@responses.activate
def test_retry_after_zero_still_pauses(sleeps: list[float]) -> None:
    # Floored at the backoff base: honouring a literal 0 would fire the next
    # request off with no pause at all.
    stub(body=b"", status=429, headers={"Retry-After": "0"})
    stub()
    make_client(backoff_base=2.0).get(PARAMS)
    assert sleeps == [2.0]


@responses.activate
def test_retry_after_longer_than_the_budget_fails_fast(sleeps: list[float]) -> None:
    # A real ENTSO-E ban lasts ten minutes; the Lambda budget is far shorter.
    stub(body=b"", status=429, headers={"Retry-After": "600"})
    with pytest.raises(EntsoeRateLimitError):
        make_client(total_timeout=30.0).get(PARAMS)
    assert len(responses.calls) == 1
    assert sleeps == []


@responses.activate
@pytest.mark.parametrize("status", [200, 503])
def test_retry_budget_does_not_interrupt_an_in_flight_request(
    sleeps: list[float], monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    elapsed = [0.0]
    monkeypatch.setattr(client, "monotonic", lambda: elapsed[0])

    def slow_response(
        request: requests.PreparedRequest,
    ) -> tuple[int, dict[str, str], bytes]:
        # Model a download finishing after the retry budget. A valid response
        # remains useful; a failure must not schedule another request.
        elapsed[0] = 31.0
        return status, {}, DOCUMENT if status == 200 else b"unavailable"

    responses.add_callback(responses.GET, DEFAULT_BASE_URL, callback=slow_response)
    grabber = make_client(total_timeout=30.0)
    if status == 200:
        assert grabber.get(PARAMS) == (DOCUMENT,)
    else:
        with pytest.raises(EntsoeServerError):
            grabber.get(PARAMS)

    assert len(responses.calls) == 1
    assert sleeps == []


@responses.activate
def test_backoff_that_exhausts_the_retry_budget_prevents_another_attempt(
    sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    elapsed = [0.0]
    monkeypatch.setattr(client, "monotonic", lambda: elapsed[0])

    def oversleep(delay: float) -> None:
        sleeps.append(delay)
        elapsed[0] = 31.0

    monkeypatch.setattr(client, "sleep", oversleep)
    stub(body=b"unavailable", status=503)

    with pytest.raises(EntsoeServerError):
        make_client(total_timeout=30.0).get(PARAMS)

    assert len(responses.calls) == 1
    assert len(sleeps) == 1


@responses.activate
def test_maintenance_page_is_retried_then_raises_server_error(
    sleeps: list[float],
) -> None:
    for _ in range(3):
        stub(body=MAINTENANCE_PAGE, status=503)
    with pytest.raises(EntsoeServerError) as excinfo:
        make_client(max_attempts=3).get(PARAMS)
    assert "Service Temporarily Unavailable" in str(excinfo.value)
    assert len(responses.calls) == 3


@responses.activate
def test_html_http_200_is_retried_then_raises_server_error(
    sleeps: list[float],
) -> None:
    for _ in range(3):
        stub(
            body=MAINTENANCE_PAGE,
            status=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
        )
    with pytest.raises(EntsoeServerError, match="unexpected HTML response"):
        make_client(max_attempts=3).get(PARAMS)
    assert len(responses.calls) == 3


@responses.activate
def test_empty_http_200_is_retried_as_a_server_failure(sleeps: list[float]) -> None:
    for _ in range(3):
        stub(body=b"", status=200)
    with pytest.raises(EntsoeServerError, match="empty response body"):
        make_client().get(PARAMS)
    assert len(responses.calls) == 3
    assert len(sleeps) == 2


@responses.activate
def test_http_408_is_retried_as_a_server_failure(sleeps: list[float]) -> None:
    stub(body=b"request timed out", status=408)
    stub()
    assert make_client().get(PARAMS) == (DOCUMENT,)
    assert len(responses.calls) == 2
    assert len(sleeps) == 1


@responses.activate
def test_connection_failure_is_retried_then_raises(sleeps: list[float]) -> None:
    for _ in range(3):
        responses.add(
            responses.GET, DEFAULT_BASE_URL, body=requests.ConnectionError("refused")
        )
    with pytest.raises(EntsoeConnectionError):
        make_client(max_attempts=3).get(PARAMS)
    assert len(responses.calls) == 3
    assert len(sleeps) == 2


@responses.activate
def test_read_timeout_is_treated_as_transient(sleeps: list[float]) -> None:
    responses.add(
        responses.GET, DEFAULT_BASE_URL, body=requests.ReadTimeout("too slow")
    )
    stub()
    assert make_client().get(PARAMS) == (DOCUMENT,)


# --- the token must not leak --------------------------------------------------


@responses.activate
def test_token_never_reaches_logs_or_errors(
    sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    leaky = requests.ConnectionError(
        f"failed: {DEFAULT_BASE_URL}?securityToken={TOKEN}&documentType=A44"
    )
    for _ in range(3):
        responses.add(responses.GET, DEFAULT_BASE_URL, body=leaky)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(EntsoeConnectionError) as excinfo:
        make_client(max_attempts=3).get(PARAMS)

    assert TOKEN not in str(excinfo.value)
    assert "securityToken=***" in str(excinfo.value)
    assert TOKEN not in caplog.text
    # The replacement is raised outside the except block, so the original
    # token-bearing exception is absent rather than merely hidden.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert excinfo.value.__suppress_context__


@responses.activate
def test_non_transient_requests_error_is_sanitized_without_retry(
    sleeps: list[float],
) -> None:
    responses.add(
        responses.GET,
        DEFAULT_BASE_URL,
        body=requests.exceptions.InvalidURL(
            f"bad URL: {DEFAULT_BASE_URL}?securityToken={TOKEN}"
        ),
    )
    with pytest.raises(EntsoeRequestError) as excinfo:
        make_client().get(PARAMS)
    assert TOKEN not in str(excinfo.value)
    assert excinfo.value.__context__ is None
    assert len(responses.calls) == 1
    assert sleeps == []


@responses.activate
def test_successful_request_is_logged_with_the_token_redacted(
    sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    stub()
    caplog.set_level(logging.INFO)
    make_client().get(PARAMS)
    assert TOKEN not in caplog.text
    assert "securityToken=***" in caplog.text


# --- configuration ------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_url": "http://web-api.tp.entsoe.eu/api"},
        {"base_url": "not-a-url"},
        {"connect_timeout": 0.0},
        {"read_timeout": float("nan")},
        {"max_attempts": 0},
        {"backoff_base": 0.0},
        {"backoff_max": 0.5},
        {"total_timeout": 0.0},
    ],
)
def test_invalid_configuration_fails_fast(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        EntsoeClient(TOKEN, **kwargs)


def test_empty_security_token_fails_fast() -> None:
    with pytest.raises(ValueError, match="security_token"):
        EntsoeClient("  ")


# --- session lifecycle --------------------------------------------------------


@responses.activate
def test_context_manager_closes_the_session(
    sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    session = requests.Session()
    closed: list[bool] = []
    monkeypatch.setattr(session, "close", lambda: closed.append(True))
    stub()
    with EntsoeClient(TOKEN, session=session) as grabber:
        grabber.get(PARAMS)
    assert closed == [True]


# --- Retry-After parsing ------------------------------------------------------


@responses.activate
def test_retry_after_accepts_an_http_date(sleeps: list[float]) -> None:
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=45))
    stub(body=b"", status=429, headers={"Retry-After": when})
    stub()
    make_client().get(PARAMS)
    assert 40.0 <= sleeps[0] <= 45.0


@responses.activate
def test_retry_after_in_the_past_is_floored_to_the_backoff_base(
    sleeps: list[float],
) -> None:
    when = format_datetime(datetime.now(UTC) - timedelta(seconds=30))
    stub(body=b"", status=429, headers={"Retry-After": when})
    stub()
    make_client().get(PARAMS)
    assert sleeps == [1.0]


@responses.activate
def test_unusable_retry_after_gives_up_like_a_missing_one(sleeps: list[float]) -> None:
    # A header that cannot be parsed says no more about when to come back than
    # no header at all, so it takes the same path rather than a special case.
    stub(body=b"", status=429, headers={"Retry-After": "whenever"})
    stub()
    with pytest.raises(EntsoeRateLimitError):
        make_client(backoff_base=2.0).get(PARAMS)
    assert len(responses.calls) == 1
    assert sleeps == []


# --- malformed acknowledgements -----------------------------------------------


@responses.activate
def test_truncated_error_acknowledgement_is_not_retried(
    sleeps: list[float],
) -> None:
    stub(body=b"<Acknowledgement_MarketDocument><Reason><code>999", status=400)
    with pytest.raises(EntsoeRequestError) as excinfo:
        make_client().get(PARAMS)
    assert excinfo.value.code is None
    assert len(responses.calls) == 1
    assert sleeps == []


@responses.activate
@pytest.mark.parametrize("archived", [False, True])
def test_truncated_success_acknowledgement_is_retried(
    sleeps: list[float], archived: bool
) -> None:
    body = b"<Acknowledgement_MarketDocument><Reason><code>999"
    if archived:
        body = zip_body({"acknowledgement.xml": body})
    stub(body=body)
    stub()

    assert make_client().get(PARAMS) == (DOCUMENT,)
    assert len(responses.calls) == 2
    assert len(sleeps) == 1


@responses.activate
def test_truncated_success_acknowledgement_exhausts_retries(
    sleeps: list[float],
) -> None:
    stub(body=b"<Acknowledgement_MarketDocument><Reason><code>999")

    with pytest.raises(EntsoeServerError, match="not well-formed XML"):
        make_client().get(PARAMS)

    assert len(responses.calls) == 3
    assert len(sleeps) == 2


@responses.activate
def test_acknowledgement_without_a_reason_still_raises(sleeps: list[float]) -> None:
    stub(
        body=b"<Acknowledgement_MarketDocument><mRID>x</mRID>"
        b"</Acknowledgement_MarketDocument>"
    )
    with pytest.raises(EntsoeRequestError) as excinfo:
        make_client().get(PARAMS)
    assert excinfo.value.code is None
    assert excinfo.value.text is None


@responses.activate
@pytest.mark.parametrize("padding", [0, 4096])
def test_document_mentioning_acknowledgements_is_still_a_document(
    sleeps: list[float], padding: int
) -> None:
    # A document's text never identifies its type, even near the root tag.
    closing_tag = b"</Publication_MarketDocument>"
    body = DOCUMENT.replace(
        closing_tag,
        b"<note>"
        + b"x" * padding
        + b"Acknowledgement_MarketDocument</note>"
        + closing_tag,
    )
    stub(body=body)
    assert make_client().get(PARAMS) == (body,)


@responses.activate
def test_retry_after_with_an_unknown_zone_is_treated_as_utc(
    sleeps: list[float],
) -> None:
    # "-0000" means "UTC, but the real zone is unknown"; email.utils hands back
    # a naive datetime for it, which would otherwise blow up the subtraction.
    when = datetime.now(UTC) + timedelta(seconds=45)
    stub(
        body=b"",
        status=429,
        headers={"Retry-After": when.strftime("%a, %d %b %Y %H:%M:%S -0000")},
    )
    stub()
    make_client().get(PARAMS)
    assert 40.0 <= sleeps[0] <= 45.0


# Captured verbatim from https://web-api.tp-iop.entsoe.eu/api on 2026-09-01.
# Note the reason code: 999 is documented as "No matching data found", but the
# platform also uses it for a rejected token.
IOP_AUTH_FAILURE = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<Acknowledgement_MarketDocument xmlns="urn:iec62325.351:tc57wg16:'
    b'451-1:acknowledgementdocument:7:0">\n'
    b"\t<mRID>b7120a69-9952-4</mRID>\n"
    b"\t<createdDateTime>2026-09-01T07:40:52Z</createdDateTime>\n"
    b"\t<Reason>\n\t\t<code>999</code>\n"
    b"\t\t<text>Authentication failed.</text>\n\t</Reason>\n"
    b"</Acknowledgement_MarketDocument>"
)


@responses.activate
def test_rejected_token_beats_the_999_in_its_own_body(sleeps: list[float]) -> None:
    # Regression guard: classify on status before body. Reading the body first
    # would turn a bad token into NoMatchingDataError, and a scheduled run
    # would report zero rows every day instead of failing.
    stub(body=IOP_AUTH_FAILURE, status=401)
    with pytest.raises(EntsoeAuthError):
        make_client().get(PARAMS)
    assert len(responses.calls) == 1
