"""GET-only HTTP client for the ENTSO-E Transparency Platform RESTful API.

The platform serves market data as XML documents from a single endpoint,
selected entirely by query parameters. This module owns the transport concerns
-- authentication, timeouts, retries and rate limiting -- and hands back the
response body untouched. Parsing belongs elsewhere.

Two shapes of body are normal on success. Most queries answer with an XML
market document; queries whose result set the platform considers too large
answer with a ZIP archive instead, recognisable by its ``PK`` magic bytes.
:meth:`EntsoeClient.get` returns both unchanged.
"""

import logging
import random
import re
from collections import deque
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256
from math import isfinite
from threading import Lock
from time import monotonic, sleep
from types import TracebackType
from typing import ClassVar, Self
from urllib.parse import urlsplit
from xml.etree import ElementTree

import requests

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://web-api.tp.entsoe.eu/api"

# Reason code returned for both empty results and rejected requests. The HTTP
# status disambiguates it: 200 means no data, while 400 means an invalid query.
_NO_MATCHING_DATA_CODE = "999"

_ACKNOWLEDGEMENT_MARKER = b"Acknowledgement_MarketDocument"
_ACKNOWLEDGEMENT_SNIFF_BYTES = 2048
_TOKEN_PATTERN = re.compile(r"(securityToken=)[^&\s]*", re.IGNORECASE)
_WINDOW_SECONDS = 60.0
_SUMMARY_LIMIT = 200
_PLATFORM_REQUESTS_PER_MINUTE = 400
_RECOMMENDED_REQUESTS_PER_SECOND = 7


class EntsoeError(Exception):
    """Base class for every error raised by :class:`EntsoeClient`."""


class EntsoeTransientError(EntsoeError):
    """A failure that may resolve on its own.

    Usually raised after the retry budget is spent. A rate-limit response with
    no usable delay, or a local throttle that cannot fit inside the call's
    deadline, fails immediately instead. Callers can treat it as "try again on
    the next scheduled run" rather than "the query is wrong".

    Parameters
    ----------
    message
        Human-readable description, already stripped of the security token.
    retry_after
        Delay the platform asked for, or the local limiter needs, in seconds.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class EntsoeConnectionError(EntsoeTransientError):
    """The request never produced a response."""


class EntsoeServerError(EntsoeTransientError):
    """The platform timed out, answered 5xx, or returned an unusable success.

    Includes scheduled maintenance, which serves an HTML page rather than a
    market document.
    """


class EntsoeRateLimitError(EntsoeTransientError):
    """The platform answered 429, or the local token budget is exhausted.

    ENTSO-E allows 400 requests per minute, counted per security token rather
    than per IP: the R3 API dropped IP-based limiting, so everything sharing a
    token shares one budget. Exceeding it can ban the token for about ten
    minutes, which outlives any sensible Lambda timeout. The client therefore
    retries only when the platform named a ``Retry-After`` it can sit out
    inside the budget, and gives up at once otherwise -- blind retries cannot
    outlast a ban, and each one spends more of the budget that earned it.
    A local throttle also gives up immediately when its required wait would
    exceed the call's deadline.
    """


class EntsoeAuthError(EntsoeError):
    """The security token was missing, invalid, or suspended."""


class EntsoeRequestError(EntsoeError):
    """The platform rejected the query itself. Retrying will not help.

    Parameters
    ----------
    message
        Human-readable description, already stripped of the security token.
    code
        ``Reason.code`` from the acknowledgement document, when it carried one.
    text
        ``Reason.text`` from the acknowledgement document, when present.
    """

    def __init__(
        self, message: str, code: str | None = None, text: str | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.text = text


class NoMatchingDataError(EntsoeRequestError):
    """The query was well-formed but the platform holds no data for it.

    Reason code 999. This is an empty result, not a fault: a control area that
    published nothing for the requested day lands here, and callers usually
    want to record zero rows and carry on.
    """


def _redact(text: str) -> str:
    """Replace every ``securityToken`` value in ``text`` with ``***``."""
    return _TOKEN_PATTERN.sub(r"\1***", text)


def _summarise(content: bytes) -> str:
    """Condense a response body into one short line fit for a log message."""
    collapsed = " ".join(content.decode("utf-8", errors="replace").split())
    if len(collapsed) > _SUMMARY_LIMIT:
        collapsed = collapsed[:_SUMMARY_LIMIT] + "..."
    return _redact(collapsed) or "<empty body>"


def _local_name(tag: str) -> str:
    """Strip the XML namespace from a tag name."""
    return tag.rpartition("}")[2]


def _acknowledgement_reason(content: bytes) -> tuple[str | None, str | None] | None:
    """Extract ``(code, text)`` from an acknowledgement document.

    Returns
    -------
    tuple or None
        ``None`` when the body is not an acknowledgement at all, which is the
        normal case for a market document and for the HTML page a maintenance
        window returns. A pair of ``None`` when it is one but carries no
        readable reason.
    """
    if _ACKNOWLEDGEMENT_MARKER not in content[:_ACKNOWLEDGEMENT_SNIFF_BYTES]:
        return None
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        # Truncated or corrupt: still an acknowledgement, just an unreadable
        # one. Reporting it as a document would hand the caller garbage.
        return None, None
    for element in root.iter():
        if _local_name(element.tag) != "Reason":
            continue
        code: str | None = None
        text: str | None = None
        for child in element:
            if _local_name(child.tag) == "code":
                code = (child.text or "").strip()
            elif _local_name(child.tag) == "text":
                text = (child.text or "").strip()
        return code, text
    return None, None


def _retry_after_seconds(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header into seconds, or ``None`` if unusable."""
    if value is None:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except TypeError, ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _check(response: requests.Response) -> bytes:
    """Return the body, or raise the error the response describes.

    Raises
    ------
    EntsoeAuthError
        On 401 or 403.
    EntsoeRateLimitError
        On 429.
    EntsoeServerError
        On 408, any 5xx, or an empty HTTP 200 response.
    NoMatchingDataError
        On HTTP 200 with an acknowledgement carrying reason code 999.
    EntsoeRequestError
        When the body is any other acknowledgement, or the status is any
        unexpected non-200 response.
    """
    status = response.status_code

    # Status is checked before the body on purpose. Reason code 999 is
    # overloaded: the guide documents it as "No matching data found", but a
    # rejected token comes back as 401 carrying that same 999, with the text
    # "Authentication failed." Reading the body first would file a bad token
    # as an empty result, and a scheduled run would then write nothing every
    # day without ever raising an error.
    if status in (401, 403):
        raise EntsoeAuthError(
            f"HTTP {status}: security token rejected (missing, invalid or suspended)"
        )
    if status == 429:
        raise EntsoeRateLimitError(
            "HTTP 429: rate limited by the platform",
            _retry_after_seconds(response.headers.get("Retry-After")),
        )
    if status == 408 or status >= 500:
        raise EntsoeServerError(f"HTTP {status}: {_summarise(response.content)}")

    reason = _acknowledgement_reason(response.content)
    if reason is not None:
        code, text = reason
        message = f"HTTP {status}: ENTSO-E returned no document (reason {code}): {text}"
        if status == 200 and code == _NO_MATCHING_DATA_CODE:
            raise NoMatchingDataError(message, code, text)
        raise EntsoeRequestError(message, code, text)

    if status != 200:
        raise EntsoeRequestError(
            f"unexpected HTTP {status}: {_summarise(response.content)}"
        )
    if not response.content:
        raise EntsoeServerError("HTTP 200: empty response body")
    content_type = response.headers.get("Content-Type", "").partition(";")[0].strip()
    if content_type.lower() == "text/html":
        raise EntsoeServerError(
            f"HTTP 200: unexpected HTML response: {_summarise(response.content)}"
        )

    return response.content


class _RateLimiter:
    """Holds requests under a per-minute ceiling, paced out across the minute.

    Every client in this process that uses the same token shares one timestamp
    window. Access is synchronized so simultaneous callers cannot all spend
    the same slot. This is still defence in depth rather than a distributed
    cap: ENTSO-E aggregates every host using the token, so the deployment also
    constrains Lambda concurrency to one.

    Two windows, because the platform documents two ceilings. The minute is
    the hard 400-a-minute limit, held below with headroom. The second is its
    separate advice to average six or seven a second rather than spend a whole
    minute's budget in one burst; a window rather than a fixed gap between
    requests, so a short burst still goes straight through, as the same advice
    allows.
    """

    _lock = Lock()
    _recent_by_token: ClassVar[dict[bytes, deque[float]]] = {}

    def __init__(self, token: str, max_per_minute: int, max_per_second: int) -> None:
        self._limits = ((_WINDOW_SECONDS, max_per_minute), (1.0, max_per_second))
        token_key = sha256(token.encode()).digest()
        with self._lock:
            self._recent = self._recent_by_token.setdefault(token_key, deque())

    def acquire(self, deadline: float) -> None:
        """Block until a request fits, without waiting beyond ``deadline``."""
        while True:
            with self._lock:
                now = monotonic()
                delay = self._delay(now)
                if delay <= 0:
                    self._recent.append(now)
                    return
            if now + delay > deadline:
                raise EntsoeRateLimitError(
                    "local rate limit cannot be cleared inside the call budget",
                    delay,
                )
            logger.warning("local rate limit reached, pausing %.1fs", delay)
            sleep(delay)

    def _delay(self, now: float) -> float:
        """Seconds until another request fits, dropping what has aged out."""
        while self._recent and now - self._recent[0] >= _WINDOW_SECONDS:
            self._recent.popleft()
        delay = 0.0
        for span, ceiling in self._limits:
            # Timestamps run oldest-first, so the ceiling-th newest is the one
            # that has to age out of ``span`` before there is room again.
            index = len(self._recent) - ceiling
            if index >= 0 and (age := now - self._recent[index]) < span:
                delay = max(delay, span - age)
        return delay


class EntsoeClient:
    """Downloads documents from the ENTSO-E Transparency Platform.

    Parameters
    ----------
    security_token
        Transparency Platform API token. Sent as a query parameter, so it ends
        up in the request URL; the client redacts it from every log line and
        error message it produces.
    base_url
        Endpoint to query. Defaults to the documented production endpoint.
    session
        Pre-built session, mainly for tests. One is created if omitted, and
        reused across calls so warm invocations skip the TLS handshake.
    connect_timeout, read_timeout
        Per-attempt socket timeouts, in seconds. The platform allows itself
        300s per request, so a read timeout is a judgement about when a slow
        response stops being worth waiting for, not a limit it will respect.
    max_attempts
        Total attempts per call, including the first.
    backoff_base, backoff_max
        Bounds for exponential backoff, in seconds, before jitter.
    total_timeout
        Wall-clock budget for one call. When local throttling or the next
        backoff would overrun it, the client stops early instead of sleeping
        past its own deadline. It does not abort a request already in flight,
        so the real worst case is this plus ``connect_timeout`` plus
        ``read_timeout``: the last attempt can start just inside the deadline
        and still run its full socket timeout. Size the Lambda timeout against
        that sum, not this alone.
    max_requests_per_minute
        Local ceiling, deliberately below the platform's documented 400.
    max_requests_per_second
        Local pace, from the platform's advice to average six or seven a
        second instead of bursting. Above ``max_requests_per_minute / 60`` on
        purpose: it flattens bursts, and leaves the minute the binding limit
        for sustained load.
    """

    def __init__(
        self,
        security_token: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        session: requests.Session | None = None,
        connect_timeout: float = 5.0,
        read_timeout: float = 60.0,
        max_attempts: int = 4,
        backoff_base: float = 1.0,
        backoff_max: float = 30.0,
        total_timeout: float = 180.0,
        max_requests_per_minute: int = 350,
        max_requests_per_second: int = 6,
    ) -> None:
        if not security_token.strip():
            raise ValueError("security_token must not be empty")
        endpoint = urlsplit(base_url)
        if endpoint.scheme.lower() != "https" or not endpoint.netloc:
            raise ValueError("base_url must be an absolute HTTPS URL")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        positive_values = {
            "connect_timeout": connect_timeout,
            "read_timeout": read_timeout,
            "backoff_base": backoff_base,
            "backoff_max": backoff_max,
            "total_timeout": total_timeout,
        }
        for name, value in positive_values.items():
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")
        if backoff_max < backoff_base:
            raise ValueError(
                "backoff_max must be greater than or equal to backoff_base"
            )
        if not 1 <= max_requests_per_minute <= _PLATFORM_REQUESTS_PER_MINUTE:
            raise ValueError(
                "max_requests_per_minute must be between 1 and "
                f"{_PLATFORM_REQUESTS_PER_MINUTE}"
            )
        if not 1 <= max_requests_per_second <= _RECOMMENDED_REQUESTS_PER_SECOND:
            raise ValueError(
                "max_requests_per_second must be between 1 and "
                f"{_RECOMMENDED_REQUESTS_PER_SECOND}"
            )

        self._token = security_token
        self._base_url = base_url
        self._session = requests.Session() if session is None else session
        self._timeout = (connect_timeout, read_timeout)
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._total_timeout = total_timeout
        self._limiter = _RateLimiter(
            security_token, max_requests_per_minute, max_requests_per_second
        )

    def get(self, params: Mapping[str, str]) -> bytes:
        """Fetch one document, retrying transient failures.

        Parameters
        ----------
        params
            Query parameters describing the document. The security token is
            added here; do not pass it in.

        Returns
        -------
        bytes
            The response body verbatim: an XML market document, or a ZIP
            archive when the platform judged the result set too large.

        Raises
        ------
        EntsoeTransientError
            Connection failure, 408, 5xx, or empty 200 that survived every
            attempt; a 429 is retried only when it named a ``Retry-After``.
            A local throttle fails immediately if its wait exceeds the budget.
        EntsoeAuthError
            The token was rejected.
        NoMatchingDataError
            The query was valid but matched no data.
        EntsoeRequestError
            The query itself was rejected.
        """
        deadline = monotonic() + self._total_timeout
        query = {**params, "securityToken": self._token}
        attempt = 0

        while True:
            self._limiter.acquire(deadline)
            try:
                return self._attempt(query)
            except EntsoeTransientError as exc:
                attempt += 1
                if attempt >= self._max_attempts:
                    logger.error("giving up after %d attempts: %s", attempt, exc)
                    raise
                if isinstance(exc, EntsoeRateLimitError) and exc.retry_after is None:
                    # A 429 with no Retry-After most likely means the token is
                    # banned, which lasts about ten minutes. Backing off for
                    # seconds cannot outlast that, and every retry adds to the
                    # request count that caused it.
                    logger.error("giving up: %s (no Retry-After to honour)", exc)
                    raise
                delay = self._delay(attempt, exc.retry_after)
                if monotonic() + delay > deadline:
                    logger.error(
                        "giving up: %s (waiting %.1fs would overrun the budget)",
                        exc,
                        delay,
                    )
                    raise
                logger.warning(
                    "%s -- retrying in %.1fs (attempt %d of %d)",
                    exc,
                    delay,
                    attempt + 1,
                    self._max_attempts,
                )
                sleep(delay)
                if monotonic() >= deadline:
                    logger.error("giving up: %s (call budget exhausted)", exc)
                    raise

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()

    def __enter__(self) -> Self:
        """Return the client itself, for use as a context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the session on leaving the context."""
        self.close()

    def _attempt(self, query: Mapping[str, str]) -> bytes:
        """Perform one request and classify its outcome."""
        try:
            response = self._session.get(
                self._base_url, params=query, timeout=self._timeout
            )
        except requests.RequestException as exc:
            # requests puts the request URL -- token included -- into the text
            # of its exceptions. Build a sanitized replacement here, then
            # raise it after leaving the except block so the original cannot
            # survive as an implicit __context__.
            message = f"{type(exc).__name__}: {_redact(str(exc))}"
            if isinstance(
                exc,
                (
                    requests.ConnectionError,
                    requests.Timeout,
                    requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ContentDecodingError,
                ),
            ):
                error: EntsoeError = EntsoeConnectionError(message)
            else:
                error = EntsoeRequestError(f"request failed before sending: {message}")
        else:
            logger.info(
                "GET %s -> %d (%d bytes)",
                _redact(response.url),
                response.status_code,
                len(response.content),
            )
            return _check(response)
        raise error from None

    def _delay(self, attempt: int, retry_after: float | None) -> float:
        """Seconds to wait before ``attempt`` + 1, honouring ``Retry-After``."""
        if retry_after is not None:
            # Floored: a header of 0, or a date that has already passed, would
            # otherwise send the next request off with no pause at all.
            return max(retry_after, self._backoff_base)
        ceiling = min(self._backoff_max, self._backoff_base * 2.0 ** (attempt - 1))
        # Full jitter: spreads concurrent invocations instead of synchronising
        # their retries into a second burst.
        return ceiling * random.uniform(0.5, 1.0)
