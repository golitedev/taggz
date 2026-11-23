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

from taggz.catalog import Edition, Entry
from taggz.config import load_config
from taggz.match import common, score, select
from taggz.release import Review, read_release, read_toc
from taggz.storage import Staging, atomic_json, read_header


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
        wav = self.root / "fixture.wav"
        with wave.open(str(wav), "wb") as audio:
            audio.setparams((2, 2, 44100, 0, "NONE", "not compressed"))
            audio.writeframes(b"\x10\x00\xf0\xff" * 44100)
        self.flac = self.root / "fixture.flac"
        subprocess.run(["flac", "--silent", "-o", str(self.flac), str(wav)], check=True)

    def track(self, folder, name="Track01.flac", **tags):
        folder.mkdir(exist_ok=True)
        target = folder / name
        shutil.copyfile(self.flac, target)
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
        (report,) = self.run_cli("--apply", expected=1)
        self.assertIn("already exists", report["reason"])
        self.assertEqual((destination / "cover.jpg").read_bytes(), b"original cover")

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
