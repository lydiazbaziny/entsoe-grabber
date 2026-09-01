"""GET-only HTTP client for the ENTSO-E Transparency Platform RESTful API.

The platform serves market data as XML documents from a single endpoint,
selected entirely by query parameters. This module owns the transport concerns
-- authentication, timeouts, retries and rate-limit responses -- and hands back
validated XML bytes. Domain parsing belongs elsewhere.

Two shapes of body are normal on success. Most queries answer with an XML
market document; queries whose result set the platform considers too large
answer with a ZIP archive containing one or more XML documents.
:meth:`EntsoeClient.get` normalizes both shapes to a tuple of XML byte strings.
"""

import logging
import random
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from io import BytesIO
from math import isfinite
from time import monotonic, sleep
from types import TracebackType
from typing import Self
from urllib.parse import urlsplit
from xml.etree import ElementTree
from zipfile import BadZipFile, LargeZipFile, ZipFile, is_zipfile

import requests

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://web-api.tp.entsoe.eu/api"

type XmlDocuments = tuple[bytes, ...]

# Reason code returned for both empty results and rejected requests. The HTTP
# status disambiguates it: 200 means no data, while 400 means an invalid query.
_NO_MATCHING_DATA_CODE = "999"

_ACKNOWLEDGEMENT_MARKER = b"Acknowledgement_MarketDocument"
_ACKNOWLEDGEMENT_SNIFF_BYTES = 2048
_TOKEN_PATTERN = re.compile(r"(securityToken=)[^&\s]*", re.IGNORECASE)
_SUMMARY_LIMIT = 200


class EntsoeError(Exception):
    """Base class for every error raised by :class:`EntsoeClient`."""


class EntsoeTransientError(EntsoeError):
    """A failure that may resolve on its own.

    Usually raised after the retry budget is spent. A rate-limit response with
    no usable delay fails immediately instead. Callers can treat it as "try
    again on the next scheduled run" rather than "the query is wrong".

    Parameters
    ----------
    message
        Human-readable description, already stripped of the security token.
    retry_after
        Delay the platform asked for, in seconds.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class EntsoeConnectionError(EntsoeTransientError):
    """The request never produced a response."""


class EntsoeServerError(EntsoeTransientError):
    """The platform timed out, answered 5xx, or returned an unusable success.

    Unusable successes include empty or HTML bodies, malformed XML, and ZIP
    archives that are invalid, empty, or contain anything other than XML.
    """


class EntsoeRateLimitError(EntsoeTransientError):
    """The platform answered 429.

    ENTSO-E allows 400 requests per minute, counted per security token rather
    than per IP: the R3 API dropped IP-based limiting, so everything sharing a
    token shares one budget. Exceeding it can ban the token for about ten
    minutes, which outlives any sensible Lambda timeout. The client therefore
    retries only when the platform named a ``Retry-After`` it can sit out
    inside the budget, and gives up at once otherwise -- blind retries cannot
    outlast a ban, and each one spends more of the budget that earned it.
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


def _xml_documents(content: bytes) -> XmlDocuments:
    """Return well-formed XML documents from an XML body or ZIP archive."""
    source = BytesIO(content)
    names: tuple[str, ...]

    if is_zipfile(source):
        source.seek(0)
        try:
            with ZipFile(source) as archive:
                members = tuple(
                    info for info in archive.infolist() if not info.is_dir()
                )
                if not members:
                    raise EntsoeServerError(
                        "HTTP 200: ZIP archive contains no XML documents"
                    )
                invalid = tuple(
                    info.filename
                    for info in members
                    if not info.filename.lower().endswith(".xml")
                )
                if invalid:
                    raise EntsoeServerError(
                        "HTTP 200: ZIP archive contains non-XML content: "
                        + ", ".join(invalid)
                    )
                names = tuple(info.filename for info in members)
                documents = tuple(archive.read(info) for info in members)
        except EntsoeServerError:
            raise
        except (BadZipFile, LargeZipFile, OSError, RuntimeError, ValueError) as exc:
            raise EntsoeServerError(
                f"HTTP 200: unusable ZIP archive ({type(exc).__name__})"
            ) from None
    else:
        names = ("response body",)
        documents = (content,)

    for name, document in zip(names, documents, strict=True):
        try:
            ElementTree.fromstring(document)
        except ElementTree.ParseError as exc:
            raise EntsoeServerError(
                f"HTTP 200: {name} is not well-formed XML: {exc}"
            ) from None

        reason = _acknowledgement_reason(document)
        if reason is None:
            continue
        code, text = reason
        message = f"HTTP 200: ENTSO-E returned no document (reason {code}): {text}"
        if code == _NO_MATCHING_DATA_CODE:
            raise NoMatchingDataError(message, code, text)
        raise EntsoeRequestError(message, code, text)

    return documents


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


class EntsoeClient:
    """Download validated XML documents from the ENTSO-E Transparency Platform.

    The platform may answer with one XML document or a ZIP archive containing
    several. :meth:`get` normalizes both forms to an immutable tuple of XML byte
    strings. Archives are read in memory, and no response is returned unless
    every document is well-formed XML.

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
        Wall-clock budget for one call. When the next backoff would overrun it,
        the client stops early instead of sleeping past its own deadline. It
        does not abort a request already in flight, so the real worst case is
        this plus ``connect_timeout`` plus ``read_timeout``: the last attempt
        can start just inside the deadline and still run its full socket
        timeout. Size the Lambda timeout against that sum, not this alone.
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

        self._token = security_token
        self._base_url = base_url
        self._session = requests.Session() if session is None else session
        self._timeout = (connect_timeout, read_timeout)
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._total_timeout = total_timeout

    def get(self, params: Mapping[str, str]) -> XmlDocuments:
        """Fetch and validate all XML documents for one API query.

        Parameters
        ----------
        params
            Query parameters describing the requested documents. The token is
            added here; do not pass it in.

        Returns
        -------
        tuple of bytes
            One or more well-formed XML byte strings in response order. Direct
            XML produces one item; ZIP content produces one item per member.

        Raises
        ------
        EntsoeTransientError
            A connection failure, 408, 5xx, or unusable HTTP 200 response that
            survived every attempt. Unusable responses include empty, HTML, or
            malformed bodies and invalid or non-XML ZIP content. A 429 is
            retried only when it provides a usable ``Retry-After`` value.
        EntsoeAuthError
            The token was rejected.
        NoMatchingDataError
            The query was valid but matched no data.
        EntsoeRequestError
            The query was rejected or the platform returned another negative
            acknowledgement.
        """
        query = {**params, "securityToken": self._token}
        return self._request_with_retries(query)

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

    def _request_with_retries(self, query: Mapping[str, str]) -> XmlDocuments:
        """Run one logical request within its attempt and time budgets."""
        deadline = monotonic() + self._total_timeout

        for attempt in range(1, self._max_attempts + 1):
            try:
                return self._request_once(query)
            except EntsoeTransientError as error:
                if attempt == self._max_attempts:
                    logger.error("giving up after %d attempts: %s", attempt, error)
                    raise
                if (
                    isinstance(error, EntsoeRateLimitError)
                    and error.retry_after is None
                ):
                    # A 429 with no Retry-After most likely means the token is
                    # banned, which lasts about ten minutes. Backing off for
                    # seconds cannot outlast that, and every retry adds to the
                    # request count that caused it.
                    logger.error("giving up: %s (no Retry-After to honour)", error)
                    raise
                delay = self._retry_delay(attempt, error)
                self._wait_before_retry(error, delay, deadline, attempt)

        raise AssertionError("retry loop exhausted without returning or raising")

    def _request_once(self, query: Mapping[str, str]) -> XmlDocuments:
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
            return _xml_documents(_check(response))
        raise error from None

    def _retry_delay(self, attempt: int, error: EntsoeTransientError) -> float:
        """Seconds to wait before ``attempt`` + 1, honouring ``Retry-After``."""
        if error.retry_after is not None:
            # Floored: a header of 0, or a date that has already passed, would
            # otherwise send the next request off with no pause at all.
            return max(error.retry_after, self._backoff_base)
        ceiling = min(self._backoff_max, self._backoff_base * 2.0 ** (attempt - 1))
        # Full jitter: spreads concurrent invocations instead of synchronising
        # their retries into a second burst.
        return ceiling * random.uniform(0.5, 1.0)

    def _wait_before_retry(
        self,
        error: EntsoeTransientError,
        delay: float,
        deadline: float,
        attempt: int,
    ) -> None:
        """Wait for the next attempt, unless doing so exhausts the call budget."""
        if monotonic() + delay > deadline:
            logger.error(
                "giving up: %s (waiting %.1fs would overrun the budget)",
                error,
                delay,
            )
            raise error
        logger.warning(
            "%s -- retrying in %.1fs (attempt %d of %d)",
            error,
            delay,
            attempt + 1,
            self._max_attempts,
        )
        sleep(delay)
        if monotonic() >= deadline:
            logger.error("giving up: %s (call budget exhausted)", error)
            raise error
