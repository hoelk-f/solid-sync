# Solid Sync

This add-on mirrors Home Assistant entity snapshots into a Solid pod.

## Features

- Ingress web UI with a dedicated `Solid Sync` sidebar entry
- Global Solid connection settings stored once
- Multiple sync profiles
- Multiple measurements per profile
- OIDC client-credentials authentication
- Live subscription to Home Assistant `state_changed` events
- Rolling 24-hour upload window per profile
- Manual test trigger per profile for immediate upload
- Automatic creation of missing parent containers in the Solid pod

## Current payload model

Each profile writes to one fixed JSON file. Relevant `state_changed` events are first collected locally for up to 24 hours. When the upload window is due, the add-on downloads that file, appends all queued snapshot entries and uploads it again:

```json
{
  "profile": "Garden weather station",
  "resource_path": "weather-stations/garden.json",
  "updated_at": "2026-03-15T16:42:01.284991+00:00",
  "entries": [
    {
      "captured_at": "2026-03-15T16:42:01.284991+00:00",
      "measurements": {
        "temperature": {
          "entity_id": "sensor.garden_temperature",
          "state": "23.4",
          "attributes": {
            "unit_of_measurement": "degC"
          }
        },
        "humidity": {
          "entity_id": "sensor.garden_humidity",
          "state": "48",
          "attributes": {
            "unit_of_measurement": "%"
          }
        }
      }
    }
  ]
}
```

## First start

1. Install and start the add-on.
2. Open the web UI via the `Solid Sync` sidebar entry or `Open Web UI`.
3. Save your Solid connection settings once at the top of the page.
4. Create one or more profiles with a resource path and multiple measurement mappings.

## Notes

- The add-on stores both settings and profiles in `/data/solid-sync.json`.
- Secrets are stored there in plain text because the add-on needs them to authenticate against the Solid issuer.
- Each mapped `state_changed` event queues a new local snapshot entry for the next daily upload.
- If parent containers in the target path are missing, the add-on tries to create them before uploading the JSON resource.
- `Test now` bypasses the daily wait by capturing one fresh snapshot and flushing the whole pending queue immediately.
