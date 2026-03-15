# Solid Sync

This add-on mirrors Home Assistant entity snapshots into a Solid pod.

## Features

- Ingress web UI with a dedicated `Solid` sidebar entry
- Global Solid connection settings stored once
- Multiple sync profiles
- Multiple measurements per profile
- Per profile write mode: single file or timestamped snapshots
- OIDC client-credentials authentication
- Live subscription to Home Assistant `state_changed` events
- Manual test trigger per profile

## Current payload model

Each profile can either overwrite one fixed file or write timestamped JSON resources below the configured base path:

```json
{
  "profile": "Garden weather station",
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
```

For timestamped mode and a base path like `weather-stations/garden`, each sync writes a new file such as:

```text
weather-stations/garden/2026-03-15T16-42-01.284991Z.json
```

## First start

1. Install and start the add-on.
2. Open the web UI via the `Solid` sidebar entry or `Open Web UI`.
3. Save your Solid connection settings once at the top of the page.
4. Create one or more profiles with a resource base path, a write mode and multiple measurement mappings.

## Notes

- The add-on stores both settings and profiles in `/data/solid-sync.json`.
- Secrets are stored there in plain text because the add-on needs them to authenticate against the Solid issuer.
- Each `state_changed` event from a mapped entity creates a new snapshot resource.
