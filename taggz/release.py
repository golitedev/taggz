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
CUE_FILE = re.compile(r'^(\s*FILE\s+)"([^"]+)"\s+\S+\s*$', re.I)
CUE_TRACK = re.compile(r"^\s*TRACK\s+(\d+)\s+AUDIO\s*$", re.I)
CUE_INDEX = re.compile(r"^\s*INDEX\s+01\s+\d+:\d+:\d+\s*$", re.I)
AUDIO_REF = re.compile(r"\.(?:wav|flac|aif|aiff|ape|wv|mp3)$", re.I)
TOC_ROW = re.compile(
    r"^\s*(\d+)\s*\|[^\n|]+\|[^\n|]+\|\s*(\d+)\s*\|\s*(\d+)\s*$",
    re.M,
)
WEB_STORES = {
    "amazon",
    "amazon music",
    "apple music",
    "bandcamp",
    "beatport",
    "deezer",
    "hdtracks",
    "itunes",
    "qobuz",
    "tidal",
}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold().replace("&", " and "))
    return "".join(letter for letter in text if letter.isalnum())


def track_key(title: str) -> str:
    title = FEATURE.sub("", title)
    title = re.sub(
        r"\s*[\[(](?:album version|bonus track)[)\]]\s*$", "", title, flags=re.I
    )
    return normalize(title)


def barcode_key(value: str) -> str:
    return re.sub(r"\D", "", value).lstrip("0")


def first(tags: dict[str, list[str]], *keys: str) -> str:
    return next((v.strip() for key in keys for v in tags.get(key, []) if v.strip()), "")


def text_file(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError as error:
            raise Review(f"invalid UTF-16 text in {path.name}") from error
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
    channels: int = 2

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
    cue_notes: dict[str, str] = field(default_factory=dict)

    @property
    def tags(self) -> dict[str, list[str]]:
        return self.tracks[0].tags

    def isrc_for(self, track: Track) -> str:
        return track.isrc or self.cue_isrcs.get((track.disc, track.number), "")


@dataclass
class CueTrack:
    file_line: int
    track: Track
    isrc: str = ""


def number(value: str, label: str) -> int:
    try:
        n = int(value.split("/", 1)[0])
    except ValueError as error:
        raise Review(f"invalid {label}: {value!r}") from error
    if not 1 <= n <= 999:
        raise Review(f"invalid {label}: {value!r}")
    return n


def parent_disc(path: Path) -> int | None:
    for part in reversed(path.parent.parts):
        match = DISC.fullmatch(part)
        if match:
            return int(match[1])
    return None


def disc_number(path: Path) -> int:
    return parent_disc(path) or 1


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
        channels=audio.info.channels,
    )


def audio_reference(value: str, sidecar: Path, tracks: list[Track]) -> Track:
    src = Path(value.replace("\\", "/"))
    path = Path(os.path.normpath(sidecar.parent / src))
    exact = [
        t for t in tracks if t.path == path or t.path.with_suffix(src.suffix) == path
    ]
    if len(exact) == 1:
        return exact[0]
    matches = [
        t
        for t in tracks
        if t.path.parent == path.parent
        and normalize(t.path.stem) == normalize(path.stem)
    ]
    if len(matches) == 1:
        return matches[0]
    # Rippers can record absolute and Windows paths. Only staged tracks are resolved.
    matches = [t for t in tracks if normalize(t.path.stem) == normalize(src.stem)]
    if len(matches) == 1:
        return matches[0]
    prefix = re.match(r"^(\d+)(?:[ ._-]|$)", src.stem)
    if prefix and AUDIO_REF.search(value):
        matches = [
            t
            for t in tracks
            if t.disc == disc_number(sidecar) and t.number == int(prefix[1])
        ]
        if len(matches) == 1:
            return matches[0]
    raise Review(f"cannot resolve {value!r} in {sidecar}")


def cue_tracks(lines: list[str], path: Path, tracks: list[Track]) -> list[CueTrack]:
    result = []
    files = set()
    current = None
    position = None
    indexed = False
    code = ""
    for index, line in enumerate(lines):
        if re.match(r"^\s*FILE\b", line, re.I):
            match = CUE_FILE.fullmatch(line)
            if not match:
                raise Review(f"unsupported cue FILE line in {path}")
            track = audio_reference(match[2], path, tracks)
            if track.path in files:
                raise Review(f"duplicate cue file reference in {path}")
            files.add(track.path)
            current = CueTrack(index, track)
        elif match := CUE_TRACK.fullmatch(line):
            if position is not None and not indexed:
                raise Review(f"cue track {position:02d} has no INDEX 01 in {path}")
            position, indexed, code = int(match[1]), False, ""
        elif CUE_INDEX.fullmatch(line):
            # EAC can place FILE after TRACK and INDEX 00. INDEX 01
            # identifies the audio file where this track actually begins.
            if current is None or position is None or indexed:
                raise Review(
                    f"cue refers to an image or contains unmapped tracks: {path}"
                )
            if current.track.number != position:
                raise Review(
                    f"cue track {position:02d} points to {current.track.path} in {path}"
                )
            current.isrc = code
            result.append(current)
            current, indexed = None, True
        elif position is not None:
            match = re.fullmatch(r"\s*ISRC\s+([A-Z0-9]+)\s*", line, re.I)
            if match and CUE_ISRC.fullmatch(match[1].upper()):
                code = match[1].upper()
                if indexed:
                    result[-1].isrc = code
    if not indexed or len(result) != len(files):
        raise Review(f"cue contains unmapped tracks: {path}")
    return result


def read_toc(log: str, tracks: list[Track]) -> Toc | None:
    """Convert a verified CD rip track table to the MusicBrainz disc ID."""
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


def artist_credit(tags: dict[str, list[str]], *keys: str) -> str:
    for key in keys:
        artists = [item.strip() for item in tags.get(key, []) if item.strip()]
        if artists:
            return ", ".join(artists)
    return ""


def folder_medium(name: str) -> str:
    qualifiers = [
        (bracket or parenthesis).strip()
        for bracket, parenthesis in re.findall(
            r"\[([^\]]+)\]|\(([^()]*)\)", name.casefold()
        )
    ]
    tail = re.search(
        r"(?:\s[-—–]\s(web|vinyl|cd)(?:[- ]+flac)?|\s(web|vinyl|cd)[- ]+flac)\s*$",
        name,
        re.I,
    )
    if tail:
        qualifiers.append((tail[1] or tail[2]).lower())
    kinds = {
        medium
        for medium, pattern in (
            ("CD", r"^(?:flac[- ]+cd|cd(?:[- ]?(?:flac|rip))?)\b"),
            ("Vinyl", r"^(?:vinyl|lp[- ]?rip)\b"),
            ("Digital Media", r"^(?:web|digital media)\b"),
        )
        if any(re.search(pattern, item) for item in qualifiers)
    }
    return next(iter(kinds)) if len(kinds) == 1 else ""


def valid_gtin(text: str) -> bool:
    return (
        len(text) in {8, 12, 13, 14}
        and text.isascii()
        and text.isdigit()
        and any(ch != "0" for ch in text)
        and sum(
            int(ch) * (1 if index % 2 == 0 else 3)
            for index, ch in enumerate(reversed(text))
        )
        % 10
        == 0
    )


def folder_identifiers(name: str) -> tuple[str, str]:
    blocks = re.findall(r"\{([^{}]+)\}", name)
    # Bracketed label and catalog hints are common on LP rips. Skip technical
    # format groups, including years and audio resolution suffixes.
    for block in re.findall(r"\[([^]]+)\]", name):
        if re.search(r"\b(?:flac|web|vinyl|cd|khz|bit)\b", block, re.I):
            continue
        if valid_gtin(block) or re.search(
            r"(?:,\s*\d{8,14}|[A-Z]{2,}[- ]\d{3,})$", block, re.I
        ):
            blocks.append(block)
    if not blocks:
        return "", ""
    ordered = [block.strip() for block in blocks if re.search(r"\d", block)]
    if not ordered:
        return "", ""
    hint = ordered[0]
    code = re.search(r"(?<!\d)\d{8,14}$", hint)
    return hint, code[0] if code and valid_gtin(code[0]) else ""


def source_medium(name: str, tags: dict[str, list[str]], cd_log: bool) -> str:
    media = first(tags, "media", "source").casefold()
    source = first(tags, "source").casefold()
    if cd_log or media in {"cd", "compact disc"}:
        return "CD"
    hint = folder_medium(name)
    if hint in {"CD", "Vinyl"} or "vinyl" in media or "vinyl" in source:
        return hint if hint in {"CD", "Vinyl"} else "Vinyl"
    if (
        hint == "Digital Media"
        or media in {"web", "digital", "digital media"}
        or (source in WEB_STORES or first(tags, "qobuz_album_id", "hdtracks"))
    ):
        return "Digital Media"
    return ""


def ripper_name(log: str) -> str:
    if "Exact Audio Copy" in log:
        return "Exact Audio Copy"
    if "X Lossless Decoder" in log or "XLD extraction logfile" in log:
        return "XLD"
    return ""


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
        [
            artist_credit(t.tags, "albumartist", "album artist", "albumartists")
            for t in tracks
        ],
        normalize,
        "album artists",
    )
    fallback_artist, fallback_title = folder_identity(root)
    artist = album_artist or fallback_artist
    if not artist:
        artist = agreed(
            [artist_credit(t.tags, "artist", "artists") for t in tracks],
            normalize,
            "artists",
        )
    album = album or fallback_title
    if not artist or not album:
        raise Review("missing album title or album artist")
    existing_barcode = [
        next(
            (
                code
                for key in ("barcode", "upc", "mcn")
                if barcode_key(code := first(t.tags, key))
            ),
            "",
        )
        for t in tracks
    ]
    cue_barcodes = []
    cue_codes: dict[tuple[int, int], set[str]] = {}
    cue_notes = {}
    tocs = []
    rip_logs = {}
    cd_log = False
    for path in paths:
        if path.suffix.lower() == ".cue":
            try:
                cue = text_file(root / path)
                mapped = cue_tracks(cue.splitlines(), path, tracks)
            except Review as error:
                cue_notes[path.as_posix()] = str(error)
                continue
            cue_barcodes += [
                code
                for code in re.findall(
                    r"^\s*CATALOG\s+([0-9]{8,14})\s*$", cue, re.M | re.I
                )
                if valid_gtin(code)
            ]
            for entry in mapped:
                if entry.isrc:
                    cue_codes.setdefault(
                        (entry.track.disc, entry.track.number), set()
                    ).add(entry.isrc)
        if path.suffix.lower() != ".log":
            continue
        try:
            log = text_file(root / path)
        except Review as error:
            rip_logs[path.as_posix()] = str(error)
            continue
        ripper = ripper_name(log)
        if not ripper:
            rip_logs[path.as_posix()] = "format is not recognized as a CD rip log"
            continue
        cd_log = True
        disc = parent_disc(path)
        suffix = re.search(
            r"(?:^|[\s._(\[-])(?:cd|disc|disk)\s*0*(\d+)[)\]]?$", path.stem, re.I
        )
        if disc is None and suffix:
            disc = int(suffix[1])
        options = [disc] if disc is not None else discs
        matching = [
            toc
            for n in options
            if (toc := read_toc(log, [t for t in tracks if t.disc == n]))
        ]
        toc = matching[0] if len(matching) == 1 else None
        rip_logs[path.as_posix()] = (
            f"verified disc {toc.disc} TOC ({toc.id})"
            if toc
            else (
                f"{ripper} log found, but its table of contents could not be assigned to one FLAC disc"
            )
        )
        if toc and toc not in tocs:
            tocs.append(toc)
    if len({toc.disc for toc in tocs}) != len(tocs):
        raise Review("conflicting CD rip logs")
    tag = tracks[0].tags
    medium = source_medium(root.name, tag, cd_log)
    catalog = agreed(
        [first(t.tags, "catalognumber", "catalog_number") for t in tracks],
        normalize,
        "catalog numbers",
    )
    catalog_hint, barcode_hint = folder_identifiers(root.name)
    return Release(
        root,
        artist,
        album,
        first(tag, "date", "year"),
        agreed(existing_barcode + cue_barcodes, barcode_key, "barcodes")
        or barcode_hint,
        catalog or catalog_hint,
        medium,
        tracks,
        tocs,
        entries,
        {key: next(iter(codes)) for key, codes in cue_codes.items() if len(codes) == 1},
        rip_logs,
        cue_notes,
    )
