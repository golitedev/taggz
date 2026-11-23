import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    staging: Path
    musicbrainz: bool = True
    discogs: bool = True
    discogs_token_env: str = "TAGGZ_DISCOGS_TOKEN"
    contact: str = ""
    replaygain: bool = True
    qobuz: bool = True
    qobuz_credentials: Path | None = None
    qobuz_app_id: str = ""


def load_config(path: Path, *, container: bool = False) -> Config:
    with path.open("rb") as stream:
        raw = tomllib.load(stream)
    paths = raw["paths"]
    staging = Path(paths["staging"]).expanduser()
    if not staging.is_absolute():
        raise ValueError("paths.staging must be absolute")
    if container:
        staging = Path("/staging")
    staging = staging.resolve(strict=True)
    if not staging.is_dir():
        raise ValueError("staging must be an existing directory")
    matching = raw.get("matching", {})
    output = raw.get("output", {})
    token_env = matching.get("discogs_token_env", "TAGGZ_DISCOGS_TOKEN")
    if not isinstance(token_env, str) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", token_env
    ):
        raise ValueError("discogs_token_env must be an environment variable name")
    contact = matching.get("contact", "")
    if not isinstance(contact, str) or "\n" in contact or "\r" in contact:
        raise ValueError("matching.contact must be a single-line string")
    qobuz = raw.get("qobuz", {})
    credentials = qobuz.get("credentials_file", "")
    if not isinstance(credentials, str):
        raise ValueError("qobuz.credentials_file must be an absolute path")
    credentials_path = Path(credentials).expanduser() if credentials else None
    if credentials_path is not None:
        if not credentials_path.is_absolute():
            raise ValueError("qobuz.credentials_file must be an absolute path")
        if container:
            credentials_path = Path("/qobuz-credentials")
    app_id = qobuz.get("app_id", "")
    if not isinstance(app_id, str) or (
        app_id and not re.fullmatch(r"[0-9]{9}", app_id)
    ):
        raise ValueError("qobuz.app_id must be a quoted nine-digit app ID")
    options = dict(
        musicbrainz=matching.get("musicbrainz", True),
        discogs=matching.get("discogs", True),
        replaygain=output.get("replaygain", True),
        qobuz=qobuz.get("enabled", True),
    )
    if any(type(option) is not bool for option in options.values()):
        raise ValueError(
            "musicbrainz, discogs, replaygain and qobuz.enabled must be booleans"
        )
    return Config(
        staging,
        discogs_token_env=token_env,
        contact=contact,
        qobuz_credentials=credentials_path,
        qobuz_app_id=app_id,
        **options,
    )
