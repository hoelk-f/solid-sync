# Changelog

## 0.5.0

- Rename the sidebar entry from `Solid` to `Solid Sync`
- Remove profile editing from the UI so profiles can only be created, tested or deleted
- Create missing parent containers in the Solid pod before writing a profile resource
- Bump the add-on version for the next Home Assistant update

## 0.4.0

- Remove write mode and always append new snapshots into one JSON file per profile
- Add collapsible sections for connection settings, profile editor and profile list
- Rework the ingress UI toward a flatter Home Assistant dark theme
- Remove the persistent connection badge from the header

## 0.3.0

- Add-on version bump for proper Home Assistant update detection
- Profiles can now write either a single file or timestamped snapshots
- UI refined with subtler dark mode styling and a simpler header
- Profiles are always active and can only be deleted

## 0.2.0

- Global Solid connection settings stored once for all profiles
- Weather-station style profiles with multiple measurements per snapshot
- Timestamped resource creation for every sync instead of overwriting one file
- Snapshot payloads now contain multiple mapped entities in one resource
- Updated ingress UI for station-oriented configuration

## 0.1.0

- Initial standalone Home Assistant add-on release
- Ingress web UI with `Solid` sidebar entry
- Multiple sensor-to-Solid sync profiles
- Home Assistant websocket subscription via supervisor API
- Fixed JSON payload format with `state` and `attributes`
