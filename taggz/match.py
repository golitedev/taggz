import re
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from pathlib import Path
from unicodedata import normalize as unicode_normalize
from urllib.parse import urlsplit

from .catalog import Catalog, Edition, Entry, fields
from .release import (
    Release,
    Review,
    Track,
    artist_credit,
    barcode_key,
    first,
    normalize,
    track_key,
)

CREDITS = {
    "composer",
    "lyricist",
    "writer",
    "producer",
    "mixartist",
    "mixer",
    "mastering",
    "engineer",
    "arranger",
    "conductor",
    "director",
    "remixer",
    "performer",
    "publisher",
    "musicbrainz_workid",
}
RECORDING_FIELDS = CREDITS | {"musicbrainz_artistid", "musicbrainz_trackid", "work"}

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
    program_only: bool = False


@dataclass
class TrackPlan:
    track: Track
    path: Path
    tags: dict[str, list[str]]
    sources: list[str]
    withheld: list[str] = field(default_factory=list)


@dataclass
class Proposal:
    release: Release
    folder: str
    tracks: list[TrackPlan]
    sources: list[str]
    evidence: str
    notes: list[str] = field(default_factory=list)

    def report(self) -> dict:
        return dict(
            source=self.release.root.name,
            folder=self.folder,
            outcome="completed",
            evidence=self.evidence,
            sources=self.sources,
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
                    previous_fields={
                        k: v
                        for k, v in item.track.tags.items()
                        if item.tags.get(k) != v
                    },
                    withheld=item.withheld,
                )
                for item in self.tracks
            ],
        )


def title_matches(left: str, right: str) -> bool:
    a, b = (normalize(DISC_SUFFIX.sub("", title)) for title in (left, right))
    if a == b:
        return True
    for roman, numeral in (("ii", "2"), ("iii", "3"), ("iv", "4"), ("volv", "vol5")):
        if a.endswith(roman) and b == a.removesuffix(roman) + numeral:
            return True
        if b.endswith(roman) and a == b.removesuffix(roman) + numeral:
            return True
    return False


def artist_key(name: str) -> str:
    return normalize(
        re.sub(
            r"\s+(?:y|feat(?:uring)?\.?|ft\.?)\s+|\s*[,/]\s*", " and ", name, flags=re.I
        )
    )


def media_compatible(source: Release, candidate: Edition) -> bool:
    return bool(
        candidate.medium in {"CD", "Digital Media", "Vinyl"}
        and (not source.medium or source.medium == candidate.medium)
    )


def supported_variant(release: Release, edition: Edition) -> str:
    variant = normalize(edition.variant)
    qualifiers = re.findall(r"\(([^()]+)\)|\[([^\[\]]+)\]", release.root.name)
    if variant and any(
        normalize(left or right) == variant for left, right in qualifiers
    ):
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
    title = re.sub(
        r"\s*[\[(](?:live|en vivo)(?:[,;]\s*[^)\]]+)?[)\]]\s*$",
        "(live)",
        cd_title(title),
        flags=re.I,
    )
    return track_key(title)


def anchored_title_key(title: str) -> str:
    title = re.sub(
        r"\s*[\[(](?:latin pop|album version|bonus track)[)\]]", "", title, flags=re.I
    )
    # A named style before the same remix marker is a subtitle, not a reason
    # to conflate a remix with the unqualified recording.
    title = re.sub(
        r"\s*[\[(]versi[oó]n [^)\]]+[)\]](?=\s*[\[(]remix[)\]])",
        "",
        title,
        flags=re.I,
    )
    return cd_title_key(title)


def product_title_key(title: str) -> str:
    title = re.sub(
        r"\s*[\[(](?:(?:\d{4} )?remaster(?:ed)?(?: \d{4})?|"
        r"\d+(?:\.\d+)?\s*kHz\s*/\s*\d+\s*bit)[)\]]\s*$",
        "",
        title,
        flags=re.I,
    )
    return anchored_title_key(title)


def position_issue(release: Release, edition: Edition) -> str | None:
    if len(release.tracks) != len(edition.tracks):
        return f"track count differs: staging has {len(release.tracks)}, candidate has {len(edition.tracks)}"
    for source, target in zip(release.tracks, edition.tracks, strict=True):
        if (source.disc, source.number) != (target.disc, target.number):
            return (
                f"track position differs: staging disc {source.disc} track {source.number:02d}, "
                f"candidate disc {target.disc} track {target.number:02d}"
            )
    return None


def recording_title_matches(title: str, entry: Entry) -> bool:
    if track_key(title) == track_key(entry.title):
        return True
    work = first(entry.tags, "work")
    return bool(
        work
        and entry.title.startswith(f"{work}: ")
        and track_key(title) == track_key(entry.title.removeprefix(f"{work}: "))
    )


def exact_cd_toc(release: Release, edition: Edition) -> bool:
    return (
        edition.medium == "CD"
        and len(release.tocs) == release.tracks[-1].disc
        and all((item.disc, item.id) in edition.discids for item in release.tocs)
    )


def layout_issue(
    release: Release,
    edition: Edition,
    *,
    anchored: bool = False,
    physical_identity: bool = False,
    product_identity: bool = False,
) -> str | None:
    if issue := position_issue(release, edition):
        return issue
    variant = supported_variant(release, edition)
    typos = 0
    for source, target in zip(release.tracks, edition.tracks, strict=True):
        left, right = track_key(source.title), track_key(target.title)
        if recording_title_matches(source.title, target) or (
            variant and right == left + variant
        ):
            continue
        mismatch = f"disc {source.disc} track {source.number:02d} title differs: {source.title!r} vs {target.title!r}"
        if not anchored:
            return mismatch
        if product_identity and product_title_key(source.title) == product_title_key(
            target.title
        ):
            continue
        left, right = anchored_title_key(source.title), anchored_title_key(target.title)
        if left == right:
            continue
        if (
            physical_identity
            and target.seconds is not None
            and abs(source.seconds - target.seconds) <= 1.5
        ):
            # Exact pressing identifiers can corroborate a translation or a
            # single shortened title when the audio positions and lengths agree.
            a, b = sorted(
                (source.title.casefold().strip(), target.title.casefold().strip()),
                key=len,
            )
            suffix = b[len(a) :].strip() if b.startswith(a) else ""
            if (
                suffix
                and not re.search(
                    r"\b(?:live|en vivo|remix|mix|version|versión|alternate|edit|dub|"
                    r"instrumental|acoustic|demo|remaster\w*|mono|stereo|karaoke)\b",
                    suffix,
                )
                and (
                    re.fullmatch(r"\([^()]+\)", suffix)
                    or (not typos and suffix.isalpha())
                )
            ):
                typos += 1
                continue
        if (
            typos
            or min(len(left), len(right)) < 8
            or re.findall(r"\d+", left) != re.findall(r"\d+", right)
            or left.endswith("live") != right.endswith("live")
            or SequenceMatcher(None, left, right).ratio() < 0.92
        ):
            return mismatch
        typos += 1
    return None


def barcode_matches(release: Release, edition: Edition) -> bool:
    return bool(
        release.barcode
        and any(
            barcode_key(b) == barcode_key(release.barcode)
            for b in edition.tags.get("barcode", [])
        )
    )


def identifiers(release: Release, edition: Edition) -> tuple[bool, bool, bool]:
    return (
        barcode_matches(release, edition),
        any(
            catalog_matches(release.catalog, code)
            for code in edition.tags.get("catalognumber", [])
        ),
        any(
            set(release.tags.get(key, [])) & set(edition.tags.get(key, []))
            for key in EDITION_IDS - {"barcode", "catalognumber"}
        ),
    )


def album_identity_matches(
    release: Release, edition: Edition, *, anchored: bool = False
) -> bool:
    aliases = {artist_key(name) for name in {edition.artist, *edition.aliases}}
    if anchored:
        aliases |= {artist_key(part) for part in edition.artist.split(" / ")}
        if edition.tags.get("qobuz_album_id"):
            aliases |= {
                artist_key(name) for name in edition.tags.get("albumartists", [])
            }
    return artist_key(release.artist) in aliases


def edition_title_matches(
    release: Release, edition: Edition, *, anchored: bool = False
) -> bool:
    if title_matches(release.title, edition.title):
        return True
    variant = (
        normalize(edition.variant) if anchored else supported_variant(release, edition)
    )
    a, b = normalize(release.title), normalize(edition.title)
    return bool(variant and (a == b + variant or b == a + variant))


def same_web_product(left: Edition, right: Edition) -> bool:
    if left.medium != "Digital Media" or right.medium != "Digital Media":
        return False
    codes = {barcode_key(value) for value in left.tags.get("barcode", [])} - {""}
    if not codes.intersection(
        barcode_key(value) for value in right.tags.get("barcode", [])
    ):
        return False
    return len(left.tracks) == len(right.tracks) and all(
        (a.disc, a.number) == (b.disc, b.number)
        and (
            recording_title_matches(a.title, b)
            or recording_title_matches(b.title, a)
            or product_title_key(a.title) == product_title_key(b.title)
        )
        and (a.seconds is None or b.seconds is None or abs(a.seconds - b.seconds) <= 4)
        for a, b in zip(left.tracks, right.tracks)
    )


def score(
    release: Release, edition: Edition, *, rejection: list[str] | None = None
) -> Match | None:
    def reject(reason: str) -> None:
        if rejection is not None:
            rejection.append(reason)

    if not edition.artist:
        return reject("candidate has no album artist")
    if not media_compatible(release, edition):
        return reject(
            f"medium does not match: staging is {release.medium or 'unconfirmed'}, "
            f"candidate is {edition.medium or 'unconfirmed'}"
        )
    toc = exact_cd_toc(release, edition)
    barcode, catalog, saved_id = identifiers(release, edition)
    anchored = toc or barcode or catalog or saved_id
    variant = supported_variant(release, edition)
    if not album_identity_matches(release, edition, anchored=anchored):
        return reject(f"album artist differs: {release.artist!r} vs {edition.artist!r}")
    if not edition_title_matches(release, edition, anchored=anchored):
        return reject(f"release title differs: {release.title!r} vs {edition.title!r}")
    if issue := position_issue(release, edition):
        return reject(issue)
    if (
        release.barcode
        and edition.tags.get("barcode")
        and not barcode_matches(release, edition)
    ):
        return reject(
            f"barcode differs: {release.barcode} vs {', '.join(edition.tags['barcode'])}"
        )
    physical_identity = (
        toc
        or (barcode and catalog and edition.medium == "CD")
        or (catalog and edition.medium == "Vinyl")
    )
    if issue := layout_issue(
        release,
        edition,
        anchored=anchored,
        physical_identity=physical_identity,
        product_identity=barcode or saved_id,
    ):
        return reject(issue)
    durations = [
        (abs(track.seconds - entry.seconds), track.disc, track.number)
        for track, entry in zip(release.tracks, edition.tracks)
        if entry.seconds is not None
    ]
    delta, max_disc, max_track = max(durations, default=(0, 0, 0))
    have_durations = len(durations) == len(release.tracks)
    isrc_votes = 0
    for track, entry in zip(release.tracks, edition.tracks):
        if release.isrc_for(track) in entry.tags.get("isrc", []):
            isrc_votes += 1

    if toc and delta <= 3:
        return Match(
            edition,
            100 + 4 * barcode + 2 * catalog + saved_id,
            "CD table of contents and track order",
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
            edition, 55, "HDtracks provenance, year, track order and durations"
        )
    if delta > 4:
        return reject(
            f"disc {max_disc} track {max_track:02d} duration differs by {delta:.2f}s "
            "(more than the allowed 4s)"
        )
    is_qobuz = bool(edition.tags.get("qobuz_album_id"))
    if is_qobuz:
        if (barcode or saved_id) and (have_durations or isrc_votes):
            rank = 85 + 5 * barcode + 12 * saved_id
            return Match(
                edition, rank, "Qobuz release ID or barcode, track order and durations"
            )
        if (
            release.medium == "Digital Media"
            and not release.barcode
            and not first(release.tags, "qobuz_album_id")
            and release.date == edition.date
            and len(release.date) == 10
            and have_durations
            and delta <= 1.2
            and isrc_votes == len(release.tracks)
        ):
            return Match(
                edition,
                66,
                "Qobuz release date and matching ISRCs, track order and durations",
            )
        return reject(
            "Qobuz edition not verified by an identifier or exact date and complete ISRC match"
        )
    if (barcode or catalog or saved_id) and (
        have_durations or isrc_votes or barcode and catalog or saved_id
    ):
        rank = 75 + 8 * barcode + 4 * catalog + 2 * saved_id
        return Match(edition, rank, "release identifier and matching track order")
    if (
        isrc_votes == len(release.tracks)
        and have_durations
        and release.medium == "Digital Media"
        and (not release.date or not edition.date or release.date == edition.date)
    ):
        return Match(edition, 65, "ISRCs, durations and digital track order")
    if (
        variant
        and edition.medium == "Digital Media"
        and release.date
        and release.date[:4] == edition.date[:4]
        and have_durations
        and delta <= 1.2
    ):
        return Match(
            edition,
            54,
            "named digital version, release year, track order and durations",
        )
    same_date = edition.date == release.date and len(release.date) == 10
    if (
        same_date
        and release.medium == edition.medium == "Digital Media"
        and have_durations
        and delta <= 1.2
    ):
        return Match(edition, 50, "digital release date, track order and durations")
    hints = []
    if release.tocs and not toc:
        hints.append("CD disc ID did not match")
    elif toc and delta > 3:
        hints.append(
            f"disc {max_disc} track {max_track:02d} is {delta:.2f}s apart "
            "(3s allowed for an exact CD TOC match)"
        )
    if release.barcode and not barcode:
        hints.append("candidate did not confirm the barcode")
    if release.catalog and not catalog:
        hints.append("catalog number did not match")
    if not have_durations:
        hints.append(
            f"durations available for {len(durations)}/{len(release.tracks)} tracks"
        )
    if release.medium == "Digital Media":
        hints.append(f"{isrc_votes}/{len(release.tracks)} ISRCs matched")
        if not same_date:
            hints.append("exact release date could not be confirmed")
        elif delta > 1.2:
            hints.append(
                f"largest duration difference was {delta:.2f}s (1.2s allowed for a date match)"
            )
    return reject(
        "track list matches, but release identity could not be verified"
        + (f" ({'; '.join(hints)})" if hints else "")
    )


def program_match(release: Release, edition: Edition) -> Match | None:
    if (
        not album_identity_matches(release, edition, anchored=True)
        or not edition_title_matches(release, edition)
        or position_issue(release, edition)
    ):
        return None
    pairs = list(zip(release.tracks, edition.tracks, strict=True))
    shared_codes = [release.isrc_for(t) in e.tags.get("isrc", []) for t, e in pairs]
    delta = max(
        (abs(t.seconds - e.seconds) for t, e in pairs if e.seconds is not None),
        default=0,
    )
    complete_lengths = all(e.seconds is not None for _, e in pairs)
    medium = release.medium
    if medium not in {"CD", "Digital Media", "Vinyl"}:
        return None
    complete_cd_tables = medium == "CD" and {toc.disc for toc in release.tocs} == {
        t.disc for t in release.tracks
    }
    if medium != edition.medium and not (complete_cd_tables and len(pairs) >= 3):
        return None
    if (
        complete_cd_tables
        and layout_issue(release, edition, anchored=True) is None
        and len(pairs) >= 3
        and complete_lengths
        and delta <= 4
    ):
        rank, evidence = (
            40,
            "CD rip tables, matching titles, track positions and durations",
        )
    elif (
        medium == edition.medium == "Digital Media"
        and (
            all(shared_codes)
            or (len(pairs) >= 3 and sum(shared_codes) * 5 >= len(pairs) * 4)
        )
        and layout_issue(release, edition, anchored=True) is None
        and complete_lengths
        and delta <= 1.2
    ):
        rank, evidence = (
            39,
            "matching digital track positions, durations and most ISRCs",
        )
    else:
        return None
    if release.medium and release.medium != edition.medium and not release.date:
        return None
    safe_fields = {
        "artist",
        "artists",
        "musicbrainz_artistid",
        "musicbrainz_trackid",
        "work",
    } | CREDITS
    tracks = [
        replace(
            e,
            tags={key: val for key, val in e.tags.items() if key in safe_fields}
            if shared_code
            else {},
        )
        for (_, e), shared_code in zip(pairs, shared_codes, strict=True)
    ]
    return Match(
        replace(
            edition,
            medium=medium,
            date=release.date or edition.date,
            tracks=tracks,
            tags={
                key: val
                for key, val in edition.tags.items()
                if key
                in {
                    "albumartists",
                    "musicbrainz_albumartistid",
                    "musicbrainz_releasegroupid",
                    "originaldate",
                }
            },
            discids=set(),
            lookup="",
        ),
        rank,
        evidence + "; exact edition not established",
        program_only=True,
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
        if best < 100:
            detail = "available evidence does not distinguish them"
        elif len(groups) != 1 or "" in groups:
            detail = "their release groups disagree or are missing"
        else:
            detail = (
                f"only {agreed_recordings}/{len(recordings[0])} recording IDs agree "
                "across the CD pressings"
            )
        raise Review(f"{len(winners)} equally supported editions; {detail}")
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


def propose(release: Release, catalog: Catalog, diagnostics: dict) -> Proposal:
    candidates = diagnostics["candidates"] = []

    def assess(edition: Edition) -> Match | None:
        rejection = []
        match = score(release, edition, rejection=rejection)
        aligned = (
            list(zip(release.tracks, edition.tracks, strict=True))
            if (position_issue(release, edition) is None)
            else []
        )
        deltas = [
            abs(t.seconds - e.seconds) for t, e in aligned if e.seconds is not None
        ]
        candidates.append(
            {
                "source": edition.source,
                "artist": edition.artist,
                "title": edition.title,
                "date": edition.date,
                "medium": edition.medium,
                "track_count": len(edition.tracks),
                "barcodes": edition.tags.get("barcode", []),
                "catalog_numbers": edition.tags.get("catalognumber", []),
                "exact_cd_toc": exact_cd_toc(release, edition),
                "decision": "qualified" if match else "rejected",
                "reason": match.evidence if match else rejection[0],
                "compared_durations": len(deltas),
                "maximum_duration_difference_seconds": round(max(deltas), 3)
                if deltas
                else None,
                "matching_isrcs": sum(
                    bool(release.isrc_for(t) in e.tags.get("isrc", []))
                    for t, e in aligned
                ),
            }
        )
        return match

    qobuz = catalog.qobuz(release)
    mb = catalog.musicbrainz(release)
    links = [
        edition.lookup for edition in mb if title_matches(edition.title, release.title)
    ]
    discogs = catalog.discogs(release, links)
    pool = qobuz + mb + discogs
    qobuz_matches = [match for edition in qobuz if (match := assess(edition))]
    mb_matches = [match for edition in mb if (match := assess(edition))]
    discogs_matches = [match for edition in discogs if (match := assess(edition))]
    linked = {match.edition.lookup for match in mb_matches} - {""}
    matches = qobuz_matches + [
        match
        for match in mb_matches + discogs_matches
        if match.edition.source not in linked
        and not any(same_web_product(match.edition, q.edition) for q in qobuz_matches)
    ]
    diagnostics["sources"] = [m.edition.source for m in matches] or [
        item.source for item in pool
    ]
    try:
        winners, evidence = select(matches)
    except Review:
        best = max((m.rank for m in matches), default=None)
        tied = {m.edition.source for m in matches if m.rank == best}
        for item in candidates:
            if item["source"] in tied:
                item["decision"] = "ambiguous"
        programs = [m for edition in pool if (m := program_match(release, edition))]
        if not programs:
            raise
        rank = max(m.rank for m in programs)
        winners = [m for m in programs if m.rank == rank]
        if len({(m.edition.medium, m.edition.date) for m in winners}) > 1:
            raise
        evidence = winners[0].evidence
    program_only = winners[0].program_only
    diagnostics["match_scope"] = (
        "track program"
        if program_only
        else "shared pressings"
        if len(winners) > 1
        else "release edition"
    )
    editions = [m.edition for m in winners]
    preferred = editions[0]
    date = album_date(winners, release.date)
    album = preferred.title if len({m.title for m in editions}) == 1 else release.title
    if DISC_SUFFIX.search(release.title) and not DISC_SUFFIX.search(album):
        album = release.title
    variant = supported_variant(release, preferred)
    if variant and not normalize(album).endswith(variant):
        album += f" ({preferred.variant})"
    artist = (
        release.artist
        if normalize(release.artist) in {normalize(x) for x in preferred.aliases}
        else preferred.artist
    )
    album_tags = common([m.tags for m in editions])
    sources = [m.source for m in editions]
    linked_discogs = [
        edition
        for edition in discogs
        if len(editions) == 1
        and edition.source == preferred.lookup
        and edition.medium == preferred.medium
        and album_identity_matches(release, edition, anchored=True)
        and title_matches(release.title, edition.title)
        and layout_issue(release, edition, anchored=True) is None
        and all(
            e.seconds is None or abs(t.seconds - e.seconds) <= 4
            for t, e in zip(release.tracks, edition.tracks, strict=True)
        )
        and not (
            release.barcode
            and edition.tags.get("barcode")
            and not barcode_matches(release, edition)
        )
    ]
    supplemental = linked_discogs
    if len(editions) == 1 and not program_only and preferred.medium == "Digital Media":
        alternatives = (
            mb + discogs
            if preferred.tags.get("qobuz_album_id")
            else (qobuz if any(identifiers(release, preferred)) else [])
        )
        supplemental += [
            edition for edition in alternatives if same_web_product(preferred, edition)
        ]
    supplemental_sources = {edition.source for edition in supplemental}
    supplemental_groups = {}
    if supplemental:
        for edition in supplemental:
            authority = next(
                (
                    key
                    for key in (
                        "musicbrainz_albumid",
                        "qobuz_album_id",
                        "discogs_releaseid",
                    )
                    if key in edition.tags
                ),
                edition.source,
            )
            supplemental_groups.setdefault(authority, []).append(edition)
        for group in supplemental_groups.values():
            for key, value in common([edition.tags for edition in group]).items():
                album_tags.setdefault(key, value)
        sources.extend(dict.fromkeys(edition.source for edition in supplemental))
    diagnostics["sources"] = sources[:]
    for item in candidates:
        if item["source"] in sources:
            item["decision"] = (
                "supplemental"
                if item["source"] in supplemental_sources
                else "selected for track program"
                if program_only
                else "selected"
            )
            if program_only:
                item["edition_reason"] = item["reason"]
                item["reason"] = evidence
            elif item["decision"] == "supplemental":
                item["reason"] = (
                    "same edition confirmed by a catalog link or shared barcode and track list"
                )
    diagnostics["withheld_release_fields"] = sorted(
        (EDITION_IDS | {"releasecountry"})
        & (
            set().union(*(m.tags for m in pool if m.source in sources))
            - set(album_tags)
            - set(release.tags)
        )
    )
    notes = []
    if program_only:
        notes.append(
            "The verified track program was used for naming and shared credits. "
            "Edition-specific fields from the reference were withheld."
        )
    elif len(editions) > 1:
        notes.append(
            "Pressing-specific fields are included only when all matching editions agree."
        )
    shared_cd_layout = all(exact_cd_toc(release, edition) for edition in editions)
    recording_references = [
        edition
        for edition in qobuz + mb
        if (edition.source not in sources or program_only)
        and edition_title_matches(release, edition, anchored=True)
        and album_identity_matches(release, edition, anchored=True)
        and position_issue(release, edition) is None
    ]
    tracks = []
    for idx, track in enumerate(release.tracks):
        records = [m.tracks[idx] for m in editions]
        verified = common([r.tags for r in records])
        withheld = sorted(
            {"musicbrainz_trackid", "musicbrainz_releasetrackid", "isrc"}
            & (set().union(*(r.tags for r in records)) - set(verified))
        )
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
        source_isrc = release.isrc_for(track)
        preferred_codes = (
            {source_isrc} if source_isrc else set(verified.get("isrc", []))
        )
        for group in supplemental_groups.values():
            additional = [
                edition.tracks[idx].tags
                for edition in group
                if not preferred_codes
                or not edition.tracks[idx].tags.get("isrc")
                or preferred_codes.intersection(edition.tracks[idx].tags["isrc"])
            ]
            if additional:
                for key, value in common(additional).items():
                    verified.setdefault(key, value)
        if len(preferred_codes) == 1:
            refs = [
                edition
                for edition in recording_references
                if (entry := edition.tracks[idx]).seconds is not None
                and abs(track.seconds - entry.seconds) <= 3
                and recording_title_matches(title, entry)
                and preferred_codes.intersection(entry.tags.get("isrc", []))
            ]
            # Each provider can supply additional credits for the same ISRC.
            # Candidate editions from one provider must agree on the values.
            for group in (
                [e for e in refs if e.tags.get("qobuz_album_id")],
                [e for e in refs if e.tags.get("musicbrainz_albumid")],
            ):
                if group:
                    credits = {
                        key: value
                        for key, value in common(
                            [e.tracks[idx].tags for e in group]
                        ).items()
                        if key in RECORDING_FIELDS and key not in verified
                    }
                    if credits:
                        verified.update(credits)
                        track_sources.extend(
                            f"{e.source} (matching ISRC)" for e in group
                        )
        if source_isrc:
            verified["isrc"] = [source_isrc]
        elif len(verified.get("isrc", [])) > 1:
            verified.pop("isrc")
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
            artist_credit(verified, "artist", "artists")
            or artist_credit(track.tags, "artist", "artists")
            or artist
        )
        if normalize(track_artist) == normalize(preferred.artist):
            track_artist = artist
        track_artists = (
            verified.get("artists")
            or track.tags.get("artists")
            or (track.tags.get("artist") if not verified.get("artist") else None)
            or [track_artist]
        )
        track_artists = [
            artist if normalize(p) == normalize(preferred.artist) else p
            for p in track_artists
        ]
        album_artists = (
            album_tags.get("albumartists")
            or release.tags.get("albumartists")
            or (
                release.tags.get("albumartist")
                if len(release.tags.get("albumartist", [])) > 1
                else None
            )
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
        tracks.append(
            TrackPlan(
                track,
                parent / name,
                entry_tags,
                track_sources,
                [key for key in withheld if key not in entry_tags],
            )
        )
    folder = directory_name(artist, album, date, preferred.medium, release)
    return Proposal(
        release,
        folder,
        tracks,
        sources,
        evidence,
        notes,
    )
