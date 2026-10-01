# Changelog

## 0.6.2

- Accept browser-uploaded gzip resources served as `application/x-gzip` or binary data, avoiding Solid GET 501 conversion errors
- Preserve recognized binary media types when updating existing gzip resources
- Keep ETag preconditions, decompression checks and upload verification before clearing pending entries

## 0.6.1

- Fix local builds on Supervisor 2026.04+ by setting an explicit, pinned default base image
- Add Home Assistant image labels and align advertised architectures with the amd64/aarch64 base image
- Normalize the startup script's line endings for Linux builds from Windows checkouts

## 0.6.0

- Store the complete history losslessly as one `.json.gz` resource per profile
- Import the legacy JSON on the first gzip upload and preserve the original file
- Verify gzip uploads after decompression and retain all measurements and metadata
- Stop on corrupt gzip or missing previously uploaded resources instead of reusing stale JSON
- Bound expanded JSON size to 128 MiB

## 0.5.2

- Keep the existing single-file JSON format and URL, with compact UTF-8 serialization
- Save settings and queued snapshots through a flushed temporary file and atomic replacement
- Save a compressed local recovery copy of each complete intended upload before replacing the Pod resource
- Guard Pod writes with ETag preconditions and read back the uploaded JSON before clearing the queue
- Avoid duplicate snapshots when retrying an upload whose response or local acknowledgement was lost
- Reject partially unsupported histories instead of silently removing entries
- Keep upload errors visible when new sensor events arrive

## 0.5.1

- Keep the sidebar metadata at `Solid Sync` and bump the add-on version again
- Change live syncs into a rolling 24-hour upload window per profile
- Queue daily snapshots locally and upload them in one append operation
- Show pending entry count and next upload time in the UI

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
