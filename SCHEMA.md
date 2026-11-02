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

## Health.md metrics

The receiver keeps only the HealthKit types it maps. Other types stay in the
stored uploads and can be mapped later; a restart replays them.

| Metric | Unit | HealthKit type |
| --- | --- | --- |
| `steps` | `count` | `StepCount` |
| `active_calories`, `basal_calories` | `kcal` | `ActiveEnergyBurned`, `BasalEnergyBurned` |
| `walking_running_distance` | `km` | `DistanceWalkingRunning` |
| `flights_climbed` | `count` | `FlightsClimbed` |
| `heart_rate` | `bpm` | `HeartRate` |
| `walking_speed` | `m_per_s` | `WalkingSpeed` |
| `step_length` | `cm` | `WalkingStepLength` |
| `walking_asymmetry_percent`, `double_support_percent`, `walking_steadiness_percent` | `percent`, a 0 to 1 fraction | `WalkingAsymmetryPercentage`, `WalkingDoubleSupportPercentage`, `AppleWalkingSteadiness` |
| `headphone_audio_level` | `dBASPL` | `HeadphoneAudioExposure` |
| `headphone_audio_alerts` | `count`, 1 per alert | `HeadphoneAudioExposureEvent` |
| `weight`, `lean_body_mass` | `kg` | `BodyMass`, `LeanBodyMass` |
| `body_fat_percent` | `percent`, a 0 to 1 fraction | `BodyFatPercentage` |
| `body_mass_index` | `count` | `BodyMassIndex` |
| `height` | `cm` | `Height` |
| `dietary_*` | `kcal`, `g`, `mg` or `mcg`, as sent | 24 `Dietary*` types, see `HEALTHMD_DIETARY_MAPPINGS` |
| `workout_duration` | `s` | `HKWorkoutTypeIdentifier` |
| `sleep_core`, `sleep_deep`, `sleep_rem`, `sleep_asleep`, `sleep_in_bed`, `sleep_awake`, `sleep_total` | `s` | `SleepAnalysis` |

A sample whose unit does not match the expected HealthKit unit is skipped.
