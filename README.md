# Taggz

Taggz matches staged FLAC releases to verifiable metadata, formats them like the music library, and routes them inside staging. The library is a read-only reference.

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

Preview is the default. Docker mounts the library and config read-only in both modes. Staging is read-only in preview. With `--apply`, staging is the only writable data mount. The launcher requires recursive read-only bind support for the library. On Linux this requires kernel 5.12 or newer.

To preview a single album, pass its staging folder name exactly:

```sh
python3 -m taggz.docker --config taggz.toml --json -- "Artist - Album"
```

`--json` includes before-and-after tag values, input evidence, and the reason each candidate qualified or was rejected. Candidate details include the number of matching ISRCs and PCM checksums and the largest duration difference when track positions align. Apply saves the same details in the release report and prints its path. Review reports identify the step that failed. Use `--offline` to search the library only. Native execution is also possible with `pip install .` and `taggz --config taggz.toml`. Install FLAC's `metaflac` binary for native ReplayGain. Use the Docker launcher when you want the OS to enforce read-only library access.

## Staging layout

Place one release per immediate subfolder of the configured staging directory. Taggz recognizes FLAC tracks, including releases with multiple disc folders. On apply it creates these directories:

- `completed/` receives successfully tagged and verified releases.
- `needs-review/` receives releases that cannot be matched or safely edited.
- `.taggz/reports/` stores JSON reports. Each completed release also has a ZIP under `.taggz/backups/` with the original FLAC metadata blocks, tags, paths, and any rewritten cue sheets or playlists.

The apply operation never imports a release into the library. Releases with insufficient matching evidence and releases whose destination already exists go to review. After resolving an issue, move the release back to the staging inbox and run Taggz again.

## Matching and verification

Taggz checks track titles, order, format, durations, ISRCs, barcodes and catalog numbers. It uses Exact Audio Copy and XLD rip logs to compute CD disc IDs for MusicBrainz, and reads barcodes and ISRCs from cue sheets. Discogs entries linked from MusicBrainz can supply additional credits. An optional Discogs token enables independent barcode and catalog searches. Set the environment variable named by `matching.discogs_token_env`, which defaults to `TAGGZ_DISCOGS_TOKEN`. The token is not written into tags or reports. Configure `matching.contact` to identify your MusicBrainz requests.

Library comparison uses folders named `Artist — Album (Year) [Source] [FLAC] [Quality]` at the library root. Identical PCM checksums can verify that staged tracks match the decoded audio in the library. When only the track list or recordings can be verified, Taggz can still standardize naming and add supported recording credits while keeping the staged medium and withholding unverified release identifiers. Existing Qobuz tags can be reused from a verified library edition. This version does not query Qobuz directly.

On apply, Taggz prepares a separate copy, rewrites supported cue and playlist references, calculates ReplayGain if enabled, and verifies the FLAC frame hashes, stream metadata, and embedded images. It publishes the prepared copy only after verification. A later apply recovers interrupted staging transactions before processing new releases. Original rip logs and existing cover files are kept with the release.

Exit codes: `0` all processed releases can complete, `1` at least one release needs review, and `2` configuration or routing error.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

The integration tests use local FLAC fixtures and do not contact providers or modify NAS data.
To run the routing tests through Docker, build `taggz:local` and set `TAGGZ_TEST_DOCKER=1` when running the same test command.
