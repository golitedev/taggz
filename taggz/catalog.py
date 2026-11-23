import json
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import Config
from .release import Release, first


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


def medium_name(value: str) -> str:
    if "vinyl" in value.casefold():
        return "Vinyl"
    if value in ("Digital Media", "CD"):
        return value
    if value in ("Enhanced CD", "SACD (CD layer)"):
        return "CD"
    return ""


def display_credits(credits: list[dict]) -> tuple[str, list[str]]:
    names = [credit.get("name") or credit["artist"]["name"] for credit in credits]
    display = "".join(
        name + credit.get("joinphrase", "") for name, credit in zip(names, credits)
    )
    return display.strip(), names


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
    link = next(
        (
            x["url"]["resource"]
            for x in data.get("relations", [])
            if x.get("type") == "discogs"
            and "/release/" in x.get("url", {}).get("resource", "")
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
        link,
        aliases,
        data.get("disambiguation") or "",
        download_links,
    )


def from_discogs(data: dict) -> Edition:
    artists = [re.sub(r" \(\d+\)$", "", a["name"]) for a in data.get("artists", [])]
    formats = {medium_name(f["name"]) for f in data.get("formats", [])}
    medium = formats.pop() if len(formats) == 1 else ""
    entries = []
    for item in data.get("tracklist", []):
        if item.get("type_") != "track" or not item.get("position"):
            continue
        position = item["position"]
        match = re.fullmatch(r"(\d+)[-.](\d+)", position)
        disc, pos = (int(match[1]), int(match[2])) if match else (1, len(entries) + 1)
        if item.get("sub_tracks"):
            return Edition("", "", "", "", [], "")
        duration = item.get("duration", "").split(":")
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
    date = data.get("released", "").strip()
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
        self._cache: dict[tuple, dict] = {}
        self._next: dict[str, float] = {}
        self._agent = "Taggz/0.1.0" + (f" ({config.contact})" if config.contact else "")

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
        ids = set()
        previous = first(release.tags, "musicbrainz_albumid")
        if re.fullmatch(r"[0-9a-fA-F-]{36}", previous):
            ids.add(previous)
        for toc in release.tocs:
            results = self.get(
                "MusicBrainz", f"discid/{toc.id}", cdstubs="no", toc=toc.query
            )
            ids.update(item["id"] for item in results.get("releases", []))
        if not ids or release.barcode or release.catalog:
            queries = []
            if release.barcode:
                queries.append("barcode:" + re.sub(r"\D", "", release.barcode))
            if release.catalog:
                queries.append("catno:" + self.quote(release.catalog))
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
                ids.update(item["id"] for item in results.get("releases", [])[:8])
        include = (
            "artists+artist-credits+aliases+recordings+labels+release-groups+discids+isrcs"
            "+url-rels+recording-level-rels+artist-rels+work-rels+work-level-rels+genres"
        )
        editions = []
        for identifier in sorted(ids)[:24]:
            result = self.get("MusicBrainz", f"release/{identifier}", inc=include)
            if result.get("media"):
                try:
                    editions.append(from_musicbrainz(result))
                except (KeyError, TypeError, ValueError):
                    self.errors.add("MusicBrainz returned an incomplete release")
        return editions

    def discogs(self, release: Release, links: list[str]) -> list[Edition]:
        if self.offline or not self.config.discogs:
            return []
        ids = set()
        for link in links:
            match = re.search(r"discogs\.com/(?:[a-z]+/)?release/(\d+)", link)
            if match:
                ids.add(match[1])
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
