# Taggz

Taggz looks up staged FLAC releases in Qobuz, MusicBrainz and Discogs. It verifies editions, writes tags and organizes releases within staging.

## Run on the NAS

Run these commands from the project directory on the Docker host. The host launcher uses Python 3.11 or newer and Docker. It passes your UID and GID through to the container.

```sh
cp taggz.toml.example taggz.toml
# Edit [paths] in taggz.toml. Use the host's absolute paths.
docker build -t taggz:local .
python3 -m taggz.docker --config taggz.toml
python3 -m taggz.docker --config taggz.toml --apply
```

Each push builds and tests a Linux AMD64 image. Successful builds from `main`
publish `ghcr.io/golitedev/taggz:latest` and a commit-specific tag. To use the
published image, pull it on the NAS and pass it to the launcher. For a private
package, log in to GHCR before pulling:

```sh
docker pull ghcr.io/golitedev/taggz:latest
python3 -m taggz.docker --image ghcr.io/golitedev/taggz:latest --config taggz.toml
```

Preview is the default. Docker mounts the config and optional Qobuz credentials read-only. Staging is read-only in preview and writable with `--apply`. Existing configs that still have `paths.library` can be used during the transition. Taggz uses only `paths.staging`.

## Qobuz credentials

Add a `[qobuz]` section to `taggz.toml` and set `credentials_file` to the absolute path of a separate file on the Docker host. The launcher mounts that file read-only.

```toml
[qobuz]
credentials_file = "/srv/docker/taggz/qobuz-credentials.toml"
```

The credentials file accepts a Qobuz token in a `[qobuz]` table using `token` or `user_auth_token`. It also accepts a Streamrip config that uses `use_auth_token = true` and `password_or_token`, or a Qobuz export with a `qobuz:` heading and `token:` field. You can also set `TAGGZ_QOBUZ_TOKEN` in the launcher's environment. Taggz obtains the public Qobuz app ID from the web player. If discovery fails, set `qobuz.app_id` or `TAGGZ_QOBUZ_APP_ID`. To disable Qobuz, set `qobuz.enabled = false`. MusicBrainz and Discogs can still be used without a Qobuz token.

To preview a single album, pass its staging folder name exactly:

```sh
python3 -m taggz.docker --config taggz.toml --json -- "Artist - Album"
```

`--json` includes before-and-after tag values, input evidence, the provider URLs, the number of matching ISRCs, and why each candidate qualified or was rejected. Apply saves the same details in the release report and prints its path. `--offline` inspects staged files without network access or applying changes. Native execution is also possible with `pip install .` and `taggz --config taggz.toml`. Install FLAC's `metaflac` binary for native ReplayGain.

## Staging layout

Place one release per immediate subfolder of the configured staging directory. Taggz recognizes FLAC tracks, including releases with multiple disc folders. On apply it creates these directories:

- `completed/` receives successfully tagged and verified releases.
- `needs-review/` receives releases that cannot be matched or safely edited.
- `.taggz/reports/` stores JSON reports. Each completed release also has a ZIP under `.taggz/backups/` with the original FLAC metadata blocks, tags, paths, and any rewritten cue sheets or playlists.

Apply operates within staging. Releases with insufficient matching evidence and releases whose destination already exists go to review. After resolving an issue, move the release back to the staging inbox and run Taggz again.

## Matching and verification

Taggz checks track titles, order, format, durations, ISRCs, barcodes and catalog numbers. It uses Exact Audio Copy and XLD rip logs to compute CD disc IDs for MusicBrainz. It matches tracks in cue sheets to staged files before using cue barcodes and ISRCs. Existing valid FLAC ISRCs take priority over cue codes. Discogs entries linked from MusicBrainz can supply additional credits. An optional Discogs token enables independent barcode and catalog searches. Set the environment variable named by `matching.discogs_token_env`, which defaults to `TAGGZ_DISCOGS_TOKEN`. The token is not written into tags or reports. Configure `matching.contact` to identify your MusicBrainz requests.

For a verified Qobuz digital edition, Taggz uses its album and track IDs, credits, ISRCs, release dates, barcode, label, genre, description, copyright, explicit advisory and classical work when provided. MusicBrainz can enrich the same edition when the providers agree on the barcode and track list. A recording confirmed by ISRC can supply credits across editions. CD and vinyl retain their physical format and pressing metadata. Qobuz product IDs require evidence for the digital edition.

On apply, Taggz prepares a separate copy, rewrites supported cue and playlist references, calculates ReplayGain if enabled, and verifies the FLAC frame hashes, stream metadata, and embedded images. For releases with incompatible audio formats, it preserves existing ReplayGain tags and reports why it skipped calculation. It publishes the prepared copy only after verification. A later apply recovers interrupted staging transactions before processing new releases. Original rip logs and existing cover files are kept with the release.

Exit codes: `0` all processed releases can complete, `1` at least one release needs review, and `2` configuration or routing error.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

The integration tests use local FLAC fixtures and mocked catalog responses. To run them inside the built image, mount the tests directory read-only and run `python -m unittest discover -s /tests -v` as the entrypoint.
