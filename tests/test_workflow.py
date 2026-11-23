import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mutagen.flac import FLAC

from taggz.catalog import Catalog, Edition, Entry, from_discogs, from_musicbrainz
from taggz.config import load_config
from taggz.match import Library, common, propose, score, select
from taggz.release import Review, read_release, read_toc
from taggz.storage import Staging, atomic_json, read_header, replaygain_skip_reason


@unittest.skipUnless(
    shutil.which("flac") and shutil.which("metaflac"), "requires FLAC tools"
)
class Workflow(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.staging = self.root / "staging"
        self.library = self.root / "library"
        self.staging.mkdir()
        self.library.mkdir()
        self.config = self.root / "settings.toml"
        self.config.write_text(
            f'[paths]\nstaging = "{self.staging}"\nlibrary = "{self.library}"\n',
            encoding="utf-8",
        )
        self.flac = self.make_flac(44100)

    def make_flac(self, rate):
        wav = self.root / f"fixture-{rate}.wav"
        with wave.open(str(wav), "wb") as audio:
            audio.setparams((2, 2, rate, 0, "NONE", "not compressed"))
            audio.writeframes(b"\x10\x00\xf0\xff" * rate)
        flac = wav.with_suffix(".flac")
        subprocess.run(["flac", "--silent", "-o", str(flac), str(wav)], check=True)
        return flac

    def track(self, folder, name="Track01.flac", *, fixture=None, **tags):
        folder.mkdir(exist_ok=True)
        target = folder / name
        shutil.copyfile(fixture or self.flac, target)
        audio = FLAC(target)
        for key, value in tags.items():
            audio[key] = [value]
        audio.save()
        return target

    def pair(self):
        tags = dict(
            album="Album",
            albumartist="Artist",
            artist="Artist",
            title="Song",
            tracknumber="1",
            isrc="GBABC2400001",
            media="Digital Media",
            barcode="001999123456",
            date="2024-03-15",
        )
        library = self.track(
            self.library / "Artist — Album (2024) [WEB] [FLAC] [16B-44.1kHz]",
            "01 - Song.flac",
            **tags,
            composer="A Writer",
            qobuz_album_id="album-identity",
        )
        staged = self.track(self.staging / "Artist - Album", **tags)
        return staged, library

    def run_cli(self, *options, expected=0):
        container = bool(os.environ.get("TAGGZ_TEST_DOCKER"))
        module = "taggz.docker" if container else "taggz"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                module,
                "--config",
                str(self.config),
                "--offline",
                "--json",
                *options,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, expected, proc.stdout + proc.stderr)
        releases = json.loads(proc.stdout)["releases"]
        if container:
            for release in releases:
                for key in ("destination", "backup", "report_file"):
                    if key in release:
                        release[key] = str(
                            self.staging / Path(release[key]).relative_to("/staging")
                        )
        return releases

    def test_preview_is_read_only_and_apply_preserves_audio(self):
        staged, library = self.pair()
        lrc = staged.with_suffix(".lrc")
        lrc.write_text("[00:00.00]Test", encoding="utf-8")
        cue = staged.parent / "album.cue"
        cue.write_text(
            'FILE "Track01.wav" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n'
        )
        original = staged.read_bytes()
        library_before = library.read_bytes()
        gain_before = read_header(staged)[1]
        review = self.staging / "Unmatched"
        self.track(
            review,
            "01.flac",
            album="Unknown",
            artist="Unknown",
            title="Other",
            tracknumber="1",
        )
        preview = self.run_cli(expected=1)
        self.assertEqual(preview[0]["tracks"][0]["fields"]["composer"], ["A Writer"])
        self.assertEqual(preview[0]["input"]["track_count"], 1)
        self.assertEqual(preview[0]["candidates"][0]["decision"], "selected")
        self.assertEqual(
            preview[0]["tracks"][0]["previous_fields"]["tracknumber"], ["1"]
        )
        self.assertNotIn("verification", preview[0])
        self.assertEqual(staged.read_bytes(), original)
        self.assertFalse((self.staging / ".taggz").exists())
        self.assertFalse((self.staging / "completed").exists())
        reports = self.run_cli("--apply", expected=1)
        completed = Path(reports[0]["destination"])
        new = completed / "01 - Song.flac"
        audio = FLAC(new)
        self.assertEqual(reports[0]["same_library_edition"], [library.parent.name])
        self.assertEqual(audio["composer"], ["A Writer"])
        self.assertEqual(audio["qobuz_album_id"], ["album-identity"])
        self.assertEqual(audio["tracknumber"], ["01"])
        self.assertIn("replaygain_album_gain", audio)
        self.assertEqual(read_header(new)[1], gain_before)
        self.assertEqual(library.read_bytes(), library_before)
        self.assertEqual((completed / "01 - Song.lrc").read_bytes(), b"[00:00.00]Test")
        saved = json.loads(Path(reports[0]["report_file"]).read_text())
        self.assertEqual(saved["phase"], "completed")
        self.assertEqual(saved["verification"]["audio_frame_hashes_matched"], 1)
        self.assertTrue(saved["verification"]["replaygain_calculated"])
        self.assertIn(
            'FILE "01 - Song.flac" WAVE',
            (completed / "album.cue").read_text("utf-8-sig"),
        )
        self.assertEqual(
            Path(reports[1]["destination"]), self.staging / "needs-review" / "Unmatched"
        )
        with zipfile.ZipFile(reports[0]["backup"]) as backup:
            self.assertEqual(
                backup.read("sidecars/album.cue").splitlines()[0],
                b'FILE "Track01.wav" WAVE',
            )
            self.assertTrue(backup.read("headers/001.bin").startswith(b"fLaC"))
        self.assertEqual(self.run_cli(), [])

    def test_symlink_is_routed_without_following_it(self):
        staged, library = self.pair()
        original = library.read_bytes()
        (staged.parent / "linked.flac").symlink_to(library)
        (report,) = self.run_cli("--apply", expected=1)
        self.assertIn("unsupported link", report["reason"])
        self.assertTrue((Path(report["destination"]) / "linked.flac").is_symlink())
        self.assertEqual(library.read_bytes(), original)

    def test_hardlinked_track_is_not_modified_in_library(self):
        staged, library = self.pair()
        staged.unlink()
        os.link(library, staged)
        before = library.read_bytes()
        (report,) = self.run_cli("--apply")
        new = Path(report["destination"]) / "01 - Song.flac"
        self.assertFalse(new.samefile(library))
        self.assertEqual(library.read_bytes(), before)

    def test_existing_completed_release_is_not_overwritten(self):
        self.pair()
        (preview,) = self.run_cli()
        destination = self.staging / "completed" / preview["folder"]
        destination.mkdir(parents=True)
        (destination / "cover.jpg").write_bytes(b"original cover")
        (preview,) = self.run_cli(expected=1)
        self.assertIn("already exists", preview["reason"])
        (report,) = self.run_cli("--apply", expected=1)
        self.assertIn("already exists", report["reason"])
        self.assertEqual(report["phase"], "prepare completed destination")
        self.assertEqual((destination / "cover.jpg").read_bytes(), b"original cover")

    def test_review_report_records_candidate_rejection_and_eac_evidence(self):
        staged, library = self.pair()
        (staged.parent / "rip.log").write_text(
            "Exact Audio Copy\nTOC of the extracted CD\n1 | 0:00.00 | 0:01.00 | 0 | 74\n"
        )
        preview = subprocess.run(
            [sys.executable, "-m", "taggz", "--config", str(self.config), "--offline"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(preview.returncode, 1)
        self.assertIn("needs-review during match release", preview.stdout)
        self.assertIn("Candidate rejected:", preview.stdout)
        self.assertIn("medium does not match", preview.stdout)
        self.assertIn("Inspecting", preview.stderr)
        (report,) = self.run_cli("--apply", expected=1)
        self.assertEqual(report["phase"], "match release")
        self.assertIn("verified disc 1 TOC", report["input"]["rip_logs"]["rip.log"])
        self.assertEqual(report["input"]["medium"], "CD")
        (candidate,) = report["candidates"]
        self.assertEqual(candidate["source"], f"library: {library.parent.name}")
        self.assertEqual(candidate["decision"], "rejected")
        self.assertIn("staging is CD, candidate is Digital Media", candidate["reason"])
        saved = json.loads(Path(report["report_file"]).read_text())
        self.assertEqual(saved["candidates"], report["candidates"])

    def test_mixed_sample_rates_skip_replaygain_and_complete(self):
        staged, library = self.pair()
        high_rate = self.make_flac(48000)
        for seed in (staged, library):
            tags = {key: values[0] for key, values in FLAC(seed).items()}
            self.track(
                seed.parent,
                "Track02.flac",
                fixture=high_rate,
                **(
                    tags
                    | {"tracknumber": "2", "title": "Other", "isrc": "GBABC2400002"}
                ),
            )
        audio = FLAC(staged)
        audio["replaygain_track_gain"] = ["-5.00 dB"]
        audio.save()
        (preview,) = self.run_cli()
        self.assertIn("common sample rate", preview["replaygain_skip_reason"])
        (report,) = self.run_cli("--apply")
        destination = Path(report["destination"])
        self.assertFalse(report["verification"]["replaygain_calculated"])
        self.assertEqual(
            FLAC(destination / "01 - Song.flac")["replaygain_track_gain"], ["-5.00 dB"]
        )
        self.assertEqual(FLAC(destination / "02 - Other.flac").info.sample_rate, 48000)

    def test_mixed_bit_depths_and_channels_skip_replaygain(self):
        staged, _ = self.pair()
        release = read_release(staged.parent)
        original = release.tracks[0]
        release.tracks = [original, replace(original, number=2, bits=24)]
        self.assertIn("common bit depth", replaygain_skip_reason(release))
        release.tracks[1] = replace(original, number=2, channels=1)
        self.assertIn("common channel count", replaygain_skip_reason(release))

    def test_cue_isrcs_follow_index_one_across_eac_file_boundaries(self):
        staged, library = self.pair()
        for seed in (staged, library):
            tags = {key: values[0] for key, values in FLAC(seed).items()}
            del tags["isrc"]
            self.track(
                seed.parent,
                "Track02.flac",
                **(tags | {"tracknumber": "2", "title": "Other"}),
            )
        del_audio = FLAC(staged)
        del del_audio["isrc"]
        del_audio.save()
        cue = staged.parent / "rip.cue"
        contents = (
            'FILE "Track01.wav" WAVE\n  TRACK 01 AUDIO\n'
            "    ISRC GBABC2400001\n    INDEX 01 00:00:00\n"
            "  TRACK 02 AUDIO\n    ISRC GBABC2400002\n    INDEX 00 00:00:71\n"
            'FILE "Track02.wav" WAVE\n    INDEX 01 00:00:00\n'
        )
        cue.write_text(contents)
        release = read_release(staged.parent)
        release.barcode = release.date = ""
        candidate = Edition(
            "Artist",
            "Album",
            "2024",
            "Digital Media",
            [
                Entry(1, t.number, t.title, 1, {"isrc": [f"GBABC240000{t.number}"]})
                for t in release.tracks
            ],
            "catalog",
        )
        self.assertIn("ISRCs", score(release, candidate).evidence)
        (report,) = self.run_cli("--apply")
        finished = Path(report["destination"])
        self.assertEqual(report["input"]["cue_isrc_count"], 2)
        self.assertEqual(FLAC(finished / "02 - Other.flac")["isrc"], ["GBABC2400002"])
        self.assertIn(
            'FILE "02 - Other.flac" WAVE', (finished / cue.name).read_text("utf-8-sig")
        )

    def test_cue_with_mismatched_track_number_cannot_supply_isrc(self):
        staged, _ = self.pair()
        cue = staged.parent / "rip.cue"
        cue.write_text(
            'FILE "Track01.wav" WAVE\n  TRACK 02 AUDIO\n'
            "    ISRC GBABC2400002\n    INDEX 01 00:00:00\n"
        )
        (report,) = self.run_cli(expected=1)
        self.assertEqual(report["input"]["cue_isrc_count"], 0)
        self.assertIn("cue track 02 points to", report["input"]["cue_notes"][cue.name])
        self.assertEqual(report["phase"], "plan sidecars")

    def test_flat_cd_rip_logs_are_assigned_by_disc_suffix(self):
        staged, _ = self.pair()
        tags = {key: values[0] for key, values in FLAC(staged).items()}
        self.track(staged.parent, "Disc2Track01.flac", **(tags | {"discnumber": "2"}))
        for disc in (1, 2):
            (staged.parent / f"Artist - Album CD{disc}.log").write_text(
                "Exact Audio Copy\nTOC of the extracted CD\n1 | 0:00.00 | 0:01.00 | 0 | 74\n"
            )
        release = read_release(staged.parent)
        self.assertEqual({toc.disc for toc in release.tocs}, {1, 2})

    def test_toc_tie_keeps_recordings_but_not_pressing_identity(self):
        staged, _ = self.pair()
        audio = FLAC(staged)
        audio["media"] = ["CD"]
        del audio["barcode"]
        audio.save()
        (staged.parent / "rip.log").write_text(
            "Exact Audio Copy\nTOC of the extracted CD\n1 | 0:00.00 | 0:01.00 | 0 | 74\n"
        )
        release = read_release(staged.parent)
        (toc,) = release.tocs
        self.assertEqual(toc.query, "1 1 225 150")
        self.assertIsNone(read_toc("Exact Audio Copy", release.tracks))
        entry = Entry(1, 1, "Song", 1.0, {"musicbrainz_trackid": ["recording"]})
        common = {"musicbrainz_releasegroupid": ["group"]}
        editions = [
            Edition(
                "Artist",
                "Album",
                "2024",
                "CD",
                [entry],
                n,
                common | {"musicbrainz_albumid": [n]},
                {(1, toc.id)},
            )
            for n in ("one", "two")
        ]
        hits = [score(release, e) for e in editions]
        selected, evidence = select(hits)
        self.assertEqual(len(selected), 2)
        self.assertIn("shared CD", evidence)
        editions[1].tracks = [
            Entry(1, 1, "Song", 1.0, {"musicbrainz_trackid": ["another"]})
        ]
        with self.assertRaises(Review):
            select([score(release, e) for e in editions])

    def test_release_alias_and_explicit_variant(self):
        staged, _ = self.pair()
        root = staged.parent.rename(self.staging / "Artist - Album (Mix Version)")
        release = read_release(root)
        candidate = Edition(
            "Canonical Name",
            "Album",
            release.date,
            release.medium,
            [Entry(1, 1, "Song (Mix Version)", 1.0)],
            "catalog",
            aliases={"Artist"},
            variant="Mix Version",
        )
        self.assertIsNotNone(score(release, candidate))
        release.root = self.staging / "Artist - Album"
        self.assertIsNone(score(release, candidate))

    def test_exact_cd_toc_accepts_typo_and_extra_track_annotations(self):
        staged, _ = self.pair()
        release = read_release(staged.parent)
        release.medium = "CD"
        release.barcode = ""
        original = [
            "Helpless Automation",
            "Crazy (Non LP B-Side",
            "Underground (Live)",
            "Who Can It Be Now",
            "Down Under",
        ]
        catalog = [
            "Helpless Automaton",
            "Bonus Tracks: Crazy",
            "Underground (live, Brazil 1996)",
            "Who Can It Be Now",
            "Down Under",
        ]
        release.tracks = [
            replace(release.tracks[0], number=number, title=title)
            for number, title in enumerate(original, 1)
        ]
        log = "TOC of the extracted CD\n" + "\n".join(
            f"{pos} | start | length | {(pos - 1) * 75} | {pos * 75 - 1}"
            for pos in range(1, 6)
        )
        toc = read_toc(log, release.tracks)
        self.assertIsNotNone(toc)
        release.tocs = [toc]
        entries = [
            Entry(1, pos, title, 1.0, {"musicbrainz_trackid": [str(pos)]})
            for pos, title in enumerate(catalog, 1)
        ]
        first = Edition(
            "Artist",
            "Album",
            "2003",
            "CD",
            entries,
            "first",
            {"musicbrainz_releasegroupid": ["group"]},
            {(1, toc.id)},
        )
        second_entries = entries[:]
        second_entries[2] = replace(
            entries[2], tags={"musicbrainz_trackid": ["alternative"]}
        )
        second = replace(first, source="second", tracks=second_entries)
        selected, _ = select([score(release, first), score(release, second)])
        self.assertEqual(len(selected), 2)
        self.assertNotIn(
            "musicbrainz_trackid", common([m.edition.tracks[2].tags for m in selected])
        )
        self.assertIsNone(score(release, replace(first, discids=set())))
        bad = entries[:]
        bad[2] = replace(entries[2], title="Underground")
        self.assertIsNone(score(release, replace(first, tracks=bad)))

    def test_xld_log_and_compound_cd_folder_keep_the_physical_medium(self):
        staged, _ = self.pair()
        root = staged.parent.rename(
            self.staging / "Artist - Album (2024) [Label, 602465081497] [FLAC-CD]"
        )
        audio = FLAC(root / staged.name)
        del audio["barcode"]
        audio.save()
        release = read_release(root)
        self.assertEqual(release.medium, "CD")
        self.assertEqual(release.barcode, "602465081497")

        root = root.rename(self.staging / "Artist - Album [FLAC]")
        (root / "rip.log").write_text(
            "X Lossless Decoder version 20230627\n\n"
            "XLD extraction logfile\n\n"
            "TOC of the extracted CD\n1 | 0:00.00 | 0:01.00 | 0 | 74\n"
        )
        release = read_release(root)
        self.assertEqual(release.medium, "CD")
        self.assertEqual(release.tocs[0].query, "1 1 225 150")

    def test_program_match_keeps_staged_edition_identifiers(self):
        staged, library = self.pair()
        for number in (2, 3, 4, 5):
            for reference in (staged, library):
                tags = {key: values[0] for key, values in FLAC(reference).items()}
                tags.update(tracknumber=str(number), isrc=f"GBABC240000{number}")
                self.track(reference.parent, f"{number:02d} - Song.flac", **tags)
        for track in staged.parent.glob("*.flac"):
            audio = FLAC(track)
            audio["barcode"] = ["602465081497"]
            audio.save()
        (report,) = self.run_cli()
        self.assertEqual(report["match_scope"], "track program")
        self.assertNotIn("qobuz_album_id", report["tracks"][0]["fields"])
        self.assertNotIn("barcode", report["tracks"][0]["fields"])
        self.assertEqual(report["tracks"][0]["fields"]["composer"], ["A Writer"])
        self.assertEqual(report["withheld_release_fields"], ["qobuz_album_id"])
        candidate = report["candidates"][0]
        self.assertEqual(candidate["matching_pcm_checksums"], 5)
        self.assertEqual(candidate["matching_isrcs"], 5)
        self.assertIn("barcode differs", candidate["edition_reason"])

    def test_verified_cd_program_can_use_web_track_list_without_web_ids(self):
        staged, library = self.pair()
        library.unlink()
        tags = {key: values[0] for key, values in FLAC(staged).items()}
        for number in (2, 3):
            self.track(
                staged.parent,
                f"{number:02d} - Song.flac",
                **(tags | {"tracknumber": str(number), "title": f"Song {number}"}),
            )
        (staged.parent / "rip.log").write_text(
            "X Lossless Decoder\nTOC of the extracted CD\n"
            "1 | start | length | 0 | 74\n"
            "2 | start | length | 75 | 149\n"
            "3 | start | length | 150 | 224\n"
        )
        release = read_release(staged.parent)
        catalog = Catalog(load_config(self.config), offline=True)
        entries = [
            Entry(1, t.number, t.title, 1.0, {"qobuz_track_id": [str(t.number)]})
            for t in release.tracks
        ]
        other = Edition(
            "Artist",
            "Album",
            "2025-04-01",
            "Digital Media",
            entries,
            "web",
            {"qobuz_album_id": ["other"], "barcode": ["602465081497"]},
        )
        with patch.object(catalog, "musicbrainz", return_value=[other]):
            proposal = propose(release, Library(self.library), catalog, {})
        self.assertIn("CD rip tables", proposal.evidence)
        self.assertIn("(2024) [CD]", proposal.folder)
        self.assertEqual(proposal.tracks[0].tags["media"], ["CD"])
        self.assertEqual(proposal.tracks[0].tags["barcode"], [tags["barcode"]])
        self.assertNotIn("qobuz_album_id", proposal.tracks[0].tags)
        self.assertNotIn("qobuz_track_id", proposal.tracks[0].tags)

    def test_named_version_wins_over_the_generic_release(self):
        staged, library = self.pair()
        library.unlink()
        release = read_release(
            staged.parent.rename(self.staging / "Artist - Album (Mix Version)")
        )
        catalog = Catalog(load_config(self.config), offline=True)
        generic = Edition(
            "Artist",
            "Album",
            release.date,
            "Digital Media",
            [Entry(1, 1, "Song", 1.0)],
            "generic",
        )
        version = replace(
            generic,
            source="explicit",
            variant="Mix Version",
            tracks=[Entry(1, 1, "Song (Mix Version)", 1.1)],
        )
        with patch.object(catalog, "musicbrainz", return_value=[generic, version]):
            proposal = propose(release, Library(self.library), catalog, {})
        self.assertEqual(proposal.sources, ["explicit"])
        self.assertIn("Album (Mix Version)", proposal.folder)

    def test_musicbrainz_counts_merged_editions_once(self):
        staged, _ = self.pair()
        release = read_release(staged.parent)
        old_id = (
            "a" * 8 + "-" + "a" * 4 + "-" + "a" * 4 + "-" + "a" * 4 + "-" + "a" * 12
        )
        new_id = old_id.replace("a", "b")
        release.tags["musicbrainz_albumid"] = [old_id]
        catalog = Catalog(load_config(self.config), offline=False)
        detail = {
            "id": new_id,
            "title": "Album",
            "media": [{"format": "Digital Media"}],
        }
        with patch.object(
            catalog, "get", side_effect=[{"releases": [{"id": new_id}]}, detail, detail]
        ):
            (edition,) = catalog.musicbrainz(release)
        self.assertEqual(edition.tags["musicbrainz_albumid"], [new_id])

    def test_discogs_slug_link_supplements_one_edition_with_sparse_credits(self):
        staged, library = self.pair()
        library.unlink()
        release = read_release(staged.parent)
        catalog = Catalog(load_config(self.config), offline=True)
        preferred = from_musicbrainz(
            {
                "id": "album",
                "title": "Album",
                "relations": [
                    {
                        "type": "discogs",
                        "url": {
                            "resource": "https://www.discogs.com/es/release/12345-Artist-Album"
                        },
                    }
                ],
            }
        )
        preferred = replace(
            preferred,
            artist="Artist",
            medium="Digital Media",
            tags={"barcode": [release.barcode]},
            tracks=[Entry(1, 1, "Song", 1.0)],
        )
        linked = from_discogs(
            {
                "id": 12345,
                "title": "Album",
                "artists": [{"name": "Artist"}],
                "formats": [{"name": "File"}],
                "tracklist": [
                    {
                        "position": "1",
                        "type_": "track",
                        "title": "Song",
                        "extraartists": [
                            {"name": "A Mastering Engineer", "role": "Mastered By"}
                        ],
                    }
                ],
            }
        )
        diagnostics = {}
        with (
            patch.object(catalog, "musicbrainz", return_value=[preferred]),
            patch.object(catalog, "discogs", return_value=[linked]) as fetch,
        ):
            proposal = propose(release, Library(self.library), catalog, diagnostics)
        self.assertEqual(fetch.call_args.args[1], [linked.source])
        self.assertEqual(proposal.tracks[0].tags["mastering"], ["A Mastering Engineer"])
        self.assertEqual(diagnostics["candidates"][-1]["decision"], "supplemental")

    def test_discogs_vinyl_sides_map_to_disc_positions(self):
        edition = from_discogs(
            {
                "id": 12345,
                "title": "Album",
                "formats": [{"name": "Vinyl"}],
                "tracklist": [
                    {"position": side, "type_": "track", "title": side}
                    for side in ("A1", "A2", "B1", "C1", "D1")
                ],
            }
        )
        self.assertEqual(
            [(entry.disc, entry.number) for entry in edition.tracks],
            [(1, 1), (1, 2), (1, 3), (2, 1), (2, 2)],
        )
        self.assertEqual(edition.tracks[-1].tags["vinylposition"], ["D1"])

    def test_multiple_source_artists_are_retained_without_catalog_credits(self):
        staged, library = self.pair()
        audio = FLAC(staged)
        audio["artist"] = ["Artist", "Guest"]
        audio.save()
        reference = FLAC(library)
        del reference["artist"]
        reference.save()
        (report,) = self.run_cli("--apply")
        tagged = FLAC(Path(report["destination"]) / "01 - Song.flac")
        self.assertEqual(tagged["artist"], ["Artist, Guest"])
        self.assertEqual(tagged["artists"], ["Artist", "Guest"])

    def test_interrupted_publish_restores_original_on_next_apply(self):
        staged, _ = self.pair()
        state = Staging(self.staging)
        state.setup()
        identifier = "a" * 32
        job = state.jobs / identifier
        job.mkdir()
        (job / "candidate").mkdir()
        atomic_json(
            job / "transaction.json",
            {
                "source": staged.parent.name,
                "destination": "Album",
                "candidate_inode": [0, 0],
            },
        )
        backup = state.backups / (identifier + ".zip")
        backup.touch()
        staged.parent.rename(job / "original")
        state.setup()
        self.assertTrue(staged.is_file())
        self.assertFalse(job.exists())
        self.assertFalse(backup.exists())


class Configuration(unittest.TestCase):
    def test_rejects_overlapping_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "library"
            library.mkdir()
            config = root / "settings.toml"
            config.write_text(f'[paths]\nstaging = "{root}"\nlibrary = "{library}"\n')
            with self.assertRaisesRegex(ValueError, "non-overlapping"):
                load_config(config)

    def test_docker_mounts_library_read_only_in_apply(self):
        from taggz.docker import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "library").mkdir()
            (root / "staging").mkdir()
            config = root / "settings.toml"
            config.write_text(
                f'[paths]\nstaging = "{root / "staging"}"\nlibrary = "{root / "library"}"\n'
            )
            with patch.object(
                sys, "argv", ["taggz-docker", "--config", str(config), "--apply"]
            ):
                with patch("taggz.docker.subprocess.call", return_value=0) as call:
                    self.assertEqual(main(), 0)
            (command,) = call.call_args.args
            self.assertIn(
                f"type=bind,src={root / 'library'},dst=/library,readonly,bind-propagation=rprivate,bind-recursive=readonly",
                command,
            )
            self.assertIn(
                f"type=bind,src={root / 'staging'},dst=/staging,bind-propagation=rprivate,bind-recursive=disabled",
                command,
            )
