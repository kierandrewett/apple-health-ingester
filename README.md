# apple-health-ingester

A private receiver for [Health.md](https://github.com/CodyBontecou/health-md) API endpoint exports and other numeric health observations.

It stores each accepted upload on disk and writes normalised observations to the InfluxDB measurement `health_observation`.

## Endpoints

| Method | Path | Body |
| --- | --- | --- |
| `POST` | `/healthmd/ingest` | Health.md `healthmd.api_export` v1 or v2, with `healthmd.health_data` v7 or v8 daily records |
| `POST` | `/health/ingest` | `health.observation_batch` v1, see [SCHEMA.md](SCHEMA.md) |
| `GET` | `/healthz` | None |

Both `POST` endpoints need `Authorization: Bearer <token>` and `Content-Type: application/json`.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `HEALTHMD_AUTH_TOKEN` | none, required | Bearer token for uploads |
| `HEALTHMD_DATA_DIR` | `/data` | Directory for accepted uploads |
| `HEALTHMD_MAX_BODY_BYTES` | `67108864` | Largest accepted request body |
| `HEALTHMD_INFLUX_URL` | `http://ha-influxdb:8086` | InfluxDB 1.x URL |
| `HEALTHMD_INFLUX_DATABASE` | `healthmd` | InfluxDB database |

## Run

```sh
docker build -t apple-health-ingester https://github.com/kierandrewett/apple-health-ingester.git
docker run -p 8080:8080 -e HEALTHMD_AUTH_TOKEN=change-me -v ./data:/data apple-health-ingester
```
