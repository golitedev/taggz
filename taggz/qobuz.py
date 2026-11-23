"""Read-only access to the Qobuz album catalog."""

import json
import os
import re
import time
import tomllib
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import Config
from .release import Release, barcode_key, first, normalize

PLAYER = "https://play.qobuz.com"
API = "https://www.qobuz.com/api.json/0.2/"
ALBUM_ID = re.compile(r"[A-Za-z0-9]{5,40}")


def credentials(config: Config) -> tuple[str, str]:
    data = {}
    if config.qobuz_credentials is not None:
        text = config.qobuz_credentials.read_text(encoding="utf-8-sig")
        try:
            data = tomllib.loads(text).get("qobuz", {})
        except tomllib.TOMLDecodeError:
            # Also accept the exported `qobuz: / user_id: / token:` block.
            data = {
                match[1]: match[2].strip().strip("\"'")
                for match in re.finditer(
                    r"^\s*(token|user_auth_token|app_id)\s*:\s*([^\r\n]+)$",
                    text,
                    re.M,
                )
            }
    token = (
        os.environ.get("TAGGZ_QOBUZ_TOKEN")
        or data.get("token")
        or data.get("user_auth_token")
    )
    if not token and data.get("use_auth_token") is True:
        token = data.get("password_or_token")
    app_id = (
        os.environ.get("TAGGZ_QOBUZ_APP_ID")
        or config.qobuz_app_id
        or data.get("app_id")
        or ""
    )
    if token and (
        not isinstance(token, str)
        or not token.isascii()
        or not token.isprintable()
        or " " in token
    ):
        raise ValueError("Qobuz credentials must contain an authentication token")
    if app_id and not re.fullmatch(r"[0-9]{9}", str(app_id)):
        raise ValueError("invalid Qobuz app ID")
    return token or "", str(app_id)


class Qobuz:
    def __init__(self, config: Config, errors: set[str], *, offline: bool):
        self.errors = errors
        self._token = self._app_id = ""
        self._blocked = ""
        self._next = 0.0
        self._cache: dict[tuple, dict] = {}
        if not offline and config.qobuz:
            try:
                self._token, self._app_id = credentials(config)
                if not self._token and config.qobuz_credentials is not None:
                    self._blocked = "Qobuz credentials file does not contain a token"
            except (OSError, ValueError, AttributeError):
                self._blocked = "Qobuz credentials could not be read or are invalid"

    @property
    def configured(self) -> bool:
        return bool(self._token or self._blocked)

    def app_id(self) -> str:
        if not self._app_id:
            with urlopen(PLAYER + "/login", timeout=10) as response:
                page = response.read().decode()
            script = re.search(r'<script[^>]+src="(/resources/[^"]+/bundle\.js)"', page)
            if script:
                with urlopen(PLAYER + script[1], timeout=10) as response:
                    bundle = response.read().decode()
                match = re.search(r'production:\{api:\{appId:"(\d{9})"', bundle)
                if match:
                    self._app_id = match[1]
        return self._app_id

    def get(self, endpoint: str, **params) -> dict:
        key = (endpoint, tuple(sorted(params.items())))
        if key in self._cache:
            return self._cache[key]
        if self._blocked:
            self.errors.add(self._blocked)
            return {}
        if not self._token:
            return {}
        try:
            app_id = self.app_id()
        except (OSError, ValueError):
            app_id = ""
        if not app_id:
            self._blocked = "Qobuz app ID discovery failed. Configure qobuz.app_id."
            self.errors.add(self._blocked)
            return {}
        url = API + endpoint + "?" + urlencode(params | {"app_id": app_id})
        request = Request(
            url, headers={"X-App-Id": app_id, "Accept": "application/json"}
        )
        request.add_unredirected_header("X-User-Auth-Token", self._token)
        for attempt in range(2):
            delay = self._next - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._next = time.monotonic() + 0.25
            try:
                with urlopen(request, timeout=10) as response:
                    result = json.load(response)
                if isinstance(result, dict):
                    self._cache[key] = result
                    return result
                break
            except HTTPError as error:
                if error.code == 404:
                    self._cache[key] = {}
                    return {}
                if error.code in {401, 403}:
                    self._blocked = f"Qobuz authorization failed (HTTP {error.code})"
                    self.errors.add(self._blocked)
                    return {}
                if error.code not in {429, 500, 502, 503, 504} or attempt:
                    self.errors.add(f"Qobuz HTTP {error.code}")
                    return {}
                time.sleep(2)
            except (URLError, OSError, ValueError):
                if attempt:
                    break
        self.errors.add("Qobuz request failed or returned an invalid response")
        return {}

    def album(self, album_id: str) -> dict:
        result = self.get("album/get", album_id=album_id, limit=100, offset=0)
        if not result:
            return {}
        section = result.get("tracks") or {}
        items = list(section.get("items") or [])
        total = result.get("tracks_count") or section.get("total") or len(items)
        while items and len(items) < total:
            page = self.get(
                "album/get", album_id=album_id, limit=100, offset=len(items)
            )
            extra = (page.get("tracks") or {}).get("items") or []
            if not extra or str(extra[0].get("id")) in {
                str(item.get("id")) for item in items
            }:
                break
            items.extend(extra)
        if not items or len(items) != total:
            self.errors.add("Qobuz returned an incomplete album track list")
            return {}
        return result | {"tracks": section | {"items": items}}

    def albums(self, release: Release) -> list[dict]:
        if not self.configured:
            return []
        direct = first(release.tags, "qobuz_album_id")
        codes = (
            [release.barcode, barcode_key(release.barcode).zfill(13)]
            if release.barcode
            else []
        )
        found = {}
        for album_id in dict.fromkeys([direct, *codes]):
            if ALBUM_ID.fullmatch(album_id) and (album := self.album(album_id)):
                found[str(album["id"])] = album
        if direct and direct in found:
            return list(found.values())
        if (
            any(
                barcode_key(str(album.get("upc") or "")) == barcode_key(release.barcode)
                for album in found.values()
            )
            and release.barcode
        ):
            return list(found.values())
        base_title = (
            re.sub(r"(?:\s+\([^()]+\))+$", "", release.title).strip() or release.title
        )
        title = normalize(base_title)
        full_title = normalize(release.title)
        for query in dict.fromkeys(
            (
                f"{release.artist} {base_title}",
                f"{release.artist} {release.title}",
                base_title,
            )
        ):
            results = self.get("album/search", query=query, limit=24, offset=0)
            items = (results.get("albums") or {}).get("items") or []
            # Prefer a matching UPC and version before popularity ranking.
            items = sorted(
                items,
                key=lambda item: (
                    bool(
                        release.barcode
                        and barcode_key(str(item.get("upc") or ""))
                        == barcode_key(release.barcode)
                    ),
                    full_title
                    in {
                        normalize(item.get("title") or ""),
                        normalize(
                            (item.get("title") or "") + (item.get("version") or "")
                        ),
                    },
                    normalize((item.get("artist") or {}).get("name") or "")
                    == normalize(release.artist),
                ),
                reverse=True,
            )
            count_before = len(found)
            for item in items:
                name = normalize(item.get("title") or "")
                album_id = str(item.get("id") or "")
                if (
                    not name
                    or not (
                        name == title
                        or (
                            len(title) >= 4
                            and (name.startswith(title) or title.startswith(name))
                        )
                    )
                    or item.get("tracks_count") not in {None, len(release.tracks)}
                    or not ALBUM_ID.fullmatch(album_id)
                    or album_id in found
                ):
                    continue
                if album := self.album(album_id):
                    found[str(album["id"])] = album
                if len(found) >= 8:
                    break
            if len(found) > count_before:
                break
        return list(found.values())
