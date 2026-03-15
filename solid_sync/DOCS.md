# SOLID Sync

This add-on mirrors selected Home Assistant sensor states into a SOLID pod.

## Features

- Ingress web UI with a dedicated `Solid` sidebar entry
- Multiple sync profiles
- OIDC client-credentials authentication
- Live subscription to Home Assistant `state_changed` events
- Manual test trigger per profile

## Current payload model

Each sync writes JSON to the configured SOLID resource:

```json
{
  "state": "23.4",
  "attributes": {
    "unit_of_measurement": "degC"
  }
}
```

## First start

1. Install and start the add-on.
2. Open the web UI via the `Solid` sidebar entry or `Open Web UI`.
3. Create one or more sync profiles.
4. Choose a Home Assistant sensor, SOLID pod URL, OIDC issuer URL, client token and client secret.

## Notes

- The add-on stores its profiles in `/data/solid-sync.json`.
- Secrets are stored there in plain text because the add-on needs them to authenticate against the SOLID issuer.
- This first add-on version keeps the current fixed payload structure. Flexible data modeling can be added next.
