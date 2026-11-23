import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from unicodedata import normalize as unicode_normalize
from urllib.parse import urlsplit

from .catalog import Catalog, Edition, Entry, fields
from .release import (
    Release,
    Review,
    Track,
    barcode_key,
    first,
    folder_identity,
    normalize,
    read_release,
    track_key,
)

CREDITS = {
    "composer",
    "lyricist",
    "writer",
    "producer",
    "mixartist",
    "mixer",
    "engineer",
    "arranger",
    "conductor",
    "performer",
    "musicbrainz_workid",
}

DISC_SUFFIX = re.compile(
    r"\s*\((?:reissue|remaster(?:ed)?|deluxe(?: edition)?)\)$", re.I
)
YEAR = re.compile(r"^\d{4}(?:-\d{2}(?:-\d{2})?)?$")
EDITION_IDS = {
    "musicbrainz_albumid",
    "qobuz_album_id",
    "discogs_releaseid",
    "barcode",
    "catalognumber",
}


@dataclass
class Match:
    edition: Edition
    rank: int
    evidence: str
    delta: float


@dataclass
class TrackPlan:
    track: Track
    path: Path
    tags: dict[str, list[str]]
    sources: list[str]


@dataclass
class Proposal:
    release: Release
    folder: str
    tracks: list[TrackPlan]
    sources: list[str]
    evidence: str
    library: list[str]
    same_library_edition: list[str]
    notes: list[str] = field(default_factory=list)

    def report(self) -> dict:
        return dict(
            source=self.release.root.name,
            folder=self.folder,
            outcome="completed",
            evidence=self.evidence,
            sources=self.sources,
            library_matches=self.library,
            same_library_edition=self.same_library_edition,
            notes=self.notes,
            tracks=[
                dict(
                    old=str(item.track.path),
                    new=str(item.path),
                    sources=item.sources,
                    fields={
                        k: v
                        for k, v in item.tags.items()
                        if item.track.tags.get(k) != v
                    },
                    removed=sorted(set(item.track.tags) - set(item.tags)),
                )
                for item in self.tracks
            ],
        )


def title_matches(left: str, right: str) -> bool:
    return normalize(DISC_SUFFIX.sub("", left)) == normalize(DISC_SUFFIX.sub("", right))


def media_compatible(source: Release, candidate: Edition) -> bool:
    return bool(
        candidate.medium and (not source.medium or source.medium == candidate.medium)
    )


def supported_variant(release: Release, edition: Edition) -> str:
    variant = normalize(edition.variant)
    if variant and variant in normalize(release.root.name):
        return variant
    return ""


def catalog_matches(source: str, target: str) -> bool:
    a, b = normalize(source), normalize(target)
    # Catalog notes in braces sometimes include the label before the number.
    return bool(a and b and (a == b or (len(b) >= 5 and a.endswith(b))))


def cd_title(title: str) -> str:
    title = re.sub(r"^bonus tracks?\s*:\s*", "", title, flags=re.I)
    return re.sub(r"\s*\(non[- ]lp\s+b[- ]side\)?\s*$", "", title, flags=re.I)


def cd_title_key(title: str) -> str:
    title = re.sub(r"\(live[,;]\s*[^)]+\)\s*$", "(live)", cd_title(title), flags=re.I)
    return track_key(title)


def exact_cd_toc(release: Release, edition: Edition) -> bool:
    return (
        edition.medium == "CD"
        and len(release.tocs) == release.tracks[-1].disc
        and all((item.disc, item.id) in edition.discids for item in release.tocs)
    )


def layout_matches(
    release: Release, edition: Edition, *, exact_toc: bool = False
) -> bool:
    if len(release.tracks) != len(edition.tracks):
        return False
    variant = supported_variant(release, edition)
    typos = 0
    for source, target in zip(release.tracks, edition.tracks, strict=True):
        if (source.disc, source.number) != (target.disc, target.number):
            return False
        left, right = track_key(source.title), track_key(target.title)
        if left == right or (variant and right == left + variant):
            continue
        if not exact_toc:
            return False
        left, right = cd_title_key(source.title), cd_title_key(target.title)
        if left == right:
            continue
        if (
            typos
            or min(len(left), len(right)) < 8
            or re.findall(r"\d+", left) != re.findall(r"\d+", right)
            or left.endswith("live") != right.endswith("live")
            or SequenceMatcher(None, left, right).ratio() < 0.94
        ):
            return False
        typos += 1
    return True


def barcode_matches(release: Release, edition: Edition) -> bool:
    return bool(
        release.barcode
        and any(
            barcode_key(b) == barcode_key(release.barcode)
            for b in edition.tags.get("barcode", [])
        )
    )


def score(release: Release, edition: Edition) -> Match | None:
    if not edition.artist or not media_compatible(release, edition):
        return None
    aliases = {normalize(name) for name in {edition.artist, *edition.aliases}}
    variant = supported_variant(release, edition)
    title_matches_edition = title_matches(release.title, edition.title) or (
        variant and normalize(release.title) == normalize(edition.title) + variant
    )
    if normalize(release.artist) not in aliases or not title_matches_edition:
        return None
    toc = exact_cd_toc(release, edition)
    if not layout_matches(release, edition, exact_toc=toc):
        return None
    if (
        release.barcode
        and edition.tags.get("barcode")
        and not barcode_matches(release, edition)
    ):
        return None
    durations = [
        abs(track.seconds - entry.seconds)
        for track, entry in zip(release.tracks, edition.tracks)
        if entry.seconds is not None
    ]
    delta = max(durations, default=0)
    have_durations = len(durations) == len(release.tracks)
    isrc_votes = 0
    for track, entry in zip(release.tracks, edition.tracks):
        if track.isrc and track.isrc in entry.tags.get("isrc", []):
            isrc_votes += 1

    barcode = barcode_matches(release, edition)
    catalog = any(
        catalog_matches(release.catalog, code)
        for code in edition.tags.get("catalognumber", [])
    )
    saved_id = any(
        set(release.tags.get(k, [])) & set(edition.tags.get(k, []))
        for k in ("musicbrainz_albumid", "qobuz_album_id", "discogs_releaseid")
    )
    if toc and delta <= 3:
        return Match(
            edition,
            100 + 4 * barcode + 2 * catalog + saved_id,
            "CD table of contents and track order",
            delta,
        )
    # HDtracks source tags and catalog download links can disambiguate older
    # digital editions with year-only dates and different silence padding.
    hdtracks = first(release.tags, "hdtracks").lower()
    if (
        hdtracks in {"www.hdtracks.com", "hdtracks.com"}
        and edition.medium == release.medium == "Digital Media"
        and release.date
        and release.date[:4] == edition.date[:4]
        and have_durations
        and delta <= 5
        and any(
            urlsplit(link).hostname in {"hdtracks.com", "www.hdtracks.com"}
            for link in edition.download_links
        )
    ):
        return Match(
            edition, 55, "HDtracks provenance, year, track order and durations", delta
        )
    if delta > 4:
        return None
    if (barcode or catalog or saved_id) and (
        have_durations or isrc_votes or barcode and catalog or saved_id
    ):
        rank = 75 + 8 * barcode + 4 * catalog + 2 * saved_id
        return Match(
            edition, rank, "release identifier and matching track order", delta
        )
    if (
        isrc_votes == len(release.tracks)
        and have_durations
        and release.medium == "Digital Media"
    ):
        return Match(edition, 65, "ISRCs, durations and digital track order", delta)
    same_date = edition.date == release.date and len(release.date) == 10
    if (
        same_date
        and release.medium == edition.medium == "Digital Media"
        and have_durations
        and delta <= 1.2
    ):
        return Match(
            edition, 50, "digital release date, track order and durations", delta
        )
    return None


class Library:
    def __init__(self, directory: Path):
        self.entries = []
        self.cache: dict[Path, Release | None] = {}
        for path in sorted(directory.iterdir()):
            if path.name.startswith(".") or path.is_symlink() or not path.is_dir():
                continue
            artist, title = folder_identity(path)
            if artist and title:
                self.entries.append((artist, title, path))

    def find(self, release: Release) -> tuple[list[Path], list[Release]]:
        names = [
            path
            for artist, title, path in self.entries
            if normalize(artist) == normalize(release.artist)
            and title_matches(title, release.title)
        ]
        releases = []
        for path in names:
            if path not in self.cache:
                try:
                    self.cache[path] = read_release(path)
                except (OSError, Review):
                    self.cache[path] = None
            if self.cache[path] is not None:
                releases.append(self.cache[path])
        return names, releases


def from_library(release: Release) -> Edition:
    base = release.tags
    tags = {
        key: values[:]
        for key, values in base.items()
        if key
        in {
            "albumartists",
            "label",
            "genre",
            "barcode",
            "catalognumber",
            "releasecountry",
            "originaldate",
            "releasestatus",
            "releasetype",
            "musicbrainz_albumid",
            "musicbrainz_releasegroupid",
            "musicbrainz_albumartistid",
            "qobuz_album_id",
            "discogs_releaseid",
        }
    }
    if release.barcode and "barcode" not in tags:
        tags["barcode"] = [release.barcode]
    entries = [
        Entry(
            t.disc,
            t.number,
            t.title,
            t.seconds,
            {
                k: v[:]
                for k, v in t.tags.items()
                if k
                in CREDITS
                | {
                    "artist",
                    "artists",
                    "isrc",
                    "musicbrainz_artistid",
                    "musicbrainz_trackid",
                    "musicbrainz_releasetrackid",
                    "qobuz_track_id",
                }
            },
        )
        for t in release.tracks
    ]
    return Edition(
        release.artist,
        release.title,
        release.date,
        release.medium,
        entries,
        f"library: {release.root.name}",
        tags,
        {(toc.disc, toc.id) for toc in release.tocs},
    )


def common(records: list[dict[str, list[str]]]) -> dict[str, list[str]]:
    return {
        key: values
        for key, values in records[0].items()
        if all(set(other.get(key, [])) == set(values) for other in records[1:])
    }


def select(matches: list[Match]) -> tuple[list[Match], str]:
    if not matches:
        raise Review("no edition met the verification threshold")
    best = max(m.rank for m in matches)
    winners = [m for m in matches if m.rank == best]
    if len(winners) == 1:
        return winners, winners[0].evidence
    groups = {first(m.edition.tags, "musicbrainz_releasegroupid") for m in winners}
    recordings = [
        tuple(first(t.tags, "musicbrainz_trackid") for t in m.edition.tracks)
        for m in winners
    ]
    agreed_recordings = sum(
        bool(ids[0]) and len(set(ids)) == 1 for ids in zip(*recordings, strict=True)
    )
    if (
        best < 100
        or len(groups) != 1
        or "" in groups
        or agreed_recordings * 5 < len(recordings[0]) * 4
    ):
        raise Review(
            "multiple plausible editions; verify the pressing, catalog number, or identifiers"
        )
    return winners, "shared CD table of contents and recording agreement"


def safe_name(value: str, limit: int = 245) -> str:
    value = unicode_normalize("NFC", value).replace("/", "／")
    value = re.sub(r'[\\:*?"<>|\x00-\x1f]', "-", value).strip(" .")
    if not value:
        raise Review("empty filename after normalization")
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", value, re.I):
        value = "_" + value
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore").rstrip(" .")


def directory_name(
    artist: str, title: str, date: str, medium: str, release: Release
) -> str:
    depth, rate = max((t.bits, t.rate) for t in release.tracks)
    origin = {"Digital Media": "WEB", "CD": "CD", "Vinyl": "VINYL"}[medium]
    suffix = f" ({date[:4]}) [{origin}] [FLAC] [{depth}B-{rate / 1000:g}kHz]"
    return safe_name(f"{artist} — {title}", 245 - len(suffix.encode("utf-8"))) + suffix


def album_date(winners: list[Match], fallback: str) -> str:
    dates = [m.edition.date for m in winners]
    if dates[0] and len(set(dates)) == 1 and YEAR.fullmatch(dates[0]):
        return dates[0]
    years = {date[:4] for date in dates if date}
    if len(years) == 1:
        year = years.pop()
        if YEAR.fullmatch(year):
            return year
    if len(winners) == 1 and YEAR.fullmatch(fallback):
        return fallback
    raise Review("verified editions do not establish a release year")


def propose(release: Release, library: Library, catalog: Catalog) -> Proposal:
    names, references = library.find(release)
    locals_ = [from_library(r) for r in references]
    # Full copy of digital edition IDs requires identity with the release already
    # in the library. A shared track program alone only supports recording credits.
    same_library = [
        item
        for item in locals_
        if release.medium == item.medium
        and (
            barcode_matches(release, item)
            or any(
                first(release.tags, key)
                and first(release.tags, key) == first(item.tags, key)
                for key in EDITION_IDS - {"barcode", "catalognumber"}
            )
        )
        and score(release, item)
    ]
    mb = catalog.musicbrainz(release)
    links = [edition.lookup for edition in mb if score(release, edition)]
    discogs = catalog.discogs(release, links)
    remote = [
        (m, edition) for edition in mb + discogs if (m := score(release, edition))
    ]
    # A linked Discogs entry augments the matching MB entry, not a second vote
    # for the same release. Its credits still have to match the track layout.
    linked = {edition.lookup for m, edition in remote if edition.lookup}
    matches = [m for m, edition in remote if edition.source not in linked]
    if not matches:
        matches = [score(release, item) for item in same_library]
    try:
        winners, evidence = select(matches)
    except Review as error:
        sources = [m.edition.source for m in matches] or [
            edition.source for edition in mb + discogs
        ][:12]
        error.sources = sources
        error.library_matches = [p.name for p in names]
        raise
    editions = [m.edition for m in winners]
    preferred = editions[0]
    date = album_date(winners, release.date)
    album = preferred.title if len({m.title for m in editions}) == 1 else release.title
    if DISC_SUFFIX.search(release.title) and not DISC_SUFFIX.search(album):
        album = release.title
    variant = supported_variant(release, preferred)
    if variant and not normalize(album).endswith(variant):
        album += f" ({preferred.variant})"
    artist = next(
        (
            r.artist
            for r in references
            if normalize(r.artist) == normalize(release.artist)
        ),
        release.artist
        if normalize(release.artist) in {normalize(x) for x in preferred.aliases}
        else preferred.artist,
    )
    album_tags = common([m.tags for m in editions])
    sources = [m.source for m in editions]
    supplemental = next(
        (
            m
            for m in discogs
            if len(editions) == 1
            and m.source == preferred.lookup
            and m.medium == preferred.medium
            and score(release, m)
        ),
        None,
    )
    if supplemental:
        for key, value in supplemental.tags.items():
            if key not in {"albumartists", "barcode", "catalognumber", "genre"}:
                album_tags.setdefault(key, value)
        sources += [supplemental.source]
    notes = []
    if len(editions) > 1:
        notes.append(
            "Pressing-specific fields are included only when all matching editions agree."
        )
    shared_cd_layout = all(exact_cd_toc(release, edition) for edition in editions)
    tracks = []
    for idx, track in enumerate(release.tracks):
        records = [m.tracks[idx] for m in editions]
        verified = common([r.tags for r in records])
        title = (
            records[0].title if len({r.title for r in records}) == 1 else track.title
        )
        if shared_cd_layout:
            catalog_titles = [cd_title(r.title) for r in records]
            if len({track_key(t) for t in catalog_titles}) == 1:
                title = catalog_titles[0]
            elif len({cd_title_key(t) for t in catalog_titles}) == 1:
                title = min(catalog_titles, key=len)
            else:
                title = cd_title(title)
        track_sources = sources[:]
        if supplemental:
            for key in CREDITS:
                if key in supplemental.tracks[idx].tags:
                    verified.setdefault(key, supplemental.tracks[idx].tags[key])
        if len(verified.get("isrc", [])) > 1:
            candidate_code = track.isrc or release.cue_isrcs.get(
                (track.disc, track.number)
            )
            if candidate_code not in verified["isrc"]:
                verified.pop("isrc")
            else:
                verified["isrc"] = [candidate_code]
        # Never overwrite a source ISRC with a code from another mastering.
        if track.isrc:
            verified["isrc"] = [track.isrc]
        code = first(verified, "isrc")
        for ref in references:
            choices = [
                t
                for t in ref.tracks
                if code
                and t.isrc == code
                and track_key(t.title) == track_key(title)
                and abs(t.seconds - track.seconds) <= 2
            ]
            if len(choices) != 1:
                continue
            credits = {
                key: choices[0].tags[key]
                for key in CREDITS
                if key in choices[0].tags and key not in verified
            }
            if credits:
                verified.update(credits)
                track_sources.append(f"library: {ref.root.name} (matching ISRC)")
        entry_tags = {
            k: v
            for k, v in track.tags.items()
            if k not in {"totaltracks", "totaldiscs", "album artist"}
        }
        for key, value in (album_tags | verified).items():
            if key == "genre" and entry_tags.get(key):
                continue
            entry_tags[key] = value
        track_artist = (
            first(verified, "artist") or first(track.tags, "artist") or artist
        )
        if normalize(track_artist) == normalize(preferred.artist):
            track_artist = artist
        track_artists = (
            verified.get("artists") or track.tags.get("artists") or [track_artist]
        )
        track_artists = [
            artist if normalize(p) == normalize(preferred.artist) else p
            for p in track_artists
        ]
        album_artists = (
            album_tags.get("albumartists")
            or release.tags.get("albumartists")
            or [artist]
        )
        album_artists = [
            artist if normalize(p) == normalize(preferred.artist) else p
            for p in album_artists
        ]
        total = sum(t.disc == track.disc for t in release.tracks)
        entry_tags.update(
            fields(
                title=title,
                album=album,
                artist=track_artist,
                artists=track_artists,
                albumartist=artist,
                albumartists=album_artists,
                tracknumber=f"{track.number:02d}",
                tracktotal=f"{total:02d}",
                discnumber=f"{track.disc:02d}",
                disctotal=f"{release.tracks[-1].disc:02d}",
                date=date,
                year=date[:4],
                media=preferred.medium,
                originalyear=first(entry_tags, "originaldate")[:4],
            )
        )
        disc_id = next((toc.id for toc in release.tocs if toc.disc == track.disc), "")
        if disc_id and all((track.disc, disc_id) in m.discids for m in editions):
            entry_tags["musicbrainz_discid"] = [disc_id]
        parent = Path(f"Disc {track.disc}") if release.tracks[-1].disc > 1 else Path()
        name = f"{track.number:02d} - {safe_name(title, 230)}.flac"
        tracks.append(TrackPlan(track, parent / name, entry_tags, track_sources))
    folder = directory_name(artist, album, date, preferred.medium, release)
    return Proposal(
        release,
        folder,
        tracks,
        sources,
        evidence,
        [p.name for p in names],
        [item.source.removeprefix("library: ") for item in same_library],
        notes,
    )
