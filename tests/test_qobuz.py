import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from taggz.catalog import Catalog, Edition, Entry, from_qobuz, qobuz_version
from taggz.config import Config
from taggz.match import propose
from taggz.qobuz import Qobuz, credentials
from taggz.release import Release, Track


def album():
    return {
        "id": "abc123def456",
        "title": "Album",
        "artist": {"name": "Act"},
        "upc": "0602465808140",
        "release_date_original": "2003-01-12",
        "release_date_download": "2021-04-17",
        "label": {"name": "A Label"},
        "genre": {"name": "Hip-Hop/Rap"},
        "description": "<p>First &amp; last.</p><p>Next.</p>",
        "release_type": "single",
        "tracks_count": 1,
        "tracks": {
            "items": [
                {
                    "id": 42,
                    "title": "Movement",
                    "work": "Suite",
                    "track_number": 1,
                    "media_number": 1,
                    "duration": 90,
                    "performer": {"name": "Act"},
                    "isrc": "USABC2300123",
                    "performers": "Act, MainArtist, Vocalist, Producer - "
                    "Guest, MainArtist, Vocalist - Featured, FeaturedArtist - "
                    "A Writer, ComposerLyricist - An Engineer, MasteringEngineer",
                    "copyright": "℗ A Label",
                    "parental_warning": False,
                }
            ]
        },
    }


def staged(edition):
    tags = {"isrc": ["USABC2300123"]}
    track = Track(Path("01.flac"), 1, 1, "Movement", 90.75, 16, 44100, tags)
    return Release(
        Path("Act — Album [WEB]"),
        "Act",
        "Album",
        edition.date,
        edition.tags["barcode"][0],
        "",
        "Digital Media",
        [track],
        [],
        {},
        {},
    )


class QobuzLookup(unittest.TestCase):
    def test_both_credential_file_formats(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict("os.environ", clear=True),
        ):
            path = Path(temp) / "credentials"
            path.write_text('qobuz:\n  user_id: "123"\n  token: "test-token"\n')
            config = Config(Path(temp), qobuz_credentials=path)
            self.assertEqual(credentials(config), ("test-token", ""))
            path.write_text(
                '[qobuz]\nuse_auth_token = true\npassword_or_token = "test-token"\n'
            )
            self.assertEqual(credentials(config), ("test-token", ""))

    def test_request_does_not_expose_token_in_url_or_error_and_blocks_bad_auth(self):
        with patch.dict("os.environ", {"TAGGZ_QOBUZ_TOKEN": "test-token"}, clear=True):
            qobuz = Qobuz(
                Config(Path("/tmp"), qobuz_app_id="123456789"), set(), offline=False
            )
        with patch(
            "taggz.qobuz.urlopen",
            side_effect=HTTPError(
                "https://example.test/test-token", 401, "test-token", None, None
            ),
        ) as get:
            self.assertEqual(qobuz.get("album/search", query="Album"), {})
            self.assertEqual(qobuz.get("album/get", album_id="abc123def456"), {})
        request = get.call_args.args[0]
        self.assertEqual(get.call_count, 1)
        self.assertNotIn("test-token", request.full_url)
        self.assertNotIn("test-token", str(qobuz.errors))
        self.assertEqual(request.get_header("X-user-auth-token"), "test-token")

    def test_direct_id_and_paginated_album_details(self):
        data = album()
        data["tracks_count"] = 2
        second = data | {"tracks": {"items": [data["tracks"]["items"][0] | {"id": 43}]}}
        with patch.dict("os.environ", {"TAGGZ_QOBUZ_TOKEN": "test-token"}, clear=True):
            qobuz = Qobuz(
                Config(Path("/tmp"), qobuz_app_id="123456789"), set(), offline=False
            )
        with patch(
            "taggz.qobuz.urlopen",
            side_effect=[
                io.BytesIO(json.dumps(data).encode()),
                io.BytesIO(json.dumps(second).encode()),
            ],
        ) as get:
            response = qobuz.album(data["id"])
            self.assertEqual(qobuz.album(data["id"]), response)
        self.assertEqual([row["id"] for row in response["tracks"]["items"]], [42, 43])
        self.assertEqual(get.call_count, 2)
        self.assertIn("offset=1", get.call_args.args[0].full_url)

    def test_search_when_barcode_is_not_the_public_id(self):
        data = album()
        release = staged(from_qobuz(data))
        with patch.dict("os.environ", {"TAGGZ_QOBUZ_TOKEN": "test-token"}, clear=True):
            qobuz = Qobuz(
                Config(Path("/tmp"), qobuz_app_id="123456789"), set(), offline=False
            )
        results = {
            "albums": {
                "items": [
                    {"id": "badalbumid", "title": "Unrelated", "tracks_count": 1},
                    {"id": data["id"], "title": "Album", "tracks_count": 1},
                ]
            }
        }
        with (
            patch.object(
                qobuz,
                "album",
                side_effect=lambda key: data if key == data["id"] else {},
            ) as get_album,
            patch.object(qobuz, "get", return_value=results) as search,
        ):
            self.assertEqual(qobuz.albums(release), [data])
        self.assertEqual(search.call_args.args[0], "album/search")
        self.assertNotIn(
            "badalbumid", [call.args[0] for call in get_album.call_args_list]
        )

    def test_version_qualifier_does_not_hide_search_result_or_repeat_in_title(self):
        data = album() | {"version": "Deluxe Edition"}
        release = staged(from_qobuz(data))
        release.title = "Album (Deluxe Edition)"
        with patch.dict("os.environ", {"TAGGZ_QOBUZ_TOKEN": "test-token"}, clear=True):
            qobuz = Qobuz(
                Config(Path("/tmp"), qobuz_app_id="123456789"), set(), offline=False
            )
        result = {
            "albums": {
                "items": [
                    {"id": data["id"], "title": "Album", "version": "Deluxe Edition"}
                ]
            }
        }
        with (
            patch.object(
                qobuz,
                "album",
                side_effect=lambda key: data if key == data["id"] else {},
            ),
            patch.object(qobuz, "get", return_value=result) as search,
        ):
            self.assertEqual(qobuz.albums(release), [data])
        self.assertEqual(search.call_args.kwargs["query"], "Act Album")
        self.assertEqual(
            qobuz_version("Album (Deluxe Edition - Explicit)", "Deluxe Edition"),
            "Album (Deluxe Edition - Explicit)",
        )

    def test_maps_role_lists_classical_works_dates_and_advisory(self):
        edition = from_qobuz(album())
        (track,) = edition.tracks
        self.assertEqual(edition.date, "2021-04-17")
        self.assertEqual(edition.tags["originaldate"], ["2003-01-12"])
        self.assertEqual(edition.tags["genre"], ["Hip-Hop/Rap"])
        self.assertEqual(edition.tags["description"], ["First & last.\nNext."])
        self.assertEqual(track.title, "Suite: Movement")
        self.assertEqual(track.tags["artists"], ["Act", "Guest", "Featured"])
        self.assertEqual(track.tags["artist"], ["Act, Guest feat. Featured"])
        self.assertEqual(track.tags["composer"], ["A Writer"])
        self.assertEqual(track.tags["lyricist"], ["A Writer"])
        self.assertEqual(track.tags["mastering"], ["An Engineer"])
        self.assertEqual(track.tags["itunesadvisory"], ["0"])

    def test_same_qobuz_product_keeps_qobuz_tags_and_musicbrainz_identifiers(self):
        qobuz = from_qobuz(album())
        release = staged(qobuz)
        mb = Edition(
            "Act",
            "Album",
            release.date,
            "Digital Media",
            [
                Entry(
                    1,
                    1,
                    "Movement",
                    90.75,
                    {
                        "isrc": ["USABC2300123"],
                        "musicbrainz_trackid": ["recording"],
                    },
                )
            ],
            "musicbrainz",
            {"barcode": ["602465808140"], "musicbrainz_albumid": ["release"]},
        )
        discogs = replace(
            mb,
            source="discogs",
            tags={
                "barcode": ["602465808140"],
                "discogs_releaseid": ["12345"],
            },
            tracks=[
                Entry(1, 1, "Movement", 90, {"mastering": ["A Mastering Engineer"]})
            ],
        )
        catalog = Catalog(Config(Path("/tmp")), offline=True)
        with (
            patch.object(catalog, "qobuz", return_value=[qobuz]),
            patch.object(catalog, "musicbrainz", return_value=[mb]),
            patch.object(catalog, "discogs", return_value=[discogs]),
        ):
            proposal = propose(release, catalog, {})
        tags = proposal.tracks[0].tags
        self.assertEqual(tags["qobuz_album_id"], ["abc123def456"])
        self.assertEqual(tags["qobuz_track_id"], ["42"])
        self.assertEqual(tags["musicbrainz_albumid"], ["release"])
        self.assertEqual(tags["musicbrainz_trackid"], ["recording"])
        self.assertEqual(tags["discogs_releaseid"], ["12345"])
        self.assertEqual(tags["composer"], ["A Writer"])

    def test_complete_isrcs_and_exact_date_without_barcode_verify_qobuz_edition(self):
        edition = from_qobuz(album())
        release = replace(staged(edition), barcode="")
        catalog = Catalog(Config(Path("/tmp")), offline=True)
        with patch.object(catalog, "qobuz", return_value=[edition]):
            proposal = propose(release, catalog, {})
        self.assertEqual(proposal.tracks[0].tags["qobuz_album_id"], ["abc123def456"])
        self.assertIn("matching ISRCs", proposal.evidence)

    def test_alternate_digital_product_adds_recording_credits_without_qobuz_ids(self):
        edition = from_qobuz(album())
        release = replace(staged(edition), barcode="999999999999")
        catalog = Catalog(Config(Path("/tmp")), offline=True)
        diagnostics = {}
        with patch.object(catalog, "qobuz", return_value=[edition]):
            proposal = propose(release, catalog, diagnostics)
        tags = proposal.tracks[0].tags
        self.assertEqual(diagnostics["match_scope"], "track program")
        self.assertEqual(tags["composer"], ["A Writer"])
        self.assertNotIn("qobuz_album_id", tags)
        self.assertNotIn("qobuz_track_id", tags)
