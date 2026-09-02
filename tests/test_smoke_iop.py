"""Live smoke tests against the ENTSO-E interoperability (IOP) environment.

Deselected by default -- ``addopts`` in pyproject.toml carries ``-m "not
smoke"``, so ``make test`` and ``make check`` stay offline and deterministic.
Run them deliberately with ``make smoke``.

These cover what a mocked test cannot: that the live platform still speaks the
protocol the client assumes. Two of them are deterministic, and two depend on
IOP holding data. IOP is sparse and its contents change, so the data-dependent
tests skip rather than fail when a query comes back empty -- an empty result is
the platform answering correctly, not the client breaking.
"""

import os
from datetime import UTC, datetime, timedelta
from xml.etree import ElementTree

import pytest

from entsoe_grabber.client import (
    EntsoeAuthError,
    EntsoeClient,
    EntsoeRequestError,
    NoMatchingDataError,
    XmlDocuments,
)

pytestmark = pytest.mark.smoke

IOP_BASE_URL = "https://web-api.tp-iop.entsoe.eu/api"

# Slovak control area (SEPS), the assignment's subject.
SK_DOMAIN = "10YSK-SEPS-----K"

# Namespace of the generation/load responses, matching the targetNamespace of
# schemas/entsoe/iec62325-451-6-generationload.xsd.
GENERATION_LOAD_NS = "urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0"

# A token of the right shape that the platform will not recognise.
UNKNOWN_TOKEN = "00000000-0000-0000-0000-000000000000"


def _window(days: int) -> dict[str, str]:
    """Return ``periodStart``/``periodEnd`` covering the last ``days`` days."""
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    return {
        "periodStart": start.strftime("%Y%m%d%H%M"),
        "periodEnd": end.strftime("%Y%m%d%H%M"),
    }


@pytest.fixture(scope="module")
def token() -> str:
    """The IOP security token, or skip the module when it is not configured."""
    value = os.environ.get("ENTSOE_SECURITY_TOKEN", "").strip()
    if not value:
        pytest.skip("ENTSOE_SECURITY_TOKEN is not set")
    return value


@pytest.fixture
def client(token: str) -> EntsoeClient:
    """A client pointed at IOP.

    ``max_attempts`` is 2 rather than the default 4: a smoke run should report
    a struggling platform quickly instead of spending the full retry budget.
    """
    return EntsoeClient(token, base_url=IOP_BASE_URL, max_attempts=2)


def _fetch(client: EntsoeClient, params: dict[str, str]) -> XmlDocuments:
    """Run a query, skipping the test when IOP holds no data for it."""
    try:
        return client.get(params)
    except NoMatchingDataError as error:
        pytest.skip(f"IOP holds no data for this query: {error}")


def test_rejected_token_raises_auth_error() -> None:
    """An unrecognised token fails as an auth error, not an empty result.

    IOP answers this with HTTP 401 whose body carries reason code 999 -- the
    same code as "no matching data". Classifying on the body first would turn a
    dead token into a silent zero-row day on every scheduled run.
    """
    with (
        EntsoeClient(UNKNOWN_TOKEN, base_url=IOP_BASE_URL, max_attempts=1) as client,
        pytest.raises(EntsoeAuthError),
    ):
        client.get(
            {
                "documentType": "A75",
                "processType": "A16",
                "in_Domain": SK_DOMAIN,
                **_window(1),
            }
        )


def test_invalid_query_is_rejected_rather_than_reported_as_empty(
    client: EntsoeClient,
) -> None:
    """A malformed query raises ``EntsoeRequestError``, never the 999 subclass.

    The platform rejects this with HTTP 400 and, again, reason code 999. The
    distinction matters: ``NoMatchingDataError`` means "record zero rows and
    carry on", while this means "the query is wrong and always will be".
    """
    with client, pytest.raises(EntsoeRequestError) as raised:
        client.get(
            {
                "documentType": "ZZZ",
                "processType": "A16",
                "in_Domain": SK_DOMAIN,
                **_window(1),
            }
        )
    assert not isinstance(raised.value, NoMatchingDataError)


def test_fetches_a_well_formed_market_document(client: EntsoeClient) -> None:
    """A real query returns a generation/load document in the expected schema.

    Asserting the namespace keeps the bundled XSD honest: if the platform moves
    to a new schema version, this fails instead of the collector quietly
    parsing a document it no longer understands.
    """
    with client:
        documents = _fetch(
            client,
            {
                "documentType": "A75",
                "processType": "A16",
                "in_Domain": SK_DOMAIN,
                **_window(1),
            },
        )

    assert documents
    root = ElementTree.fromstring(documents[0])
    assert root.tag == f"{{{GENERATION_LOAD_NS}}}GL_MarketDocument"


def test_zip_response_is_normalized_to_separate_documents(
    client: EntsoeClient,
) -> None:
    """A query answered with a ZIP yields one XML document per archive member.

    Unavailability documents over a long window are the reliable way to make
    IOP return an archive; most queries answer with a single XML body, which
    would leave the archive branch of the client untested against the platform.
    """
    with client:
        documents = _fetch(
            client,
            {
                "documentType": "A80",
                "biddingZone_Domain": SK_DOMAIN,
                **_window(365),
            },
        )

    if len(documents) == 1:
        pytest.skip("IOP answered with a single document; no archive to exercise")

    for document in documents:
        # _xml_documents already parsed these; reparsing is the assertion that
        # every member survived extraction as a usable document.
        assert ElementTree.fromstring(document).tag.endswith("MarketDocument")
