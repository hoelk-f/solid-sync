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

Each profile writes one complete gzip-compressed JSON file (`application/gzip`). The
configured path `weather-stations/garden.json` writes `weather-stations/garden.json.gz`;
an explicit `.gz` suffix is not added twice. Relevant `state_changed` events are collected
locally for up to 24 hours. The add-on downloads and decompresses the existing history,
appends queued snapshots, and uploads gzip bytes. The decompressed document is:

```json
{
  "profile": "Garden weather station",
  "resource_path": "weather-stations/garden.json.gz",
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

## Upgrade to 0.6.0

Deploy the updated Dataspace cache worker and Environmental Monitoring first, then
update Solid Sync. Monitoring prefers `weatherstation_garden.json.gz`, falling back to
the original JSON only on HTTP 404. Once gzip exists, it is authoritative. The profile
does not need to be deleted or recreated; its pending queue is preserved.

On the first upload, Solid Sync imports the legacy `.json` history if the `.gz` resource
is absent. The old file is left untouched as a migration backup and is no longer updated.
If the old file is damaged, restore the repaired JSON before migration, or seed the new
`.json.gz` resource with a validated compressed recovery copy. Existing per-resource
access rules are not copied: the new resource inherits its parent container's access
rules, so confirm that the intended reader can read the new URL if the old JSON had a
custom ACL. Update catalog distribution links/media types and any additional consumers
to the new URL (`application/gzip`) after migration.

Gzip is stored as a file, without an HTTP `Content-Encoding` header. Compression is
lossless: every measurement and attribute remains present. Only the document's
`resource_path` reflects its new URL. The complete history still grows over time.

## Upload safety and recovery

Before each upload, the add-on saves the complete intended JSON document, including the
existing history and pending entries, as a compressed local recovery copy under
`/data/recovery/<target-and-profile-hash>.json.gz`. One copy per target/profile is retained
and atomically replaced by the next prepared upload. This uses local Home Assistant
storage, not another Pod resource. Include the add-on data in your Home Assistant backups.
It is a recovery copy, not a record of confirmed uploads: a conditional upload may fail
because another client has changed the Pod since it was read.

Settings, pending entries and recovery copies are written to a temporary file in the
same directory, flushed with `fsync`, then replaced with `os.replace`. On Linux the parent
directory is also synced. See the [Python file replacement documentation](https://docs.python.org/3/library/os.html#os.replace).

Existing Pod resources require a strong ETag. `If-Match` prevents overwriting another
writer's newer version; new resources use `If-None-Match: *`. After PUT, the add-on reads
the JSON back with cache revalidation and compares its full contents. It clears pending
entries only after verification. Retrying the same snapshots does not append duplicates.
These conditions follow [HTTP preconditions](https://www.rfc-editor.org/rfc/rfc9110.html#section-13.1).

A client cannot make the server's filesystem writes atomic. A server that overwrites a
file in place may still leave a truncated resource after a connection or process failure.
Verification detects that condition and preserves the local queue and recovery copy;
it does not prevent transient broken reads by other applications or guarantee the server's
disk durability. Server-side temporary writes plus atomic replacement are needed for that.

If a resource is damaged, the error includes the local recovery path when available.
Stop the add-on and preserve its data and the damaged Pod file before restoring. Extract
the relevant `.json.gz` from the add-on data/backup and validate the decompressed JSON.
Check for changes made by other clients before replacing the Pod resource. Restore the
gzip file to the `.json.gz` URL with type `application/gzip`, restart the add-on, then
use `Test now`. Snapshots already present in
the restored file are skipped on retry. Files damaged before 0.5.2 have no recovery copy
unless a later complete upload has been prepared.

## Size and integration

The updated Environmental Monitoring consumer reads one complete resource and expects an
`entries` array with `captured_at`, measurement `state`, and pressure units under
`attributes.unit_of_measurement`. Its reader handles both stored gzip and plain JSON,
including already decompressed cache responses. Station coordinates remain in
Environmental Monitoring's station configuration. Splitting history across files is
not part of this release.

The inspected Solid Dataspace worker already compresses its public monitoring cache
responses with gzip. Its updated source fetch accepts stored gzip with a 32 MiB wire
limit and a 128 MiB expanded JSON limit; ordinary JSON retains its 32 MiB limit. The
frontend and Solid Sync also limit decoded JSON to 128 MiB. Corrupt gzip fails rather
than silently falling back to an older JSON history. Gzip reduces storage and transfer
size but does not make server writes atomic or allow unlimited history.

For a substantially smaller data model, repeated Home Assistant metadata could be
separated from observations, or an explicit export mode could retain only timestamps,
values and units. Neither change is applied automatically because other consumers may
use those fields. No history is discarded or downsampled by this release.

## Development checks

Install `solid_sync/requirements.txt`, then run `python -m unittest discover -s tests -v`
from the repository root. The tests use a local HTTP server to exercise truncated writes,
failed verification, concurrent changes, retry after restart, and local disk failures.
