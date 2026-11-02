#!/usr/bin/env python3
"""Private health-observation receiver.

Health.md is one supported source adapter. The stored observation contract is
application-neutral and has a separate endpoint for future sources.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import tempfile
from datetime import date, datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


AUTH_TOKEN = os.environ.get("HEALTHMD_AUTH_TOKEN", "")
DATA_DIRECTORY = Path(os.environ.get("HEALTHMD_DATA_DIR", "/data"))
MAXIMUM_BODY_BYTES = int(os.environ.get("HEALTHMD_MAX_BODY_BYTES", str(64 * 1024 * 1024)))
INFLUX_URL = os.environ.get("HEALTHMD_INFLUX_URL", "http://ha-influxdb:8086").rstrip("/")
INFLUX_DATABASE = os.environ.get("HEALTHMD_INFLUX_DATABASE", "healthmd")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
CAPTURE_STATUSES = {"complete", "partial", "not_requested", "legacy_unavailable"}
# Health.md Apple daily records moved from v7 to v8 in August 2026. v8 adds
# typed provider sections; the fields this receiver reads did not change.
HEALTHMD_DAILY_RECORD_SCHEMA_VERSIONS = {7, 8}
HEALTH_OBSERVATION_MEASUREMENT = "health_observation"
HEALTH_MD_SOURCE = "health_md"
HEALTHMD_OBSERVATION_MAPPINGS = {
    "HKQuantityTypeIdentifierActiveEnergyBurned": ("active_calories", "kcal", "kcal", 1.0),
    "HKQuantityTypeIdentifierBasalEnergyBurned": ("basal_calories", "kcal", "kcal", 1.0),
    "HKQuantityTypeIdentifierDistanceWalkingRunning": ("walking_running_distance", "m", "km", 0.001),
    "HKQuantityTypeIdentifierFlightsClimbed": ("flights_climbed", "count", "count", 1.0),
    "HKQuantityTypeIdentifierHeartRate": ("heart_rate", "count/min", "bpm", 1.0),
    "HKQuantityTypeIdentifierStepCount": ("steps", "count", "count", 1.0),
    "HKQuantityTypeIdentifierWalkingAsymmetryPercentage": (
        "walking_asymmetry_percent",
        "%",
        "percent",
        1.0,
    ),
    "HKQuantityTypeIdentifierWalkingDoubleSupportPercentage": (
        "double_support_percent",
        "%",
        "percent",
        1.0,
    ),
    "HKQuantityTypeIdentifierWalkingSpeed": ("walking_speed", "m/s", "m_per_s", 1.0),
    "HKQuantityTypeIdentifierWalkingStepLength": ("step_length", "m", "cm", 100.0),
    # HealthKit stores percentage types as a 0..1 fraction under the "%" unit.
    "HKQuantityTypeIdentifierAppleWalkingSteadiness": ("walking_steadiness_percent", "%", "percent", 1.0),
    "HKQuantityTypeIdentifierHeadphoneAudioExposure": ("headphone_audio_level", "dBASPL", "dBASPL", 1.0),
    "HKQuantityTypeIdentifierBodyMass": ("weight", "kg", "kg", 1.0),
    "HKQuantityTypeIdentifierLeanBodyMass": ("lean_body_mass", "kg", "kg", 1.0),
    "HKQuantityTypeIdentifierBodyFatPercentage": ("body_fat_percent", "%", "percent", 1.0),
    "HKQuantityTypeIdentifierBodyMassIndex": ("body_mass_index", "count", "count", 1.0),
    "HKQuantityTypeIdentifierHeight": ("height", "m", "cm", 100.0),
}
# Nutrition samples keep their HealthKit unit, so each maps one-to-one.
HEALTHMD_DIETARY_MAPPINGS = {
    "DietaryEnergyConsumed": ("dietary_energy", "kcal"),
    "DietaryProtein": ("dietary_protein", "g"),
    "DietaryCarbohydrates": ("dietary_carbohydrates", "g"),
    "DietarySugar": ("dietary_sugar", "g"),
    "DietaryFiber": ("dietary_fibre", "g"),
    "DietaryFatTotal": ("dietary_fat_total", "g"),
    "DietaryFatSaturated": ("dietary_fat_saturated", "g"),
    "DietaryFatMonounsaturated": ("dietary_fat_monounsaturated", "g"),
    "DietaryFatPolyunsaturated": ("dietary_fat_polyunsaturated", "g"),
    "DietaryCholesterol": ("dietary_cholesterol", "mg"),
    "DietarySodium": ("dietary_sodium", "mg"),
    "DietaryCaffeine": ("dietary_caffeine", "mg"),
    "DietaryCalcium": ("dietary_calcium", "mg"),
    "DietaryIron": ("dietary_iron", "mg"),
    "DietaryMagnesium": ("dietary_magnesium", "mg"),
    "DietaryPotassium": ("dietary_potassium", "mg"),
    "DietaryZinc": ("dietary_zinc", "mg"),
    "DietaryFolate": ("dietary_folate", "mcg"),
    "DietaryVitaminA": ("dietary_vitamin_a", "mcg"),
    "DietaryVitaminB12": ("dietary_vitamin_b12", "mcg"),
    "DietaryVitaminC": ("dietary_vitamin_c", "mg"),
    "DietaryVitaminD": ("dietary_vitamin_d", "mcg"),
    "DietaryVitaminE": ("dietary_vitamin_e", "mg"),
    "DietaryVitaminK": ("dietary_vitamin_k", "mcg"),
}
HEALTHMD_OBSERVATION_MAPPINGS.update(
    {
        f"HKQuantityTypeIdentifier{identifier}": (metric, unit, unit, 1.0)
        for identifier, (metric, unit) in HEALTHMD_DIETARY_MAPPINGS.items()
    }
)


class ValidationError(ValueError):
    """The client did not send a supported health payload."""


def parse_date(value: Any, name: str) -> date:
    if not isinstance(value, str) or not DATE_PATTERN.fullmatch(value):
        raise ValidationError(f"{name} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValidationError(f"{name} must be an ISO date") from error


def coerce_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def parse_rfc3339_timestamp(value: Any, name: str) -> datetime:
    parsed = coerce_timestamp(value)
    if parsed is None:
        raise ValidationError(f"{name} must be an RFC 3339 timestamp with a timezone")
    return parsed


def parse_identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_PATTERN.fullmatch(value):
        raise ValidationError(f"{name} must be a lower_snake_case identifier")
    return value


def parse_unit(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 64 or any(character.isspace() for character in value):
        raise ValidationError("unit must be a non-empty token")
    return value


def require(value: Any, expected: Any, name: str) -> None:
    if value != expected:
        raise ValidationError(f"unsupported {name}")


def validate_healthmd_record(record: Any, range_start: date, range_end: date) -> str:
    if not isinstance(record, dict):
        raise ValidationError("records must contain objects")
    require(record.get("schema"), "healthmd.health_data", "daily record schema")
    if record.get("schema_version") not in HEALTHMD_DAILY_RECORD_SCHEMA_VERSIONS:
        raise ValidationError("unsupported daily record schema version")
    record_date = parse_date(record.get("date"), "record date")
    if not range_start <= record_date <= range_end:
        raise ValidationError("record date is outside date_range")
    if record.get("raw_capture_status") not in CAPTURE_STATUSES:
        raise ValidationError("unsupported raw_capture_status")

    archive = record.get("healthkit_record_archive")
    if archive is not None:
        if not isinstance(archive, dict):
            raise ValidationError("healthkit_record_archive must be an object")
        require(archive.get("schema"), "healthmd.healthkit_records", "archive schema")
        require(archive.get("schema_version"), 1, "archive schema version")
        if archive.get("capture_status") not in CAPTURE_STATUSES:
            raise ValidationError("unsupported archive capture_status")
    return record_date.isoformat()


def validate_healthmd_export(payload: Any) -> tuple[datetime, list[str]]:
    if not isinstance(payload, dict):
        raise ValidationError("request body must be a JSON object")
    require(payload.get("schema"), "healthmd.api_export", "API export schema")
    if payload.get("schema_version") not in {1, 2}:
        raise ValidationError("unsupported API export schema version")
    require(payload.get("daily_record_schema"), "healthmd.health_data", "daily record schema")
    if payload.get("daily_record_schema_version") not in HEALTHMD_DAILY_RECORD_SCHEMA_VERSIONS:
        raise ValidationError("unsupported daily record schema version")
    require(payload.get("source"), "ios", "export source")
    exported_at = parse_rfc3339_timestamp(payload.get("exported_at"), "exported_at")

    date_range = payload.get("date_range")
    if not isinstance(date_range, dict):
        raise ValidationError("date_range must be an object")
    range_start = parse_date(date_range.get("start"), "date_range.start")
    range_end = parse_date(date_range.get("end"), "date_range.end")
    if range_end < range_start:
        raise ValidationError("date_range.end is before date_range.start")

    records = payload.get("records")
    if not isinstance(records, list):
        raise ValidationError("records must be an array")
    if isinstance(payload.get("record_count"), bool) or payload.get("record_count") != len(records):
        raise ValidationError("record_count does not match records")
    if not isinstance(payload.get("failed_date_details"), list):
        raise ValidationError("failed_date_details must be an array")

    record_dates = [validate_healthmd_record(record, range_start, range_end) for record in records]
    if len(record_dates) != len(set(record_dates)):
        raise ValidationError("records contains duplicate dates")

    if payload["schema_version"] == 2:
        require(
            payload.get("external_record_schema"),
            "healthmd.external_provider_daily",
            "external record schema",
        )
        require(payload.get("external_record_schema_version"), 1, "external record schema version")
        external_records = payload.get("external_records")
        if not isinstance(external_records, list):
            raise ValidationError("external_records must be an array")
        if isinstance(payload.get("external_record_count"), bool) or payload.get("external_record_count") != len(external_records):
            raise ValidationError("external_record_count does not match external_records")

    return exported_at, record_dates


def validate_observation(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("observations must contain objects")
    metric = parse_identifier(value.get("metric"), "observation metric")
    observed_at = parse_rfc3339_timestamp(value.get("observed_at"), "observed_at")
    unit = parse_unit(value.get("unit"))
    numeric_value = value.get("value")
    if not isinstance(numeric_value, (int, float)) or isinstance(numeric_value, bool):
        raise ValidationError("observation value must be numeric")
    numeric_value = float(numeric_value)
    if not math.isfinite(numeric_value):
        raise ValidationError("observation value must be finite")
    duration_seconds = value.get("duration_seconds", 0)
    if not isinstance(duration_seconds, (int, float)) or isinstance(duration_seconds, bool):
        raise ValidationError("duration_seconds must be numeric")
    duration_seconds = float(duration_seconds)
    if not math.isfinite(duration_seconds) or duration_seconds < 0:
        raise ValidationError("duration_seconds must be finite and non-negative")
    return {
        "metric": metric,
        "observed_at": observed_at,
        "unit": unit,
        "value": numeric_value,
        "duration_seconds": duration_seconds,
    }


def validate_observation_batch(payload: Any) -> tuple[str, str, list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        raise ValidationError("request body must be a JSON object")
    require(payload.get("schema"), "health.observation_batch", "health observation schema")
    require(payload.get("schema_version"), 1, "health observation schema version")
    source = parse_identifier(payload.get("source"), "source")
    partition = parse_date(payload.get("partition"), "partition")
    observations = payload.get("observations")
    if not isinstance(observations, list):
        raise ValidationError("observations must be an array")
    if isinstance(payload.get("observation_count"), bool) or payload.get("observation_count") != len(observations):
        raise ValidationError("observation_count does not match observations")
    return source, partition.isoformat(), [validate_observation(value) for value in observations]


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=".pending-", suffix=".json")
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def current_revision(path: Path) -> datetime | None:
    try:
        with path.open("rb") as source:
            value = json.load(source).get("exported_at")
        return parse_rfc3339_timestamp(value, "exported_at")
    except (OSError, ValueError, json.JSONDecodeError, ValidationError):
        return None


def persist_revision(body: bytes) -> None:
    digest = hashlib.sha256(body).hexdigest()
    received_at = datetime.now(timezone.utc)
    revision_name = f"{received_at.strftime('%Y%m%dT%H%M%S%fZ')}-{digest}.json"
    atomic_write(DATA_DIRECTORY / "revisions" / revision_name, body)


def persist_healthmd_export(
    payload: dict[str, Any], body: bytes, exported_at: datetime, record_dates: list[str]
) -> list[dict[str, Any]]:
    persist_revision(body)

    updated_records: list[dict[str, Any]] = []
    for record, record_date in zip(payload["records"], record_dates, strict=True):
        latest_path = DATA_DIRECTORY / "latest" / f"{record_date}.json"
        revision_path = DATA_DIRECTORY / "latest-meta" / f"{record_date}.json"
        existing_exported_at = current_revision(revision_path)
        if existing_exported_at is None or exported_at >= existing_exported_at:
            atomic_write(latest_path, json.dumps(record, separators=(",", ":")).encode("utf-8"))
            atomic_write(
                revision_path,
                json.dumps({"exported_at": exported_at.isoformat()}).encode("utf-8"),
            )
            updated_records.append(record)
    return updated_records


def observation_partition_path(source: str, partition: str) -> Path:
    return DATA_DIRECTORY / "observation-sources" / source / "partitions" / f"{partition}.json"


def persist_observation_batch(payload: dict[str, Any], body: bytes, source: str, partition: str) -> None:
    persist_revision(body)
    atomic_write(observation_partition_path(source, partition), json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def parse_observation_timestamp(value: Any) -> datetime | None:
    return coerce_timestamp(value)


def sleep_field(payload: dict[str, Any]) -> str | None:
    value = payload.get("symbolic_value")
    if not isinstance(value, str):
        return None
    normalised = value.replace("_", "").lower()
    if normalised == "asleepcore":
        return "sleep_core"
    if normalised == "asleepdeep":
        return "sleep_deep"
    if normalised == "asleeprem":
        return "sleep_rem"
    if normalised.startswith("asleep"):
        return "sleep_asleep"
    if normalised in {"inbed", "inbedunknown"}:
        return "sleep_in_bed"
    if normalised.startswith("awake"):
        return "sleep_awake"
    return None


def normalise_healthmd_observations(record: dict[str, Any]) -> list[dict[str, Any]]:
    archive = record.get("healthkit_record_archive")
    if not isinstance(archive, dict):
        return []
    records = archive.get("records")
    if not isinstance(records, list):
        return []

    observations: list[dict[str, Any]] = []
    seen_record_ids: set[str] = set()
    for source_record in records:
        if not isinstance(source_record, dict):
            continue
        object_type = source_record.get("object_type_identifier")
        payload = source_record.get("payload")
        if not isinstance(object_type, str) or not isinstance(payload, dict):
            continue
        start = parse_observation_timestamp(source_record.get("start_date"))
        end = parse_observation_timestamp(source_record.get("end_date"))
        if start is None or end is None:
            continue

        record_id = source_record.get("original_uuid")
        if isinstance(record_id, str) and record_id:
            identity = f"{object_type}:{record_id}"
            if identity in seen_record_ids:
                continue
            seen_record_ids.add(identity)

        duration_seconds = max(0.0, (end - start).total_seconds())
        mapping = HEALTHMD_OBSERVATION_MAPPINGS.get(object_type)
        if mapping is not None:
            metric_name, expected_unit, unit, multiplier = mapping
            value = payload.get("value")
            if payload.get("unit") != expected_unit or not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            numeric_value = float(value) * multiplier
            if not math.isfinite(numeric_value):
                continue
            observations.append(
                {
                    "metric": metric_name,
                    "observed_at": end,
                    "unit": unit,
                    "value": numeric_value,
                    "duration_seconds": duration_seconds,
                }
            )
            continue

        if object_type == "HKCategoryTypeIdentifierHeadphoneAudioExposureEvent":
            observations.append(
                {
                    "metric": "headphone_audio_alerts",
                    "observed_at": end,
                    "unit": "count",
                    "value": 1.0,
                    "duration_seconds": duration_seconds,
                }
            )
            continue
        if object_type == "HKWorkoutTypeIdentifier":
            # Health.md v8 workouts carry type and duration but no statistics.
            fields = payload.get("fields")
            workout_duration = fields.get("durationSeconds") if isinstance(fields, dict) else None
            workout_seconds = workout_duration.get("value") if isinstance(workout_duration, dict) else None
            if not isinstance(workout_seconds, (int, float)) or isinstance(workout_seconds, bool):
                continue
            if not math.isfinite(workout_seconds) or workout_seconds <= 0:
                continue
            observations.append(
                {
                    "metric": "workout_duration",
                    "observed_at": end,
                    "unit": "s",
                    "value": float(workout_seconds),
                    "duration_seconds": duration_seconds,
                }
            )
            continue
        if object_type != "HKCategoryTypeIdentifierSleepAnalysis":
            continue
        metric_name = sleep_field(payload)
        if metric_name is None or end <= start:
            continue
        values = {
            "metric": metric_name,
            "observed_at": end,
            "unit": "s",
            "value": duration_seconds,
            "duration_seconds": duration_seconds,
        }
        observations.append(values)
        if metric_name.startswith("sleep_asleep") or metric_name in {
            "sleep_core",
            "sleep_deep",
            "sleep_rem",
        }:
            observations.append({**values, "metric": "sleep_total"})
    return observations


def influx_query(query: str, database: str | None = INFLUX_DATABASE) -> None:
    parameters = {"q": query}
    if database is not None:
        parameters["db"] = database
    request = Request(
        f"{INFLUX_URL}/query",
        data=urlencode(parameters).encode("utf-8"),
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        response_body = response.read()
    if not response_body:
        return
    response_body = json.loads(response_body)
    for result in response_body.get("results", []):
        if "error" in result:
            raise OSError("InfluxDB query failed")


# Read-only view of the latest smart scale weigh-in, for an iPhone Shortcut that
# logs it to Apple Health. It uses the same bearer token as the uploads and
# returns only these values. Home Assistant writes the scale sensors to its
# own InfluxDB database; the measurement is the unit, or the entity for
# unitless sensors.
SCALE_DATABASE = os.environ.get("HEALTHMD_SCALE_DATABASE", "homeassistant")
SCALE_DEVICE = os.environ.get("HEALTHMD_SCALE_DEVICE", "smart_scale_p2_pro")
SCALE_PROFILE = os.environ.get("HEALTHMD_SCALE_PROFILE", "")


def scale_fields() -> dict[str, tuple[str, str]]:
    device, profile = SCALE_DEVICE, SCALE_PROFILE
    return {
        "weight_kg": ("kg", f"{device}_weight"),
        "heart_rate_bpm": ("bpm", f"{device}_heart_rate"),
        "body_fat_percent": ("%", f"{device}_body_fat_{profile}"),
        "lean_body_mass_kg": ("kg", f"{device}_lean_body_mass_{profile}"),
        "bmi": (f"sensor.{device}_bmi_{profile}", f"{device}_bmi_{profile}"),
    }


def influx_points(database: str, query: str) -> list[tuple[datetime, float]]:
    request = Request(
        f"{INFLUX_URL}/query",
        data=urlencode({"q": query, "db": database, "epoch": "s"}).encode("utf-8"),
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        body = json.loads(response.read() or b"{}")
    points: list[tuple[datetime, float]] = []
    for result in body.get("results", []):
        if "error" in result:
            raise OSError("InfluxDB query failed")
        for series in result.get("series", []):
            for timestamp, value in series.get("values", []):
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                    points.append((datetime.fromtimestamp(timestamp, timezone.utc), float(value)))
    return points


# Home Assistant also rewrites the restored weight on every restart, so the
# time of the latest weight point is not the weigh-in time. The scale
# integration stamps each real weigh-in in the weight sensor's measured_at
# attribute, restored with the state; InfluxDB keeps it as measured_at_str.
# Values from the same weigh-in are written within seconds of it, so only that
# short span after it is read.
WEIGH_IN_SPAN_BEFORE_S = 60
WEIGH_IN_SPAN_AFTER_S = 180


def latest_weigh_in(database: str) -> tuple[datetime, float] | None:
    query = (
        'SELECT last("measured_at_str"), last("value") FROM "kg" '
        f"WHERE \"entity_id\" = '{SCALE_DEVICE}_weight' AND time > now() - 30d"
    )
    request = Request(
        f"{INFLUX_URL}/query",
        data=urlencode({"q": query, "db": database, "epoch": "s"}).encode("utf-8"),
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        body = json.loads(response.read() or b"{}")
    for result in body.get("results", []):
        if "error" in result:
            raise OSError("InfluxDB query failed")
        for series in result.get("series", []):
            for _time, measured_at, weight in series.get("values", []):
                if not isinstance(measured_at, str) or not isinstance(weight, (int, float)):
                    continue
                stamp = coerce_timestamp(measured_at)
                if stamp is not None and math.isfinite(weight):
                    return stamp, float(weight)
    return None


def latest_scale_reading() -> dict[str, Any] | None:
    """The latest weigh-in: its weight plus each value written just after it."""
    weigh_in = latest_weigh_in(SCALE_DATABASE)
    if weigh_in is None:
        return None
    measured_at, weight = weigh_in
    reading: dict[str, Any] = {
        "measured_at": measured_at.isoformat().replace("+00:00", "Z"),
        "weight_kg": round(weight, 2),
    }
    start = int(measured_at.timestamp()) - WEIGH_IN_SPAN_BEFORE_S
    end = int(measured_at.timestamp()) + WEIGH_IN_SPAN_AFTER_S
    for name, (measurement, entity_id) in scale_fields().items():
        if name == "weight_kg":
            continue
        values = influx_points(
            SCALE_DATABASE,
            f'SELECT first("value") FROM "{measurement}" WHERE "entity_id" = \'{entity_id}\' '
            f"AND time >= {start}s AND time <= {end}s",
        )
        if values:
            reading[name] = round(values[0][1], 2)
    return reading


def write_influx_points(body: bytes) -> None:
    request = Request(
        f"{INFLUX_URL}/write?{urlencode({'db': INFLUX_DATABASE, 'precision': 'ns'})}",
        data=body,
        method="POST",
        headers={"Content-Type": "text/plain; charset=utf-8"},
    )
    with urlopen(request, timeout=10) as response:
        if not 200 <= response.status < 300:
            raise OSError("InfluxDB write failed")


def escape_influx_tag(value: str) -> str:
    return value.replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def synchronise_observations(source: str, partition: str, observations: list[dict[str, Any]]) -> None:
    influx_query(
        f'DELETE FROM "{HEALTH_OBSERVATION_MEASUREMENT}" '
        f"WHERE \"source\" = '{source}' AND \"partition\" = '{partition}'"
    )
    lines: list[str] = []
    # InfluxDB keeps one point per series and timestamp. Two samples of one
    # metric can end at the same instant, for example two meals logged
    # together, so later ones move forward by a nanosecond instead of
    # overwriting the first.
    used_timestamps: set[tuple[str, int]] = set()
    for observation in observations:
        metric = observation["metric"]
        unit = observation["unit"]
        timestamp = observation["observed_at"]
        tags = ",".join(
            (
                f"metric={escape_influx_tag(metric)}",
                f"partition={escape_influx_tag(partition)}",
                f"source={escape_influx_tag(source)}",
                f"unit={escape_influx_tag(unit)}",
            )
        )
        fields = ",".join(
            (
                f"duration_seconds={observation['duration_seconds']!r}",
                f"value={observation['value']!r}",
            )
        )
        timestamp_nanoseconds = int(timestamp.timestamp() * 1_000_000_000)
        while (tags, timestamp_nanoseconds) in used_timestamps:
            timestamp_nanoseconds += 1
        used_timestamps.add((tags, timestamp_nanoseconds))
        lines.append(f"{HEALTH_OBSERVATION_MEASUREMENT},{tags} {fields} {timestamp_nanoseconds}")
    if lines:
        write_influx_points("\n".join(lines).encode("utf-8"))


def initialise_influx() -> None:
    influx_query(f'CREATE DATABASE "{INFLUX_DATABASE}"', database=None)
    for measurement in ("health_daily", "health_samples", HEALTH_OBSERVATION_MEASUREMENT):
        influx_query(f'DROP MEASUREMENT "{measurement}"')
    for latest_path in sorted((DATA_DIRECTORY / "latest").glob("*.json")):
        with latest_path.open("rb") as source:
            record = json.load(source)
        partition = parse_date(record.get("date"), "record date").isoformat()
        synchronise_observations(HEALTH_MD_SOURCE, partition, normalise_healthmd_observations(record))
    for partition_path in sorted((DATA_DIRECTORY / "observation-sources").glob("*/partitions/*.json")):
        with partition_path.open("rb") as source:
            payload = json.load(source)
        observation_source, partition, observations = validate_observation_batch(payload)
        synchronise_observations(observation_source, partition, observations)


class HealthReceiverHandler(BaseHTTPRequestHandler):
    server_version = "HealthReceiver/2"

    def log_message(self, _format: str, *_arguments: Any) -> None:
        # Never write health payload metadata or credentials to container logs.
        return

    def send_json(self, status: HTTPStatus, value: dict[str, Any]) -> None:
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The client closed the connection before it read the response.
            # The request was already processed, so there is nothing to undo.
            pass

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self.send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if path == "/scale/latest":
            if not AUTH_TOKEN or not SCALE_PROFILE or not hmac.compare_digest(
                self.headers.get("Authorization", ""), f"Bearer {AUTH_TOKEN}"
            ):
                self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorised"})
                return
            try:
                reading = latest_scale_reading()
            except (OSError, ValueError):
                self.send_json(HTTPStatus.BAD_GATEWAY, {"error": "scale_data_unavailable"})
                return
            if reading is None:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "no_reading"})
                return
            self.send_json(HTTPStatus.OK, reading)
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path not in {"/healthmd/ingest", "/health/ingest"}:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        if not AUTH_TOKEN or not hmac.compare_digest(
            self.headers.get("Authorization", ""), f"Bearer {AUTH_TOKEN}"
        ):
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorised"})
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self.send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "application_json_required"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            content_length = -1
        if content_length < 1:
            self.send_json(HTTPStatus.LENGTH_REQUIRED, {"error": "content_length_required"})
            return
        if content_length > MAXIMUM_BODY_BYTES:
            self.send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "payload_too_large"})
            return

        body = self.rfile.read(content_length)
        if len(body) != content_length:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "incomplete_request_body"})
            return
        try:
            payload = json.loads(body)
            if path == "/healthmd/ingest":
                exported_at, record_dates = validate_healthmd_export(payload)
                updated_records = persist_healthmd_export(payload, body, exported_at, record_dates)
                for record in updated_records:
                    partition = parse_date(record.get("date"), "record date").isoformat()
                    synchronise_observations(
                        HEALTH_MD_SOURCE,
                        partition,
                        normalise_healthmd_observations(record),
                    )
                response = {"accepted": len(record_dates), "updated": len(updated_records)}
            else:
                source, partition, observations = validate_observation_batch(payload)
                persist_observation_batch(payload, body, source, partition)
                synchronise_observations(source, partition, observations)
                response = {"accepted": len(observations), "updated": len(observations)}
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
            error = "invalid_health_export" if path == "/healthmd/ingest" else "invalid_health_observation_batch"
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": error})
            return
        except OSError:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "persistence_failed"})
            return

        self.send_json(HTTPStatus.ACCEPTED, response)


def main() -> None:
    if not AUTH_TOKEN:
        raise SystemExit("HEALTHMD_AUTH_TOKEN is required")
    if MAXIMUM_BODY_BYTES < 1:
        raise SystemExit("HEALTHMD_MAX_BODY_BYTES must be positive")
    DATA_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        initialise_influx()
    except (OSError, ValueError, json.JSONDecodeError):
        # The upload path returns an error until InfluxDB becomes reachable.
        # Keep the receiver's health route available during a transient restart.
        pass
    server = ThreadingHTTPServer(("0.0.0.0", 8080), HealthReceiverHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
