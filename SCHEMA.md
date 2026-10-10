# Health observation schema

`POST /health/ingest` accepts a versioned batch of numeric health observations.
It uses the same bearer token as `/healthmd/ingest`.

```json
{
  "schema": "health.observation_batch",
  "schema_version": 1,
  "source": "example_app",
  "partition": "2026-08-11",
  "observation_count": 1,
  "observations": [
    {
      "metric": "steps",
      "observed_at": "2026-08-11T09:42:00Z",
      "unit": "count",
      "value": 93,
      "duration_seconds": 300
    }
  ]
}
```

The receiver stores each observation in InfluxDB measurement
`health_observation`.

| Part | Meaning |
| --- | --- |
| `source` | A lower snake case identifier for the contributing application. |
| `partition` | An ISO date. A new batch replaces this source and partition only. |
| `metric` | A lower snake case identifier, for example `steps` or `heart_rate`. |
| `observed_at` | RFC 3339 time at which the observation ended or occurred. |
| `unit` | A compact unit token, for example `count`, `bpm`, `km`, `kcal`, or `s`. |
| `value` | Finite numeric measurement. |
| `duration_seconds` | Optional non-negative interval duration. It is zero for an instant observation. |

The Influx timestamp is `observed_at`. For an interval observation, its start
time is `observed_at - duration_seconds`.

`/healthmd/ingest` remains compatible with Health.md. The receiver converts
that source into this schema with `source=health_md`. It keeps the original
accepted uploads on disk, so a restart can rebuild the derived measurement.
