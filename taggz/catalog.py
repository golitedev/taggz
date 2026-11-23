import json
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .config import Config
from .qobuz import ALBUM_ID, Qobuz
from .release import Release, first, normalize


@dataclass
class Entry:
    disc: int
    number: int
    title: str
    seconds: float | None
    tags: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class Edition:
    artist: str
    title: str
    date: str
    medium: str
    tracks: list[Entry]
    source: str
    tags: dict[str, list[str]] = field(default_factory=dict)
    discids: set[tuple[int, str]] = field(default_factory=set)
    lookup: str = ""
    aliases: set[str] = field(default_factory=set)
    variant: str = ""
    download_links: list[str] = field(default_factory=list)


def fields(**values) -> dict[str, list[str]]:
    return {
        key: [value] if isinstance(value, str) else list(dict.fromkeys(value))
        for key, value in values.items()
        if value
    }


class Description(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "div", "li"}:
            self.parts.append("\n")


QOBUZ_CREDIT_ROLES = {
    "composer": ("composer",),
    "composerlyricist": ("composer", "lyricist"),
    "lyricist": ("lyricist",),
    "writer": ("writer",),
    "songwriter": ("writer",),
    "author": ("writer",),
    "producer": ("producer",),
    "coproducer": ("producer",),
    "arranger": ("arranger",),
    "orchestrator": ("arranger",),
    "conductor": ("conductor",),
    "mixer": ("mixer",),
    "mixingengineer": ("mixer",),
    "mixengineer": ("mixer",),
    "engineer": ("engineer",),
    "recordingengineer": ("engineer",),
    "masteringengineer": ("mastering", "engineer"),
    "masterer": ("mastering", "engineer"),
    "remasteringengineer": ("mastering", "engineer"),
    "remixer": ("remixer",),
    "director": ("director",),
    "musicdirector": ("director",),
    "musicpublisher": ("publisher",),
}
QOBUZ_INSTRUMENT = re.compile(
    r"performer|vocal|guitar|bass|drum|percussion|piano|keyboard|synth|saxophone|"
    r"trumpet|trombone|flute|clarinet|violin|viola|cello|organ|strings|harp|"
    r"accordion|orchestra|ensemble|choir|soloist|voice|instrument|horn|programming|\brap(?:per)?\b",
    re.I,
)


def qobuz_name(value) -> str:
    name = value.get("name") if isinstance(value, dict) else value
    return name.strip() if isinstance(name, str) else ""


def qobuz_version(title: str, version: str) -> str:
    already_present = version and (
        normalize(title).endswith(normalize(version))
        or any(
            normalize(part).startswith(normalize(version))
            for part in re.findall(r"\(([^()]*)\)", title)
        )
    )
    if version and not already_present:
        return f"{title} ({version})"
    return title


def qobuz_date(value) -> str:
    if isinstance(value, str):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    return ""


def qobuz_artists(items: list[dict]) -> tuple[list[str], list[str]]:
    main, featured = [], []
    for item in items:
        name = qobuz_name(item)
        roles = {normalize(role) for role in item.get("roles") or []}
        if name and "featuredartist" in roles:
            featured.append(name)
        elif name and (not roles or "mainartist" in roles):
            main.append(name)
    return main, featured


def qobuz_display(main: list[str], featured: list[str]) -> str:
    if not main:
        return ", ".join(featured)
    primary = ", ".join(main)
    return f"{primary} feat. {', '.join(featured)}" if featured else primary


def qobuz_credits(
    text: str,
    primary: str,
) -> tuple[dict[str, list[str]], list[str], list[str]]:
    credits = defaultdict(list)
    main, featured = [], []
    for segment in re.split(r"\s+-\s+", text):
        parts = [item.strip() for item in segment.strip().split(",")]
        # A name can contain commas. Split at the first recognizable role.
        for i, part in enumerate(parts[1:], 1):
            key = normalize(part)
            if (
                key in QOBUZ_CREDIT_ROLES
                or key in {"mainartist", "featuredartist"}
                or QOBUZ_INSTRUMENT.search(part)
                or "engineer" in key
            ):
                break
        else:
            continue
        name = ", ".join(parts[:i]).strip()
        if not name:
            continue
        roles = parts[i:]
        keys = {normalize(role) for role in roles}
        if "featuredartist" in keys:
            featured.append(name)
        elif "mainartist" in keys and (
            normalize(name) == normalize(primary)
            or any(
                QOBUZ_INSTRUMENT.search(role)
                and normalize(role) not in {"performer", "associatedperformer"}
                for role in roles
            )
            or not keys & QOBUZ_CREDIT_ROLES.keys()
        ):
            main.append(name)
        for role in roles:
            key = normalize(role)
            tags = QOBUZ_CREDIT_ROLES.get(key, ())
            if not tags and ("engineer" in key or "engineering" in key):
                tags = ("engineer",)
            for tag in tags:
                credits[tag].append(name)
            if (
                not tags
                and key != "associatedperformer"
                and QOBUZ_INSTRUMENT.search(role)
            ):
                credits["performer"].append(f"{name} ({role})")
    return fields(**credits), main, featured


def from_qobuz(data: dict) -> Edition:
    album_id = str(data["id"])
    if not ALBUM_ID.fullmatch(album_id):
        raise ValueError("invalid album ID")
    main, featured = qobuz_artists(data.get("artists") or [])
    primary = qobuz_name(data.get("artist"))
    main = main or ([primary] if primary else [])
    artist = qobuz_display(main, featured)
    original = qobuz_date(data.get("release_date_original"))
    released = next(
        (
            value
            for key in ("release_date_download", "release_date_stream", "release_date")
            if (value := qobuz_date(data.get(key)))
        ),
        original,
    )
    description = Description()
    description.feed(data.get("description") or "")
    album_tags = fields(
        albumartists=[*main, *featured],
        albumcomposer=qobuz_name(data.get("composer")),
        barcode=str(data.get("upc") or ""),
        copyright=qobuz_name(data.get("copyright")),
        description="\n".join(
            line.strip()
            for line in "".join(description.parts).splitlines()
            if line.strip()
        ),
        genre=qobuz_name(data.get("genre")),
        label=qobuz_name(data.get("label")),
        originaldate=original,
        compilation="1" if data.get("release_type") == "compilation" else "",
        releasetype=data.get("release_type"),
        qobuz_album_id=album_id,
    )
    version = qobuz_name(data.get("version"))
    entries = []
    for item in data["tracks"]["items"]:
        track_primary = qobuz_name(item.get("performer"))
        direct_main, direct_featured = qobuz_artists(item.get("artists") or [])
        credits, parsed_main, parsed_featured = qobuz_credits(
            item.get("performers") or "", track_primary
        )
        featured_names = list(dict.fromkeys([*direct_featured, *parsed_featured]))
        main_names = (
            direct_main or parsed_main or ([track_primary] if track_primary else [])
        )
        main_names = [
            name
            for name in main_names
            if normalize(name) not in {normalize(f) for f in featured_names}
        ]
        track_artist = qobuz_display(main_names, featured_names)
        composer = qobuz_name(item.get("composer"))
        if composer:
            credits.setdefault("composer", [composer])
        warning = item.get("parental_warning")
        work = qobuz_name(item.get("work"))
        title = qobuz_version(item["title"].strip(), qobuz_name(item.get("version")))
        if work and normalize(work) not in normalize(title):
            title = f"{work}: {title}"
        tags = (
            fields(
                artist=track_artist,
                artists=[*main_names, *featured_names],
                copyright=qobuz_name(item.get("copyright")),
                isrc=qobuz_name(item.get("isrc")).upper(),
                itunesadvisory="1"
                if warning is True
                else "0"
                if warning is False
                else "",
                qobuz_track_id=str(item["id"]),
                work=work,
            )
            | credits
        )
        entries.append(
            Entry(
                int(item["media_number"]),
                int(item["track_number"]),
                title,
                item.get("duration") or None,
                tags,
            )
        )
    entries.sort(key=lambda item: (item.disc, item.number))
    return Edition(
        artist,
        qobuz_version(data["title"].strip(), version),
        released,
        "Digital Media",
        entries,
        f"https://open.qobuz.com/album/{album_id}",
        album_tags,
        variant=version,
    )


def medium_name(value: str) -> str:
    if "vinyl" in value.casefold():
        return "Vinyl"
    if value in ("Digital Media", "CD"):
        return value
    if value in ("Enhanced CD", "SACD (CD layer)"):
        return "CD"
    if "DVD" in value.upper() or "BLU-RAY" in value.upper():
        return "Video Disc"
    return ""


def display_credits(credits: list[dict]) -> tuple[str, list[str]]:
    names = [credit.get("name") or credit["artist"]["name"] for credit in credits]
    display = "".join(
        name + credit.get("joinphrase", "") for name, credit in zip(names, credits)
    )
    return display.strip(), names


def discogs_release_id(link: str) -> str:
    url = urlsplit(link)
    if url.hostname not in {"discogs.com", "www.discogs.com", "api.discogs.com"}:
        return ""
    match = re.match(r"^/(?:[a-z]+/)?releases?/(\d+)(?:[-/]|$)", url.path, re.I)
    return match[1] if match else ""


def relation_fields(recording: dict) -> dict[str, list[str]]:
    out = defaultdict(list)

    def add(relation, role, detail=""):
        artist = relation.get("artist", {})
        name = relation.get("target-credit") or artist.get("name")
        if name:
            out[role].append(name + (f" ({detail})" if detail else ""))

    for relation in recording.get("relations", []):
        kind = relation["type"]
        if kind in {
            "producer",
            "engineer",
            "arranger",
            "conductor",
            "mix",
            "mastering",
        }:
            add(relation, "mixartist" if kind == "mix" else kind)
        if kind in {"instrument", "vocal", "performer"}:
            attributes = ", ".join(relation.get("attributes") or [kind])
            add(relation, "performer", attributes)
        if kind == "performance":
            work = relation.get("work", {})
            if work.get("id"):
                out["musicbrainz_workid"].append(work["id"])
            for credit in work.get("relations", []):
                if credit["type"] in {"composer", "lyricist", "writer", "arranger"}:
                    add(credit, credit["type"])
    return fields(**out)


def from_musicbrainz(data: dict) -> Edition:
    credits = data.get("artist-credit", [])
    artist, artists = display_credits(credits)
    aliases = set()
    if len(credits) == 1:
        aliases = {
            a["name"]
            for a in credits[0]["artist"].get("aliases", [])
            if a.get("name") and a.get("type") != "Legal name"
        }
    group = data.get("release-group") or {}
    info = data.get("label-info", [])
    primary_type = (group.get("primary-type") or "").lower()
    entries = []
    discids = set()
    media = data.get("media", [])
    formats = {medium_name(d.get("format") or "") for d in media}
    for disc in media:
        for item in disc.get("discs", []):
            discids.add((disc["position"], item["id"]))
        for item in disc.get("tracks", []):
            recording = item.get("recording", {})
            credits = item.get("artist-credit") or recording.get("artist-credit") or []
            display, names = display_credits(credits)
            duration = item.get("length") or recording.get("length")
            tags = fields(
                artist=display,
                artists=names,
                isrc=recording.get("isrcs"),
                musicbrainz_artistid=[c["artist"]["id"] for c in credits],
                musicbrainz_releasetrackid=item.get("id"),
                musicbrainz_trackid=recording.get("id"),
                vinylposition=item.get("number") if formats == {"Vinyl"} else None,
            )
            tags.update(relation_fields(recording))
            entries.append(
                Entry(
                    disc["position"],
                    item["position"],
                    item["title"],
                    duration / 1000 if duration else None,
                    tags,
                )
            )
    entries.sort(key=lambda item: (item.disc, item.number))
    tags = fields(
        albumartists=artists,
        label=[
            v["label"]["name"]
            for v in info
            if v.get("label") and v["label"]["name"] != "[no label]"
        ],
        catalognumber=[v["catalog-number"] for v in info if v.get("catalog-number")],
        barcode=data.get("barcode"),
        releasecountry=data.get("country"),
        releasestatus=(data.get("status") or "").lower(),
        originaldate=group.get("first-release-date"),
        releasetype=([primary_type] if primary_type else [])
        + [v.lower() for v in group.get("secondary-types", [])],
        musicbrainz_albumid=data["id"],
        musicbrainz_releasegroupid=group.get("id"),
        musicbrainz_albumartistid=[
            c["artist"]["id"] for c in data.get("artist-credit", [])
        ],
    )
    genres = sorted(
        group.get("genres", []) + data.get("genres", []),
        key=lambda x: x["count"],
        reverse=True,
    )
    if genres:
        tags["genre"] = [genres[0]["name"]]
    discogs_id = next(
        (
            code
            for x in data.get("relations", [])
            if x.get("type") == "discogs"
            and (code := discogs_release_id(x.get("url", {}).get("resource", "")))
        ),
        "",
    )
    download_links = [
        r["url"]["resource"]
        for r in data.get("relations", [])
        if r.get("type") == "purchase for download" and r.get("url", {}).get("resource")
    ]
    return Edition(
        artist,
        data["title"],
        data.get("date") or "",
        formats.pop() if len(formats) == 1 else "",
        entries,
        f"https://musicbrainz.org/release/{data['id']}",
        tags,
        discids,
        f"https://www.discogs.com/release/{discogs_id}" if discogs_id else "",
        aliases,
        data.get("disambiguation") or "",
        download_links,
    )


def from_discogs(data: dict) -> Edition:
    artists = [re.sub(r" \(\d+\)$", "", a["name"]) for a in data.get("artists", [])]
    formats = {
        "Digital Media" if f["name"] == "File" else medium_name(f["name"])
        for f in data.get("formats", [])
    }
    medium = formats.pop() if len(formats) == 1 else ""
    entries = []
    positions = defaultdict(int)
    for item in data.get("tracklist", []):
        if item.get("type_") != "track" or not item.get("position"):
            continue
        position = item["position"]
        match = re.fullmatch(r"(?:CD\s*)?(\d+)[-.](\d+)", position, re.I)
        side = (
            re.fullmatch(r"([A-Z])\d+", position, re.I) if medium == "Vinyl" else None
        )
        disc = 1
        if match:
            disc = int(match[1])
        elif side:
            disc = (ord(side[1].upper()) - ord("A")) // 2 + 1
        pos = int(match[2]) if match else positions[disc] + 1
        positions[disc] = pos
        if item.get("sub_tracks"):
            return Edition("", "", "", "", [], "")
        duration = (item.get("duration") or "").split(":")
        seconds = (
            int(duration[0]) * 60 + int(duration[1])
            if len(duration) == 2 and all(p.isdigit() for p in duration)
            else None
        )
        tags = fields(vinylposition=position if medium == "Vinyl" else "")
        names = [re.sub(r" \(\d+\)$", "", a["name"]) for a in item.get("artists", [])]
        if names:
            tags.update(fields(artists=names, artist=", ".join(names)))
        roles = defaultdict(list)
        for credit in item.get("extraartists", []):
            for role in credit["role"].split(", "):
                key = {
                    "Written-By": "writer",
                    "Composed By": "composer",
                    "Lyrics By": "lyricist",
                    "Producer": "producer",
                    "Mixed By": "mixartist",
                    "Arranged By": "arranger",
                    "Mastered By": "mastering",
                    "Featuring": "performer",
                }.get(role)
                if key:
                    name = re.sub(r" \(\d+\)$", "", credit["name"])
                    roles[key].append(
                        name + (" (Featured)" if key == "performer" else "")
                    )
        tags.update(fields(**roles))
        entries.append(Entry(disc, pos, item["title"], seconds, tags))
    tags = fields(
        albumartists=artists,
        label=[x["name"] for x in data.get("labels", [])],
        catalognumber=[
            x["catno"]
            for x in data.get("labels", [])
            if x.get("catno") not in (None, "", "none")
        ],
        barcode=[
            x["value"] for x in data.get("identifiers", []) if x["type"] == "Barcode"
        ],
        genre=(data.get("styles") or data.get("genres") or [])[:1],
        discogs_releaseid=str(data["id"]),
    )
    date = (data.get("released") or "").strip()
    if date.endswith("-00-00"):
        date = date[:4]
    elif date.endswith("-00"):
        date = date[:-3]
    return Edition(
        ", ".join(artists),
        data["title"],
        date,
        medium,
        entries,
        f"https://www.discogs.com/release/{data['id']}",
        tags,
    )


class Catalog:
    def __init__(self, config: Config, *, offline: bool):
        self.config = config
        self.offline = offline
        self.errors: set[str] = set()
        self.qobuz_client = Qobuz(config, self.errors, offline=offline)
        self._cache: dict[tuple, dict] = {}
        self._next: dict[str, float] = {}
        self._agent = "Taggz/0.1.0" + (f" ({config.contact})" if config.contact else "")

    def qobuz(self, release: Release) -> list[Edition]:
        editions = []
        try:
            albums = self.qobuz_client.albums(release)
        except (AttributeError, KeyError, TypeError, ValueError):
            self.errors.add("Qobuz returned invalid search or album metadata")
            return []
        for album in albums:
            try:
                editions.append(from_qobuz(album))
            except (AttributeError, KeyError, OverflowError, TypeError, ValueError):
                self.errors.add("Qobuz returned incomplete or invalid album metadata")
        return editions

    def get(self, service: str, endpoint: str, **params) -> dict:
        key = (service, endpoint, tuple(sorted(params.items())))
        if key in self._cache:
            return self._cache[key]
        mb = service == "MusicBrainz"
        base = "https://musicbrainz.org/ws/2/" if mb else "https://api.discogs.com/"
        headers = {"User-Agent": self._agent, "Accept": "application/json"}
        token = os.environ.get(self.config.discogs_token_env)
        if not mb and token:
            headers["Authorization"] = f"Discogs token={token}"
        if mb:
            params["fmt"] = "json"
        url = base + endpoint + "?" + urlencode(params)
        for attempt in range(2):
            delay = self._next.get(service, 0) - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._next[service] = time.monotonic() + 1.1
            try:
                with urlopen(Request(url, headers=headers), timeout=12) as response:
                    result = json.load(response)
                if isinstance(result, dict):
                    self._cache[key] = result
                    return result
                self.errors.add(f"{service} returned an invalid response")
                break
            except HTTPError as error:
                if error.code == 404:
                    self._cache[key] = {}
                    return {}
                if error.code not in (429, 500, 502, 503, 504) or attempt:
                    self.errors.add(f"{service} HTTP {error.code}")
                    break
                time.sleep(2)
            except (URLError, TimeoutError, json.JSONDecodeError, UnicodeError):
                if attempt:
                    self.errors.add(f"{service} request failed")
        return {}

    @staticmethod
    def quote(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def musicbrainz(self, release: Release) -> list[Edition]:
        if self.offline or not self.config.musicbrainz:
            return []
        ids = {}

        def add_ids(result: dict, limit: int = 24) -> None:
            for item in result.get("releases", [])[:limit]:
                ids.setdefault(item["id"].lower(), None)

        previous = first(release.tags, "musicbrainz_albumid")
        if re.fullmatch(r"[0-9a-fA-F-]{36}", previous):
            ids[previous.lower()] = None
        for toc in release.tocs:
            results = self.get(
                "MusicBrainz", f"discid/{toc.id}", cdstubs="no", toc=toc.query
            )
            if results.get("id") != toc.id:
                # A fuzzy TOC response can suggest completely unrelated CDs
                # with similar track lengths.
                results = {
                    "releases": [
                        item
                        for item in results.get("releases", [])
                        if normalize(item.get("title", "")) == normalize(release.title)
                    ]
                }
            add_ids(results)
        if not ids or release.barcode or release.catalog:
            queries = []
            if release.barcode:
                code = re.sub(r"\D", "", release.barcode).lstrip("0")
                if code:
                    queries.append(
                        "("
                        + " OR ".join(
                            "barcode:" + code.zfill(length)
                            for length in sorted({len(code), 12, 13, 14})
                            if length >= len(code)
                        )
                        + ")"
                    )
            if release.catalog:
                catalog = release.catalog.split()[-1]
                if re.fullmatch(r"[A-Za-z0-9-]{5,}", catalog) and re.search(
                    r"\d", catalog
                ):
                    queries.append("catno:" + self.quote(catalog))
            if not ids:
                queries.append(
                    "release:"
                    + self.quote(release.title)
                    + " AND artist:"
                    + self.quote(release.artist)
                    + f" AND tracks:{len(release.tracks)}"
                )
            for query in queries:
                results = self.get("MusicBrainz", "release", query=query, limit=12)
                add_ids(results, 8)
        if not ids:
            # The catalog can index a newer stage name or a different artist
            # credit. Results from this search still need normal verification.
            results = self.get(
                "MusicBrainz",
                "release",
                query="release:"
                + self.quote(release.title)
                + f" AND tracks:{len(release.tracks)}",
                limit=12,
            )
            add_ids(results, 12)
        include = (
            "artists+artist-credits+aliases+recordings+labels+release-groups+discids+isrcs"
            "+url-rels+recording-level-rels+artist-rels+work-rels+work-level-rels+genres"
        )
        editions = {}
        for identifier in list(ids)[:24]:
            result = self.get("MusicBrainz", f"release/{identifier}", inc=include)
            if result.get("media"):
                try:
                    # MusicBrainz resolves old IDs for merged releases.
                    edition = from_musicbrainz(result)
                    editions.setdefault(edition.source, edition)
                except (KeyError, TypeError, ValueError):
                    self.errors.add("MusicBrainz returned an incomplete release")
        return list(editions.values())

    def discogs(self, release: Release, links: list[str]) -> list[Edition]:
        if self.offline or not self.config.discogs:
            return []
        ids = {code for link in links if (code := discogs_release_id(link))}
        if os.environ.get(self.config.discogs_token_env):
            for key, value in (
                ("barcode", release.barcode),
                ("catno", release.catalog),
            ):
                if value:
                    result = self.get(
                        "Discogs",
                        "database/search",
                        type="release",
                        per_page=8,
                        **{key: value},
                    )
                    ids.update(str(row["id"]) for row in result.get("results", []))
        editions = []
        for identifier in sorted(ids)[:12]:
            result = self.get("Discogs", f"releases/{identifier}")
            if result.get("tracklist"):
                try:
                    editions.append(from_discogs(result))
                except (KeyError, TypeError, ValueError):
                    self.errors.add("Discogs returned an incomplete release")
        return editions
