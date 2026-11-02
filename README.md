# apple-health-ingester

A private receiver for [Health.md](https://github.com/CodyBontecou/health-md) API endpoint exports and other numeric health observations.

It stores each accepted upload on disk and writes normalised observations to the InfluxDB measurement `health_observation`.

## Endpoints

| Method | Path | Body |
| --- | --- | --- |
| `POST` | `/healthmd/ingest` | Health.md `healthmd.api_export` v1 or v2, with `healthmd.health_data` v7 or v8 daily records |
| `POST` | `/health/ingest` | `health.observation_batch` v1, see [SCHEMA.md](SCHEMA.md) |
| `GET` | `/scale/latest` | None. Returns the latest smart scale weigh-in from Home Assistant |
| `GET` | `/healthz` | None |

Both `POST` endpoints and `GET /scale/latest` need `Authorization: Bearer <token>`. The `POST` endpoints also need `Content-Type: application/json`.

`/scale/latest` returns `measured_at` (UTC, ISO 8601) from the scale weight sensor's `measured_at` attribute, so a Home Assistant restart does not look like a new weigh-in, plus whichever of `weight_kg`, `heart_rate_bpm`, `body_fat_percent`, `lean_body_mass_kg` and `bmi` were written from 1 minute before to 3 minutes after it. It is off until `HEALTHMD_SCALE_PROFILE` is set.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `HEALTHMD_AUTH_TOKEN` | none, required | Bearer token for uploads and `/scale/latest` |
| `HEALTHMD_DATA_DIR` | `/data` | Directory for accepted uploads |
| `HEALTHMD_MAX_BODY_BYTES` | `67108864` | Largest accepted request body |
| `HEALTHMD_INFLUX_URL` | `http://ha-influxdb:8086` | InfluxDB 1.x URL |
| `HEALTHMD_INFLUX_DATABASE` | `healthmd` | InfluxDB database |
| `HEALTHMD_SCALE_DATABASE` | `homeassistant` | Home Assistant's InfluxDB database, read by `/scale/latest` |
| `HEALTHMD_SCALE_DEVICE` | `smart_scale_p2_pro` | Scale entity prefix in Home Assistant |
| `HEALTHMD_SCALE_PROFILE` | none | Scale profile suffix, for example `kieran`; `/scale/latest` is off without it |

## Run

```sh
docker build -t apple-health-ingester https://github.com/kierandrewett/apple-health-ingester.git
docker run -p 8080:8080 -e HEALTHMD_AUTH_TOKEN=change-me -v ./data:/data apple-health-ingester
```
