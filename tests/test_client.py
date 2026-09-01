import logging
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

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


def acknowledgement(code: str, text: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Acknowledgement_MarketDocument xmlns="urn:iec62325.351:tc57wg16:'
        '451-1:acknowledgementdocument:8:0"><mRID>xyz</mRID><Reason>'
        f"<code>{code}</code><text>{text}</text>"
        "</Reason></Acknowledgement_MarketDocument>"
    ).encode()


# Trimmed from the live response of web-api.tp.entsoe.eu on 2026-09-01: a 5xx
# whose body is an HTML page, not a market document.
MAINTENANCE_PAGE = (
    b'<!doctype html><html lang="en"><head><title>Transparency Platform'
    b"</title></head><body><h1>Service Temporarily Unavailable</h1>"
    b"<p>Scheduled maintenance is currently underway. Please check back soon."
    b"</p></body></html>"
)


@pytest.fixture(autouse=True)
def reset_rate_limiters() -> None:
    """Keep the process-wide token windows isolated between tests."""
    with client._RateLimiter._lock:
        client._RateLimiter._recent_by_token.clear()


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every sleep the client asks for, without actually waiting."""
    recorded: list[float] = []
    monkeypatch.setattr(client, "sleep", recorded.append)
    return recorded


def advance_clock(recorded: list[float], clock: list[float], delay: float) -> None:
    """Record a sleep and advance a deterministic monotonic clock."""
    recorded.append(delay)
    clock[0] += delay


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
def test_returns_the_document_body_verbatim(sleeps: list[float]) -> None:
    stub()
    assert make_client().get(PARAMS) == DOCUMENT
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
def test_zip_payload_is_returned_untouched(sleeps: list[float]) -> None:
    archive = b"PK\x03\x04" + b"\x00" * 32
    stub(body=archive)
    assert make_client().get(PARAMS) == archive


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
def test_reason_999_on_http_400_is_an_invalid_request(sleeps: list[float]) -> None:
    stub(body=acknowledgement("999", "Invalid query attributes"), status=400)
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
    assert make_client().get(PARAMS) == DOCUMENT
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
    assert make_client().get(PARAMS) == DOCUMENT
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
    assert make_client().get(PARAMS) == DOCUMENT


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


# --- local rate limiting ------------------------------------------------------


@responses.activate
def test_local_limiter_pauses_once_the_window_is_full(
    sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [0.0]
    monkeypatch.setattr(client, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        client,
        "sleep",
        lambda delay: advance_clock(sleeps, clock, delay),
    )
    for _ in range(3):
        stub()
    grabber = make_client(max_requests_per_minute=2)
    for _ in range(3):
        grabber.get(PARAMS)
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= 60.0


@responses.activate
def test_local_limiter_stays_out_of_the_way_below_the_ceiling(
    sleeps: list[float],
) -> None:
    for _ in range(3):
        stub()
    grabber = make_client(max_requests_per_minute=10)
    for _ in range(3):
        grabber.get(PARAMS)
    assert sleeps == []


@responses.activate
def test_local_limiter_does_not_sleep_past_the_call_budget(
    sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [0.0]
    monkeypatch.setattr(client, "monotonic", lambda: clock[0])
    stub()
    grabber = make_client(
        max_requests_per_minute=1,
        max_requests_per_second=1,
        total_timeout=30.0,
    )
    grabber.get(PARAMS)
    with pytest.raises(EntsoeRateLimitError) as excinfo:
        grabber.get(PARAMS)
    assert excinfo.value.retry_after == 60.0
    assert len(responses.calls) == 1
    assert sleeps == []


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
        {"max_requests_per_minute": 0},
        {"max_requests_per_minute": 401},
        {"max_requests_per_second": 0},
        {"max_requests_per_second": 8},
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
@pytest.mark.parametrize("status", [200, 400])
def test_truncated_acknowledgement_is_not_mistaken_for_a_document(
    sleeps: list[float], status: int
) -> None:
    stub(body=b"<Acknowledgement_MarketDocument><Reason><code>999", status=status)
    with pytest.raises(EntsoeRequestError) as excinfo:
        make_client().get(PARAMS)
    assert excinfo.value.code is None


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
def test_document_mentioning_the_marker_late_is_still_a_document(
    sleeps: list[float],
) -> None:
    # The sniff only looks at the head of the body, so a market document that
    # happens to quote the word further down is not misread as an error.
    body = DOCUMENT + b"x" * 4096 + b"<!-- Acknowledgement_MarketDocument -->"
    stub(body=body)
    assert make_client().get(PARAMS) == body


# --- limiter internals --------------------------------------------------------


def test_limiter_forgets_requests_older_than_the_window(
    sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [0.0]
    monkeypatch.setattr(client, "monotonic", lambda: clock[0])
    limiter = client._RateLimiter(TOKEN, 1, 1)
    limiter.acquire(deadline=180.0)
    clock[0] = 61.0
    limiter.acquire(deadline=180.0)
    assert sleeps == []


def test_limiter_spreads_a_burst_across_seconds(
    sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The minute has plenty of room; the per-second ceiling is what bites, so
    # a burst waits out the second rather than spending the minute at once.
    clock = [0.0]
    monkeypatch.setattr(client, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        client,
        "sleep",
        lambda delay: advance_clock(sleeps, clock, delay),
    )
    limiter = client._RateLimiter(TOKEN, 350, 2)
    limiter.acquire(deadline=180.0)
    limiter.acquire(deadline=180.0)
    assert sleeps == []
    limiter.acquire(deadline=180.0)
    assert sleeps == [1.0]


def test_limiter_is_shared_by_clients_using_the_same_token(
    sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [0.0]
    monkeypatch.setattr(client, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        client,
        "sleep",
        lambda delay: advance_clock(sleeps, clock, delay),
    )
    first = client._RateLimiter(TOKEN, 350, 2)
    second = client._RateLimiter(TOKEN, 350, 2)
    first.acquire(deadline=180.0)
    first.acquire(deadline=180.0)
    second.acquire(deadline=180.0)
    assert sleeps == [1.0]


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
