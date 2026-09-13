"""Lambda entry point.

Fetches every configured dataset from the ENTSO-E Transparency Platform and
writes it to S3: one CSV per returned document under ``data/``, plus the XML it
came from under ``raw/`` when ``STORE_RAW_XML`` says so.
"""

import json
import logging
import os
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import boto3

from entsoe_grabber.client import (
    DEFAULT_BASE_URL,
    EntsoeClient,
    NoMatchingDataError,
)
from entsoe_grabber.serializer import to_csv

logger = logging.getLogger(__name__)
logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Built during the init phase, not per invocation, so warm starts reuse the
# clients and their connection pools.
s3_client = boto3.client("s3")
ssm_client = boto3.client("ssm")

# Terraform passes booleans through `tostring`, so "true"/"false" is what
# actually arrives. The other values are here so that a hand-set variable
# behaves as expected too.
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# The platform expects UTC instants in yyyyMMddHHmm.
_PERIOD_FORMAT = "%Y%m%d%H%M"

# Socket timeouts for a single request. The read timeout limits inactivity
# between received bytes, not the total download time, so it is an operational
# allowance and not a hard limit.
_CONNECT_TIMEOUT_SECONDS = 5.0
_READ_TIMEOUT_SECONDS = 60.0


def _utcnow() -> datetime:
    """Return the current UTC time, isolated so tests can pin it."""
    return datetime.now(UTC)


def _run_time(event: Mapping[str, Any]) -> datetime:
    """Anchor dataset dates to the original event time, converted to UTC.

    Manual events without a timestamp use the current time. Scheduled events
    must carry one, so that a delayed delivery or a retry keeps the intended
    date.

    The check below looks for the fields that an EventBridge *rule* sends.
    EventBridge Scheduler sends a user-defined payload that carries neither of
    them, so a schedule moved to Scheduler has to put a ``time`` into that
    payload itself. Without one, a delayed delivery would quietly use the
    current date instead.
    """
    if "time" not in event:
        if (
            event.get("source") == "aws.events"
            or event.get("detail-type") == "Scheduled Event"
        ):
            raise ValueError("scheduled event must include time")
        return _utcnow()

    timestamp = event["time"]
    if not isinstance(timestamp, str):
        raise ValueError("event time must be an ISO 8601 timestamp string")
    try:
        run_time = datetime.fromisoformat(timestamp)
    except ValueError as error:
        raise ValueError("event time must be a valid ISO 8601 timestamp") from error
    if run_time.tzinfo is None:
        raise ValueError("event time must include a timezone")
    return run_time.astimezone(UTC)


def _security_token() -> str:
    """Read the API token from SSM Parameter Store."""
    name = os.environ["ENTSOE_TOKEN_SSM_PARAMETER"]
    return ssm_client.get_parameter(Name=name, WithDecryption=True)["Parameter"][
        "Value"
    ]


def _datasets(datasets_json: str) -> list[tuple[str, dict[str, str], int]]:
    """Parse ``DATASETS_JSON`` into one entry per configured dataset.

    Datasets are keyed by name and carry their own ``params`` and
    ``date_offset_days``, defaulting to no parameters and no offset. Entries
    come back as (name, parameters, offset) in configuration order.
    """
    datasets: dict[str, dict[str, Any]] = json.loads(datasets_json)
    return [
        (name, dict(d.get("params", {})), int(d.get("date_offset_days", 0)))
        for name, d in datasets.items()
    ]


def _period(target: date, market_timezone: ZoneInfo) -> dict[str, str]:
    """Return the query window covering ``target``, in the format the API uses.

    This is the market day, midnight to midnight in ``market_timezone``,
    converted to UTC. The platform publishes by market day, so a UTC day would
    reach into the next market day, which is not published yet when the run
    happens, and the next run would start after it: those hours would never be
    fetched. Converting each midnight separately also gives the 23- and 25-hour
    days of a daylight saving change.
    """
    following = target + timedelta(days=1)
    start = datetime(target.year, target.month, target.day, tzinfo=market_timezone)
    end = datetime(
        following.year, following.month, following.day, tzinfo=market_timezone
    )
    return {
        "periodStart": start.astimezone(UTC).strftime(_PERIOD_FORMAT),
        "periodEnd": end.astimezone(UTC).strftime(_PERIOD_FORMAT),
    }


def _key(
    prefix: str,
    kind: str,
    dataset: str,
    target: date,
    suffix: str,
    index: int,
) -> str:
    """Build the S3 key for one artefact.

    The key is built from the dataset name and the date the data belongs to,
    never from the wall-clock time. Running a day again therefore overwrites
    that day's objects instead of adding duplicates, and a backfill lands in
    the month the data belongs to rather than the month it was fetched.

    ``index`` separates the documents of one query, because a ZIP response
    carries several. Both artefacts of one document share the same index. If a
    later run returns fewer documents, the extra keys of the earlier run stay
    behind; bucket versioning, not this function, is what makes that
    recoverable.

    Two digits, counting from zero: the platform returns at most 100 documents
    per response, so the longest key needed is ``-99``. Above that limit the
    numbers stay unique but stop sorting in order -- ``-100`` sorts between
    ``-10`` and ``-11`` -- so the width here has to grow with it.
    """
    name = f"{dataset}-{target:%Y%m%d}-{index:02d}"
    parts = [prefix, kind, f"{target:%Y}", f"{target:%m}", f"{name}.{suffix}"]
    return "/".join(part for part in parts if part)


def handler(event: Mapping[str, Any], context: object) -> dict[str, list[str]]:
    """Fetch every configured dataset and write it to S3.

    A dataset the platform holds no data for is logged and skipped, because an
    empty day is a valid answer and not a fault. Every other failure is raised,
    so a partial run fails visibly instead of reporting success.

    Parameters
    ----------
    event
        Event whose timezone-aware ISO 8601 ``time`` anchors dataset dates.
        Required for scheduled events; manual events without it use the
        current UTC time. Explicit invalid timestamps raise ``ValueError``.
    context
        The Lambda context, unused. Retry scheduling is limited by the
        client's own ``total_timeout``, and the invocation deadline is enforced
        by Lambda itself.

    Returns
    -------
    dict
        ``{"written": [key, ...], "skipped": [dataset, ...]}`` -- the keys
        created, in the order they were written, and the datasets the platform
        held no data for. The second list is what separates a dataset that was
        queried and came back empty from one that never ran at all.
    """
    bucket = os.environ["OUTPUT_BUCKET"]
    prefix = os.environ.get("OUTPUT_PREFIX", "")
    store_raw = os.environ.get("STORE_RAW_XML", "true").strip().lower() in _TRUTHY
    market_timezone = ZoneInfo(os.environ["MARKET_TIMEZONE"])
    run_time = _run_time(event)

    written: list[str] = []
    skipped: list[str] = []
    with EntsoeClient(
        _security_token(),
        os.environ.get("ENTSOE_BASE_URL", DEFAULT_BASE_URL),
        connect_timeout=_CONNECT_TIMEOUT_SECONDS,
        read_timeout=_READ_TIMEOUT_SECONDS,
    ) as client:
        for dataset, params, offset in _datasets(os.environ.get("DATASETS_JSON", "{}")):
            target = (run_time + timedelta(days=offset)).date()
            try:
                documents = client.get({**params, **_period(target, market_timezone)})
            except NoMatchingDataError as error:
                logger.info("dataset %s has no data for %s: %s", dataset, target, error)
                skipped.append(dataset)
                continue

            if store_raw:
                for index, document in enumerate(documents):
                    raw_key = _key(prefix, "raw", dataset, target, "xml", index)
                    s3_client.put_object(
                        Bucket=bucket,
                        Key=raw_key,
                        Body=document,
                        ContentType="application/xml",
                    )
                    written.append(raw_key)

            for index, document in enumerate(documents):
                csv_key = _key(prefix, "data", dataset, target, "csv", index)
                s3_client.put_object(
                    Bucket=bucket,
                    Key=csv_key,
                    Body=to_csv(document),
                    ContentType="text/csv",
                )
                written.append(csv_key)

            logger.info(
                "wrote %d CSV(s) for dataset %s and %s (raw %s)",
                len(documents),
                dataset,
                target,
                "kept" if store_raw else "discarded",
            )

    return {"written": written, "skipped": skipped}
