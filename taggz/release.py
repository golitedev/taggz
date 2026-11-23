import base64
import hashlib
import os
import re
import stat
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from mutagen import MutagenError
from mutagen.flac import FLAC


class Review(ValueError):
    """The release needs a decision before it can be tagged."""


AUDIO_SUFFIXES = {
    ".flac",
    ".mp3",
    ".m4a",
    ".wav",
    ".aiff",
    ".aif",
    ".ape",
    ".wv",
    ".ogg",
    ".opus",
    ".alac",
    ".dsf",
    ".dff",
    ".tta",
    ".mka",
    ".wma",
}
DISC = re.compile(r"^(?:cd|disc|disk)\s*0*(\d+)$", re.I)
TRACK = re.compile(r"^0*(\d{1,3})(?=[ ._-])")
FEATURE = re.compile(r"\s*[\[(](?:feat(?:uring)?\.?|ft\.?)\s+[^)\]]+[)\]]", re.I)
FORMAT = re.compile(r"\s*\(\d{4}\)(?:\s*\[[^]]+\])*\s*(?:- INCOMPLETE)?$")
CUE_ISRC = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$")
TOC_ROW = re.compile(
    r"^\s*(\d+)\s*\|[^\n|]+\|[^\n|]+\|\s*(\d+)\s*\|\s*(\d+)\s*$",
    re.M,
)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold().replace("&", " and "))
    return "".join(letter for letter in text if letter.isalnum())


def track_key(title: str) -> str:
    return normalize(FEATURE.sub("", title))


def barcode_key(value: str) -> str:
    return re.sub(r"\D", "", value).lstrip("0")


def first(tags: dict[str, list[str]], *keys: str) -> str:
    return next((v.strip() for key in keys for v in tags.get(key, []) if v.strip()), "")


def text_file(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def signature(path: Path) -> tuple[int, int, int, int, int]:
    s = path.lstat()
    return s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns


def snapshot(root: Path) -> dict[Path, tuple[int, int, int, int, int]]:
    entries = {}
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(folder) / name
            info = signature(path)
            if not (stat.S_ISREG(info[2]) or stat.S_ISDIR(info[2])):
                raise Review(
                    f"unsupported link or special file: {path.relative_to(root)}"
                )
            entries[path.relative_to(root)] = info
    return entries


@dataclass(frozen=True)
class Track:
    path: Path
    disc: int
    number: int
    title: str
    seconds: float
    bits: int
    rate: int
    pcm_md5: int
    tags: dict[str, list[str]]

    @property
    def isrc(self) -> str:
        code = re.sub(r"[^A-Z0-9]", "", first(self.tags, "isrc").upper())
        return code if CUE_ISRC.fullmatch(code) else ""


@dataclass(frozen=True)
class Toc:
    disc: int
    id: str
    query: str


@dataclass
class Release:
    root: Path
    artist: str
    title: str
    date: str
    barcode: str
    catalog: str
    medium: str
    tracks: list[Track]
    tocs: list[Toc]
    files: dict[Path, tuple[int, int, int, int, int]]
    cue_isrcs: dict[tuple[int, int], str]
    rip_logs: dict[str, str] = field(default_factory=dict)

    @property
    def tags(self) -> dict[str, list[str]]:
        return self.tracks[0].tags


def number(value: str, label: str) -> int:
    try:
        n = int(value.split("/", 1)[0])
    except ValueError as error:
        raise Review(f"invalid {label}: {value!r}") from error
    if not 1 <= n <= 999:
        raise Review(f"invalid {label}: {value!r}")
    return n


def disc_number(path: Path) -> int:
    for part in reversed(path.parent.parts):
        match = DISC.fullmatch(part)
        if match:
            return int(match[1])
    return 1


def read_track(root: Path, path: Path) -> Track:
    try:
        audio = FLAC(root / path)
    except (MutagenError, OSError) as error:
        raise Review(f"cannot read {path}: {error}") from error
    tags = {k.lower(): list(v) for k, v in (audio.tags or {}).items()}
    match = TRACK.match(path.stem)
    pos = first(tags, "tracknumber") or (match[1] if match else "")
    disc = number(first(tags, "discnumber") or str(disc_number(path)), "disc number")
    title = first(tags, "title") or TRACK.sub("", path.stem).lstrip(" ._-")
    if not title or audio.info.length <= 0:
        raise Review(f"missing track title or audio duration: {path}")
    return Track(
        path,
        disc,
        number(pos, f"track number for {path}"),
        title,
        audio.info.length,
        audio.info.bits_per_sample,
        audio.info.sample_rate,
        audio.info.md5_signature,
        tags,
    )


def read_toc(log: str, tracks: list[Track]) -> Toc | None:
    """Convert a verified EAC track table to the MusicBrainz CD disc ID."""
    table = TOC_ROW.findall(log.partition("TOC of the extracted CD")[2])
    if len(table) != len(tracks) or not 1 <= len(table) <= 99:
        return None
    sectors = [(int(position), int(start), int(end)) for position, start, end in table]
    previous_end = -1
    for (pos, start, end), track in zip(sectors, tracks, strict=True):
        if pos != track.number or start != previous_end + 1 or end < start:
            return None
        if abs((end - start + 1) / 75 - track.seconds) > 2:
            return None
        previous_end = end
    leadout = sectors[-1][2] + 151
    offsets = [start + 150 for _, start, _ in sectors]
    formatted = f"01{len(sectors):02X}{leadout:08X}" + "".join(
        f"{value:08X}" for value in offsets + [0] * (99 - len(sectors))
    )
    disc_id = base64.b64encode(
        hashlib.sha1(formatted.encode(), usedforsecurity=False).digest()
    )
    query = " ".join(map(str, [1, len(sectors), leadout, *offsets]))
    return Toc(
        tracks[0].disc, disc_id.decode().translate(str.maketrans("+/=", "._-")), query
    )


def folder_identity(path: Path) -> tuple[str, str]:
    for separator in (" — ", " – ", " - "):
        if separator in path.name:
            artist, title = path.name.split(separator, 1)
            return artist.strip(), FORMAT.sub("", title).strip()
    return "", ""


def agreed(values: list[str], key, label: str) -> str:
    values = [value for value in values if value]
    if len({key(value) for value in values}) > 1:
        raise Review(f"conflicting {label} in source metadata")
    return values[0] if values else ""


def read_release(root: Path) -> Release:
    entries = snapshot(root)
    paths = [path for path, info in entries.items() if stat.S_ISREG(info[2])]
    formats = {path.suffix.lower() for path in paths} & AUDIO_SUFFIXES
    if formats != {".flac"}:
        raise Review(
            "expected FLAC audio only"
            + (f"; found {', '.join(sorted(formats))}" if formats else "")
        )
    tracks = sorted(
        (read_track(root, p) for p in paths if p.suffix.lower() == ".flac"),
        key=lambda t: (t.disc, t.number),
    )
    discs = sorted({t.disc for t in tracks})
    if discs != list(range(1, len(discs) + 1)):
        raise Review("disc numbering has gaps")
    for disc in discs:
        group = [t.number for t in tracks if t.disc == disc]
        if group != list(range(1, len(group) + 1)):
            raise Review(f"disc {disc} has duplicate or missing track numbers")
    album = agreed([first(t.tags, "album") for t in tracks], normalize, "album titles")
    album_artist = agreed(
        [first(t.tags, "albumartist", "album artist") for t in tracks],
        normalize,
        "album artists",
    )
    fallback_artist, fallback_title = folder_identity(root)
    artist = album_artist or fallback_artist
    if not artist:
        artist = agreed([first(t.tags, "artist") for t in tracks], normalize, "artists")
    album = album or fallback_title
    if not artist or not album:
        raise Review("missing album title or album artist")
    existing_barcode = [first(t.tags, "barcode", "upc") for t in tracks]
    cue_barcodes = []
    cue_codes: dict[tuple[int, int], set[str]] = {}
    tocs = []
    rip_logs = {}
    eac = False
    for path in paths:
        if path.suffix.lower() == ".cue":
            cue = text_file(root / path)
            cue_barcodes += re.findall(
                r"^\s*CATALOG\s+([0-9]{8,14})\s*$", cue, re.M | re.I
            )
            disc = disc_number(path)
            valid_positions = {t.number for t in tracks if t.disc == disc}
            position = 0
            for line in cue.splitlines():
                track = re.match(r"\s*TRACK\s+(\d+)\s+AUDIO\b", line, re.I)
                if track:
                    position = int(track[1])
                code = re.match(r"\s*ISRC\s+([A-Z0-9]+)\s*$", line, re.I)
                if (
                    code
                    and position in valid_positions
                    and CUE_ISRC.fullmatch(code[1].upper())
                ):
                    cue_codes.setdefault((disc, position), set()).add(code[1].upper())
        if path.suffix.lower() != ".log":
            continue
        log = text_file(root / path)
        if "Exact Audio Copy" not in log:
            rip_logs[path.as_posix()] = "format is not recognized as Exact Audio Copy"
            continue
        eac = True
        disc = disc_number(path)
        toc = read_toc(log, [t for t in tracks if t.disc == disc])
        rip_logs[path.as_posix()] = (
            f"verified disc {disc} TOC ({toc.id})"
            if toc
            else (
                f"Exact Audio Copy log found, but the disc {disc} table of contents could not be verified against the FLACs"
            )
        )
        if toc and toc not in tocs:
            tocs.append(toc)
    if len({toc.disc for toc in tocs}) != len(tocs):
        raise Review("conflicting CD rip logs")
    tag = tracks[0].tags
    hint = first(tag, "media", "source").casefold()
    folder = root.name.casefold()
    if eac or hint == "cd" or "[cd]" in folder:
        medium = "CD"
    elif "vinyl" in hint or "[vinyl]" in folder:
        medium = "Vinyl"
    elif (
        hint in {"digital media", "web", "digital"}
        or "[web]" in folder
        or first(tag, "qobuz_album_id", "hdtracks")
    ):
        medium = "Digital Media"
    else:
        medium = ""
    catalog = agreed(
        [first(t.tags, "catalognumber", "catalog_number") for t in tracks],
        normalize,
        "catalog numbers",
    )
    if not catalog:
        bracket = re.search(r"\{([^{}]+)\}", root.name)
        if bracket and re.search(r"\d", bracket[1]):
            catalog = bracket[1]
    return Release(
        root,
        artist,
        album,
        first(tag, "date", "year"),
        agreed(existing_barcode + cue_barcodes, barcode_key, "barcodes"),
        catalog,
        medium,
        tracks,
        tocs,
        entries,
        {key: next(iter(codes)) for key, codes in cue_codes.items() if len(codes) == 1},
        rip_logs,
    )
