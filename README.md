# Solid Sync

Standalone Home Assistant add-on for mirroring Home Assistant snapshots into a Solid pod.

## Repository layout

```text
repository.yaml          Home Assistant add-on repository metadata
solid_sync/              The actual add-on
archive/                 Archived custom-integration prototype
```

## What the add-on does

- adds a `Solid` sidebar entry via ingress
- stores Solid connection settings once for all profiles
- lets you create multiple sync profiles in the web UI
- lets each profile combine multiple entities into one snapshot
- subscribes to Home Assistant `state_changed` events
- can either overwrite one Solid file or write timestamped snapshot resources

Current payload shape:

```json
{
  "profile": "Garden weather station",
  "captured_at": "2026-03-15T16:42:01.284991+00:00",
  "measurements": {
    "temperature": {
      "entity_id": "sensor.garden_temperature",
      "state": "23.4"
    },
    "humidity": {
      "entity_id": "sensor.garden_humidity",
      "state": "48"
    }
  }
}
```

## Install from GitHub

After this repository is pushed to GitHub, add it in Home Assistant:

1. Open `Settings -> Add-ons -> Add-on Store`
2. Open the repository menu
3. Add `https://github.com/hoelk-f/solid-sync`
4. Install `Solid Sync`
5. Start it and open the web UI

## Install locally during development

Copy `solid_sync/` to your Home Assistant add-on directory as:

```text
/addons/solid_sync
```

Then reload the Add-on Store and install `Solid Sync`.

## Status

This repository is now structured as a dedicated add-on repository. The earlier custom component prototype is preserved under `archive/custom_component_prototype/`.
