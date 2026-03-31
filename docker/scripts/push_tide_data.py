#!/usr/bin/env python3
"""
Hourly tide-data ingestion script for Skywind Infra.

This script reads tide forecast rows from a local PostgreSQL database on the
remote server, validates and normalizes the data, then upserts batches into the
Supabase REST API for public.forecast_data.

It is designed for the Clearwater Beach use case described in task P1.4, while
keeping the required identifiers configurable through environment variables.

Examples
--------
Dry-run against the bundled sample payload:

    python3 push_tide_data.py \
      --source-json sample_data/push_tide_data_sample.json \
      --dry-run

Run against a local PostgreSQL source database:

    export SOURCE_DATABASE_DSN='postgresql://user:pass@127.0.0.1:5432/source_db'
    export SOURCE_QUERY_FILE='/opt/skywind/push_tide_data.sql'
    export SUPABASE_URL='https://dashboard.skywindsolutions.com'
    export SUPABASE_SERVICE_ROLE_KEY='...'
    export TARGET_GROUP_ID='grp_clearwater'
    python3 push_tide_data.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_LOCID = "8726724"
DEFAULT_LOCNAME = "Clearwater Beach"
DEFAULT_MODEL = "tide_hourly"
DEFAULT_TARGET_TABLE = "forecast_data"
DEFAULT_UNITS = "ft"
DEFAULT_BATCH_SIZE = 250
DEFAULT_MAX_RETRIES = 4
DEFAULT_RETRY_DELAY_SECONDS = 2.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
DEFAULT_LOG_LEVEL = "INFO"

NUMERIC_FIELDS = (
    "astrotide",
    "prelimtide",
    "tidemean",
    "tidelb",
    "tideub",
)
RETRYABLE_HTTP_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}


class ValidationError(ValueError):
    """Raised when a source row cannot be transformed into a valid target row."""


@dataclass(frozen=True)
class Config:
    source_dsn: str | None
    source_query: str | None
    source_json: Path | None
    supabase_url: str
    service_role_key: str
    target_group_id: str
    target_model: str
    target_locid: str
    target_locname: str
    target_units: str
    target_table: str
    batch_size: int
    max_retries: int
    retry_delay_seconds: float
    request_timeout_seconds: int
    dry_run: bool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Push hourly tide forecast rows from a local PostgreSQL source into Supabase."
    )
    parser.add_argument(
        "--source-dsn",
        default=os.environ.get("SOURCE_DATABASE_DSN"),
        help="PostgreSQL DSN for the local source database.",
    )
    parser.add_argument(
        "--source-query-file",
        default=os.environ.get("SOURCE_QUERY_FILE"),
        help="Path to the SQL query file used to fetch source rows.",
    )
    parser.add_argument(
        "--source-json",
        default=os.environ.get("SOURCE_JSON_FILE"),
        help="Optional JSON file for dry-run/testing instead of a database query.",
    )
    parser.add_argument(
        "--supabase-url",
        default=os.environ.get("SUPABASE_URL", ""),
        help="Supabase base URL, for example https://dashboard.skywindsolutions.com.",
    )
    parser.add_argument(
        "--service-role-key",
        default=os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""),
        help="Supabase service-role key used for ingestion writes.",
    )
    parser.add_argument(
        "--target-group-id",
        default=os.environ.get("TARGET_GROUP_ID", ""),
        help="group_id to write on each target row.",
    )
    parser.add_argument(
        "--target-model",
        default=os.environ.get("TARGET_MODEL", DEFAULT_MODEL),
        help=f"Target model name. Default: {DEFAULT_MODEL}.",
    )
    parser.add_argument(
        "--target-locid",
        default=os.environ.get("TARGET_LOCID", DEFAULT_LOCID),
        help=f"Target location ID. Default: {DEFAULT_LOCID}.",
    )
    parser.add_argument(
        "--target-locname",
        default=os.environ.get("TARGET_LOCNAME", DEFAULT_LOCNAME),
        help=f"Target location name. Default: {DEFAULT_LOCNAME}.",
    )
    parser.add_argument(
        "--target-units",
        default=os.environ.get("TARGET_UNITS", DEFAULT_UNITS),
        help=f"Units to store on target rows. Default: {DEFAULT_UNITS}.",
    )
    parser.add_argument(
        "--target-table",
        default=os.environ.get("TARGET_TABLE", DEFAULT_TARGET_TABLE),
        help=f"Supabase REST table name. Default: {DEFAULT_TARGET_TABLE}.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(os.environ.get("BATCH_SIZE", DEFAULT_BATCH_SIZE)),
        help=f"Rows per REST request. Default: {DEFAULT_BATCH_SIZE}.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=int(os.environ.get("MAX_RETRIES", DEFAULT_MAX_RETRIES)),
        help=f"Retry attempts for failed API writes. Default: {DEFAULT_MAX_RETRIES}.",
    )
    parser.add_argument(
        "--retry-delay-seconds",
        type=float,
        default=float(os.environ.get("RETRY_DELAY_SECONDS", DEFAULT_RETRY_DELAY_SECONDS)),
        help=(
            "Base retry delay in seconds. Backoff grows linearly per attempt. "
            f"Default: {DEFAULT_RETRY_DELAY_SECONDS}."
        ),
    )
    parser.add_argument(
        "--request-timeout-seconds",
        type=int,
        default=int(
            os.environ.get(
                "REQUEST_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS
            )
        ),
        help=(
            "HTTP timeout in seconds for Supabase requests. "
            f"Default: {DEFAULT_REQUEST_TIMEOUT_SECONDS}."
        ),
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", DEFAULT_LOG_LEVEL),
        help=f"Logging level. Default: {DEFAULT_LOG_LEVEL}.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and normalize rows without sending anything to Supabase.",
    )
    return parser


def configure_logging(level_name: str) -> logging.Logger:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return logging.getLogger("push_tide_data")


def parse_config(args: argparse.Namespace) -> Config:
    source_json = Path(args.source_json).expanduser() if args.source_json else None
    source_query = None
    if args.source_query_file:
        source_query = Path(args.source_query_file).expanduser().read_text(encoding="utf-8")

    if not args.source_dsn and not source_json:
        raise SystemExit(
            "Either --source-dsn/ SOURCE_DATABASE_DSN or --source-json/ SOURCE_JSON_FILE is required."
        )
    if not source_query and not source_json:
        raise SystemExit(
            "A source SQL query is required when reading from PostgreSQL. Set --source-query-file or SOURCE_QUERY_FILE."
        )
    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be greater than zero.")
    if args.max_retries < 1:
        raise SystemExit("--max-retries must be at least 1.")
    if args.retry_delay_seconds <= 0:
        raise SystemExit("--retry-delay-seconds must be greater than zero.")
    if args.request_timeout_seconds <= 0:
        raise SystemExit("--request-timeout-seconds must be greater than zero.")

    supabase_url = args.supabase_url.rstrip("/")
    service_role_key = args.service_role_key.strip()
    if not args.dry_run:
        if not supabase_url:
            raise SystemExit("SUPABASE_URL is required unless --dry-run is used.")
        if not service_role_key:
            raise SystemExit(
                "SUPABASE_SERVICE_ROLE_KEY is required unless --dry-run is used."
            )
    if not args.target_group_id:
        raise SystemExit("TARGET_GROUP_ID is required.")

    return Config(
        source_dsn=args.source_dsn,
        source_query=source_query,
        source_json=source_json,
        supabase_url=supabase_url,
        service_role_key=service_role_key,
        target_group_id=args.target_group_id,
        target_model=args.target_model,
        target_locid=str(args.target_locid),
        target_locname=args.target_locname,
        target_units=args.target_units,
        target_table=args.target_table,
        batch_size=args.batch_size,
        max_retries=args.max_retries,
        retry_delay_seconds=args.retry_delay_seconds,
        request_timeout_seconds=args.request_timeout_seconds,
        dry_run=args.dry_run,
    )


def load_source_rows(config: Config) -> list[dict[str, Any]]:
    if config.source_json:
        payload = json.loads(config.source_json.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise SystemExit("The source JSON file must contain a JSON array of objects.")
        return [coerce_mapping(row) for row in payload]

    return fetch_rows_from_postgres(config.source_dsn or "", config.source_query or "")


def fetch_rows_from_postgres(dsn: str, query: str) -> list[dict[str, Any]]:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise SystemExit(
            "psycopg is required for PostgreSQL reads. Install it with: pip install 'psycopg[binary]'"
        ) from exc

    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(query)
            return [coerce_mapping(row) for row in cursor.fetchall()]


def coerce_mapping(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise SystemExit(f"Expected each source row to be an object, got {type(row).__name__}.")
    return dict(row)


def normalize_rows(rows: Iterable[dict[str, Any]], config: Config, logger: logging.Logger) -> list[dict[str, Any]]:
    normalized_rows: list[dict[str, Any]] = []
    rejected_rows = 0

    for index, row in enumerate(rows, start=1):
        try:
            normalized_rows.append(normalize_row(row, config))
        except ValidationError as exc:
            rejected_rows += 1
            logger.warning("row_rejected row_number=%s reason=%s", index, exc)

    logger.info(
        "row_validation_complete accepted=%s rejected=%s",
        len(normalized_rows),
        rejected_rows,
    )
    return deduplicate_rows(normalized_rows, logger)


def normalize_row(row: dict[str, Any], config: Config) -> dict[str, Any]:
    forecastdtutc = parse_datetime(first_value(row, "forecastdtutc", "dtg_gmt"), "forecastdtutc")

    timestamp_raw = first_value(
        row,
        "timestamp",
        "valid_time",
        "forecast_time",
        default=forecastdtutc,
    )
    timestamp_value = parse_datetime(timestamp_raw, "timestamp")

    fhr_raw = first_value(row, "fhr", "forecast_hour", default=None)
    if fhr_raw is None:
        delta_hours = (timestamp_value - forecastdtutc).total_seconds() / 3600
        if delta_hours < 0:
            raise ValidationError("timestamp must not be earlier than forecastdtutc")
        if not math.isclose(delta_hours, round(delta_hours), abs_tol=1e-6):
            raise ValidationError(
                "derived forecast hour is not a whole number; provide source fhr explicitly"
            )
        fhr = int(round(delta_hours))
    else:
        fhr = parse_integer(fhr_raw, "fhr")
        if fhr < 0:
            raise ValidationError("fhr must be zero or greater")

    datum = parse_required_text(first_value(row, "datum"), "datum")

    numeric_values = {
        "astrotide": parse_float(
            first_value(row, "astrotide", "astronomical_forecast_fd"), "astrotide"
        ),
        "prelimtide": parse_float(
            first_value(row, "prelimtide", "tidal_observations_ft"), "prelimtide"
        ),
        "tidemean": parse_float(first_value(row, "tidemean"), "tidemean"),
        "tidelb": parse_float(first_value(row, "tidelb"), "tidelb"),
        "tideub": parse_float(first_value(row, "tideub"), "tideub"),
    }

    validate_tide_values(numeric_values)

    startdt_raw = first_value(row, "startdt", default=None)
    startdt = parse_datetime(startdt_raw, "startdt") if startdt_raw is not None else None

    target_row = {
        "group_id": config.target_group_id,
        "timestamp": timestamp_value.isoformat(),
        "locid": config.target_locid,
        "locname": config.target_locname,
        "forecastdtutc": forecastdtutc.isoformat(),
        "model": config.target_model,
        "fhr": fhr,
        "datum": datum,
        "units": config.target_units,
        **numeric_values,
    }
    if startdt is not None:
        target_row["startdt"] = startdt.isoformat()

    return target_row


def first_value(row: dict[str, Any], *keys: str, default: Any = ... ) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    if default is not ...:
        return default
    raise ValidationError(f"missing required source field; checked aliases={', '.join(keys)}")


def parse_datetime(value: Any, field_name: str) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = dt.datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError(f"{field_name} must be ISO-8601-compatible, got {value!r}") from exc
    else:
        raise ValidationError(f"{field_name} must be a datetime or ISO string, got {value!r}")

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def parse_required_text(value: Any, field_name: str) -> str:
    if value is None:
        raise ValidationError(f"{field_name} is required")
    text = str(value).strip()
    if not text:
        raise ValidationError(f"{field_name} must not be blank")
    return text


def parse_float(value: Any, field_name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name} must be numeric, got {value!r}") from exc
    if math.isnan(parsed) or math.isinf(parsed):
        raise ValidationError(f"{field_name} must be a finite number")
    return parsed


def parse_integer(value: Any, field_name: str) -> int:
    try:
        if isinstance(value, str) and value.strip() == "":
            raise ValueError("blank")
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name} must be an integer, got {value!r}") from exc
    return parsed


def validate_tide_values(values: dict[str, float]) -> None:
    for field_name in NUMERIC_FIELDS:
        value = values[field_name]
        if value < -100 or value > 100:
            raise ValidationError(f"{field_name} is outside the allowed range (-100 to 100 ft)")

    if values["tidelb"] > values["tidemean"]:
        raise ValidationError("tidelb must be less than or equal to tidemean")
    if values["tidemean"] > values["tideub"]:
        raise ValidationError("tidemean must be less than or equal to tideub")


def deduplicate_rows(rows: list[dict[str, Any]], logger: logging.Logger) -> list[dict[str, Any]]:
    deduplicated: dict[tuple[str, str, str, str, str, int], dict[str, Any]] = {}
    for row in rows:
        key = (
            row["group_id"],
            row["locid"],
            row["forecastdtutc"],
            row["timestamp"],
            row["model"],
            row["fhr"],
        )
        deduplicated[key] = row

    removed = len(rows) - len(deduplicated)
    if removed:
        logger.info("duplicate_rows_removed count=%s", removed)
    return list(deduplicated.values())


def chunked(rows: list[dict[str, Any]], batch_size: int) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(rows), batch_size):
        yield rows[start : start + batch_size]


def push_batches(rows: list[dict[str, Any]], config: Config, logger: logging.Logger) -> int:
    if not rows:
        logger.info("no_valid_rows_to_push")
        return 0

    endpoint = build_upsert_url(config)
    total_pushed = 0
    for batch_number, batch in enumerate(chunked(rows, config.batch_size), start=1):
        logger.info("batch_push_start batch_number=%s batch_size=%s", batch_number, len(batch))
        send_batch(batch, endpoint, config, logger)
        total_pushed += len(batch)
        logger.info("batch_push_complete batch_number=%s total_pushed=%s", batch_number, total_pushed)
    return total_pushed


def build_upsert_url(config: Config) -> str:
    on_conflict = urllib.parse.quote(
        "group_id,locid,forecastdtutc,timestamp,model,fhr", safe="," 
    )
    return f"{config.supabase_url}/rest/v1/{config.target_table}?on_conflict={on_conflict}"


def send_batch(
    batch: list[dict[str, Any]],
    endpoint: str,
    config: Config,
    logger: logging.Logger,
) -> None:
    payload = json.dumps(batch).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "apikey": config.service_role_key,
        "Authorization": f"Bearer {config.service_role_key}",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }

    for attempt in range(1, config.max_retries + 1):
        request = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(
                request, timeout=config.request_timeout_seconds
            ) as response:
                if 200 <= response.status < 300:
                    return
                raise RuntimeError(f"unexpected HTTP status {response.status}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code in RETRYABLE_HTTP_STATUSES and attempt < config.max_retries:
                delay = config.retry_delay_seconds * attempt
                logger.warning(
                    "batch_push_retry attempt=%s status=%s delay_seconds=%s body=%s",
                    attempt,
                    exc.code,
                    delay,
                    body[:500],
                )
                time.sleep(delay)
                continue
            raise RuntimeError(
                f"Supabase request failed with HTTP {exc.code}: {body}"
            ) from exc
        except urllib.error.URLError as exc:
            if attempt < config.max_retries:
                delay = config.retry_delay_seconds * attempt
                logger.warning(
                    "batch_push_retry attempt=%s reason=%s delay_seconds=%s",
                    attempt,
                    exc.reason,
                    delay,
                )
                time.sleep(delay)
                continue
            raise RuntimeError(f"Supabase request failed: {exc.reason}") from exc


def log_dry_run_preview(rows: list[dict[str, Any]], logger: logging.Logger) -> None:
    preview = rows[: min(3, len(rows))]
    logger.info("dry_run_preview rows=%s payload=%s", len(rows), json.dumps(preview, indent=2))


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logger = configure_logging(args.log_level)
    config = parse_config(args)

    logger.info(
        "ingestion_start target_group_id=%s target_locid=%s source_mode=%s dry_run=%s",
        config.target_group_id,
        config.target_locid,
        "json" if config.source_json else "postgres",
        config.dry_run,
    )

    source_rows = load_source_rows(config)
    logger.info("source_rows_loaded count=%s", len(source_rows))

    normalized_rows = normalize_rows(source_rows, config, logger)
    if config.dry_run:
        log_dry_run_preview(normalized_rows, logger)
        return 0

    pushed = push_batches(normalized_rows, config, logger)
    logger.info("ingestion_complete pushed_rows=%s", pushed)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        logging.getLogger("push_tide_data").exception("ingestion_failed error=%s", exc)
        raise SystemExit(1) from exc