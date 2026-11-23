import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import zipfile
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from mutagen.flac import FLAC

from .match import Proposal
from .release import Review, signature, snapshot
from .sidecars import Sidecar

RESERVED = {"completed", "needs-review"}


def atomic_json(path: Path, data: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_header(path: Path) -> tuple[bytes, str]:
    with path.open("rb") as stream:
        header = bytearray(stream.read(4))
        if header != b"fLaC":
            raise Review(f"unsupported FLAC header: {path.name}")
        while True:
            block = stream.read(4)
            if len(block) != 4:
                raise Review(f"truncated FLAC header: {path.name}")
            length = int.from_bytes(block[1:], "big")
            payload = stream.read(length)
            if len(payload) != length:
                raise Review(f"truncated FLAC metadata: {path.name}")
            header.extend(block)
            header.extend(payload)
            if block[0] & 128:
                break
        return bytes(header), hashlib.file_digest(stream, "sha256").hexdigest()


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def vacant(path: Path) -> bool:
    return not os.path.lexists(path)


def require_directory(path: Path, device: int) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError(f"expected a real staging directory: {path}")
    path.mkdir(exist_ok=True)
    if path.stat().st_dev != device:
        raise ValueError(f"must be on the staging filesystem: {path}")


@contextmanager
def staging_lock(root: Path):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("another Taggz apply is already running") from error
        yield
    finally:
        os.close(fd)


class Staging:
    def __init__(self, root: Path):
        self.root = root
        self.completed = root / "completed"
        self.review = root / "needs-review"
        self.state = root / ".taggz"
        self.jobs = self.state / "work"
        self.backups = self.state / "backups"
        self.reports = self.state / "reports"

    def setup(self) -> None:
        device = self.root.stat().st_dev
        for folder in (
            self.completed,
            self.review,
            self.state,
            self.jobs,
            self.backups,
            self.reports,
        ):
            require_directory(folder, device)
        for folder in sorted(self.jobs.iterdir()):
            if (
                folder.is_symlink()
                or not folder.is_dir()
                or not re.fullmatch(r"[0-9a-f]{32}", folder.name)
            ):
                raise ValueError(f"unexpected workspace: {folder}")
            self.recover(folder)

    def recover(self, job: Path) -> None:
        """Finish cleanup or hand the original folder back after an interruption."""
        held = job / "original"
        published = False
        saved = job / "transaction.json"
        if held.is_dir():
            transaction = json.loads(saved.read_text(encoding="utf-8"))
            old_name, new_name = transaction["source"], transaction["destination"]
            if any(
                Path(name).name != name or name in ("", ".", "..")
                for name in (old_name, new_name)
            ):
                raise ValueError(f"invalid recovery journal: {job}")
            source = self.root / old_name
            destination = self.completed / new_name
            published_inode = transaction["candidate_inode"]
            if (
                not (job / "candidate").exists()
                and destination.is_dir()
                and list(signature(destination)[:2]) == published_inode
            ):
                # Published successfully; the durable backup is already in place.
                published = True
            elif vacant(source):
                held.rename(source)
            else:
                raise ValueError(
                    f"recovery needs attention: original is retained at {held}"
                )
        if not published and saved.exists():
            (self.backups / (job.name + ".zip")).unlink(missing_ok=True)
            (self.reports / (job.name + ".json")).unlink(missing_ok=True)
        shutil.rmtree(job)

    def route_review(self, source: Path, report: dict) -> Path:
        if source.parent != self.root or source.is_symlink() or not source.is_dir():
            raise ValueError(f"cannot move release into review: {source}")
        name = source.name
        target = self.review / name
        if not vacant(target):
            suffix = f" [{uuid4().hex[:8]}]"
            stem = name.encode("utf-8")[: 245 - len(suffix)].decode(
                "utf-8", errors="ignore"
            )
            target = self.review / (stem + suffix)
        if not vacant(target):
            raise Review(f"review destination already exists: {target}")
        identifier = uuid4().hex
        report_path = self.reports / (identifier + ".json")
        report["destination"] = str(target)
        report["report_file"] = str(report_path)
        source.rename(target)
        try:
            atomic_json(report_path, report)
        except BaseException:
            if vacant(source):
                target.rename(source)
            raise
        return target

    def apply(
        self,
        proposal: Proposal,
        sidecars: list[Sidecar],
        report: dict,
        *,
        replaygain: bool,
    ) -> Path:
        source = proposal.release.root
        destination = self.completed / proposal.folder
        if not vacant(destination):
            raise Review(f"completed destination already exists: {destination.name}")
        if snapshot(source) != proposal.release.files:
            raise Review("source changed since preview")
        identifier = uuid4().hex
        job = self.jobs / identifier
        job.mkdir()
        candidate = job / "candidate"
        packed = job / "original-metadata.zip"
        backup_path = self.backups / (identifier + ".zip")
        report_path = self.reports / (identifier + ".json")
        try:
            shutil.copytree(source, candidate, symlinks=True)
            if set(snapshot(candidate)) != set(proposal.release.files):
                raise Review("file list changed during copying")
            report["audio_sha256"] = self.prepare(
                candidate, proposal, sidecars, packed, replaygain
            )
            if snapshot(source) != proposal.release.files:
                raise Review("source changed during preparation")
            if not vacant(destination):
                raise Review(
                    f"completed destination already exists: {destination.name}"
                )
            atomic_json(
                job / "transaction.json",
                {
                    "source": source.name,
                    "destination": destination.name,
                    "candidate_inode": list(signature(candidate)[:2]),
                },
            )
            report.update(
                destination=str(destination),
                backup=str(backup_path),
                report_file=str(report_path),
            )
            packed.rename(backup_path)
            atomic_json(report_path, report)
            source.rename(job / "original")
            candidate.rename(destination)
        finally:
            self.recover(job)
        return destination

    def prepare(
        self,
        candidate: Path,
        proposal: Proposal,
        sidecars: list[Sidecar],
        packed: Path,
        replaygain: bool,
    ) -> dict[str, str]:
        audio_hashes = {}
        expected_metadata = {}
        extras = {}
        moves = candidate.parent / "moves"
        moves.mkdir()
        with zipfile.ZipFile(packed, "x", compression=zipfile.ZIP_DEFLATED) as backup:
            index = {
                "source": proposal.release.root.name,
                "destination": proposal.folder,
                "tracks": {},
                "sidecars": {},
            }
            for idx, entry in enumerate(proposal.tracks):
                origin = candidate / entry.track.path
                header, sha = read_header(origin)
                header_name = f"headers/{idx + 1:03d}.bin"
                backup.writestr(header_name, header)
                index["tracks"][str(entry.track.path)] = {
                    "header": header_name,
                    "new": str(entry.path),
                    "audio_sha256": sha,
                    "tags": entry.track.tags,
                }
                tagged = FLAC(origin)
                if dict(tagged.tags or {}) != entry.track.tags:
                    raise Review(f"source tags changed: {entry.track.path}")
                expected_metadata[entry.path] = (
                    tagged.info.bits_per_sample,
                    tagged.info.sample_rate,
                    tagged.info.channels,
                    tagged.info.total_samples,
                    tagged.info.md5_signature,
                    [image.write() for image in tagged.pictures],
                )
                if tagged.tags is None:
                    tagged.add_tags()
                tagged.tags.clear()
                for key, values in entry.tags.items():
                    tagged[key.upper()] = values
                tagged.save()
                audio_hashes[str(entry.path)] = sha
            for item in sidecars:
                path = candidate / item.old
                if item.contents is not None:
                    backup.write(path, "sidecars/" + item.old.as_posix())
                    path.write_bytes(item.contents)
                if item.old != item.new or item.contents is not None:
                    index["sidecars"][str(item.old)] = str(item.new)
                extras[item.new] = digest(path)
            backup.writestr(
                "index.json", json.dumps(index, ensure_ascii=False, indent=2)
            )

        # Park all renamed files first so swaps and case-only renames are safe.
        paths = [(item.track.path, item.path) for item in proposal.tracks]
        paths += [(item.old, item.new) for item in sidecars if item.old != item.new]
        for i, (old, _) in enumerate(paths):
            (candidate / old).rename(moves / str(i))
        for i, (_, new) in enumerate(paths):
            target = candidate / new
            target.parent.mkdir(parents=True, exist_ok=True)
            if not vacant(target):
                raise Review(f"filename collision: {new}")
            (moves / str(i)).rename(target)
        if replaygain:
            result = subprocess.run(
                [
                    "metaflac",
                    "--add-replay-gain",
                    *[str(candidate / item.path) for item in proposal.tracks],
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=600,
            )
            if result.returncode:
                raise Review(f"ReplayGain failed: {result.stderr.strip()}")
        expected_gain = {
            "replaygain_track_gain",
            "replaygain_track_peak",
            "replaygain_album_gain",
            "replaygain_album_peak",
        }
        gain_fields = expected_gain | {"replaygain_reference_loudness"}
        for entry in proposal.tracks:
            path = candidate / entry.path
            tagged = FLAC(path)
            if read_header(path)[1] != audio_hashes[str(entry.path)]:
                raise Review(f"audio frames changed: {entry.path}")
            metadata = (
                tagged.info.bits_per_sample,
                tagged.info.sample_rate,
                tagged.info.channels,
                tagged.info.total_samples,
                tagged.info.md5_signature,
                [image.write() for image in tagged.pictures],
            )
            if metadata != expected_metadata[entry.path]:
                raise Review(
                    f"audio stream information or artwork changed: {entry.path}"
                )
            if any(
                tagged.get(key) != value
                for key, value in entry.tags.items()
                if not replaygain or key not in gain_fields
            ):
                raise Review(f"written tags failed verification: {entry.path}")
            if replaygain and not expected_gain <= set(tagged.keys()):
                raise Review(f"missing ReplayGain tags: {entry.path}")
        if any(digest(candidate / path) != value for path, value in extras.items()):
            raise Review("a sidecar changed unexpectedly")
        return audio_hashes
