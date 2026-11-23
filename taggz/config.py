import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    staging: Path
    library: Path
    musicbrainz: bool = True
    discogs: bool = True
    discogs_token_env: str = "TAGGZ_DISCOGS_TOKEN"
    contact: str = ""
    replaygain: bool = True


def load_config(path: Path, *, container: bool = False) -> Config:
    with path.open("rb") as stream:
        raw = tomllib.load(stream)
    paths = raw["paths"]
    staging = Path(paths["staging"]).expanduser()
    library = Path(paths["library"]).expanduser()
    if not staging.is_absolute() or not library.is_absolute():
        raise ValueError("paths.staging and paths.library must be absolute")
    if container:
        staging, library = Path("/staging"), Path("/library")
        if not os.statvfs(library).f_flag & os.ST_RDONLY:
            raise ValueError("mount /library read-only")
    staging = staging.resolve(strict=True)
    library = library.resolve(strict=True)
    if not staging.is_dir() or not library.is_dir():
        raise ValueError("staging and library must be existing directories")
    if staging == library or staging in library.parents or library in staging.parents:
        raise ValueError(
            "staging and library must be separate, non-overlapping directories"
        )
    if staging.samefile(library):
        raise ValueError("staging and library point to the same directory")
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
    options = (
        matching.get("musicbrainz", True),
        matching.get("discogs", True),
        output.get("replaygain", True),
    )
    if any(type(option) is not bool for option in options):
        raise ValueError("musicbrainz, discogs, and replaygain must be booleans")
    return Config(
        staging, library, options[0], options[1], token_env, contact, options[2]
    )
