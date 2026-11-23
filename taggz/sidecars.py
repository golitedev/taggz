import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .match import Proposal
from .release import Review, disc_number, normalize, text_file

FILE = re.compile(r'^(\s*FILE\s+)"([^"]+)"(\s+\S+\s*)$', re.I)
AUDIO_REF = re.compile(r"\.(?:wav|flac|aif|aiff|ape|wv|mp3)$", re.I)


@dataclass(frozen=True)
class Sidecar:
    old: Path
    new: Path
    contents: bytes | None = None


def reference(value: str, sidecar: Path, destinations: dict[Path, Path]) -> Path:
    src = Path(value.replace("\\", "/"))
    path = Path(os.path.normpath(sidecar.parent / src))
    direct = [
        old
        for old in destinations
        if old.parent == path.parent and normalize(old.stem) == normalize(path.stem)
    ]
    if len(direct) == 1:
        return direct[0]
    # Rippers can put Windows drive paths in a cue. An unambiguous basename
    # still maps to a file in this release; the referenced path is never opened.
    by_name = [
        old for old in destinations if normalize(old.stem) == normalize(src.stem)
    ]
    if len(by_name) == 1:
        return by_name[0]
    prefix = re.match(r"^(\d+)(?:[ ._-]|$)", src.stem)
    if prefix and AUDIO_REF.search(value):
        matches = [
            old
            for old, new in destinations.items()
            if disc_number(new) == disc_number(sidecar)
            and int(new.name.split(" ", 1)[0]) == int(prefix[1])
        ]
        if len(matches) == 1:
            return matches[0]
    raise Review(f"cannot resolve {value!r} in {sidecar}")


def plan_sidecars(proposal: Proposal) -> list[Sidecar]:
    dest = {item.track.path: item.path for item in proposal.tracks}
    by_stem = {(p.parent, p.stem.casefold()): p for p in dest}
    extras = sorted(
        path
        for path, info in proposal.release.files.items()
        if stat.S_ISREG(info[2]) and path not in dest
    )
    output = set(dest.values())
    changes = []
    for sidecar in extras:
        extension = sidecar.suffix.casefold()
        content = None
        target = sidecar
        if extension in (".cue", ".m3u", ".m3u8"):
            text = text_file(proposal.release.root / sidecar)
            sep = "\r\n" if "\r\n" in text else "\n"
            lines = text.splitlines()
            seen = set()
            if extension == ".cue":
                for idx, line in enumerate(lines):
                    match = FILE.fullmatch(line)
                    if match is None:
                        if re.match(r"^\s*FILE\b", line, re.I):
                            raise Review(f"unsupported cue FILE line in {sidecar}")
                        continue
                    original = reference(match[2], sidecar, dest)
                    if original in seen:
                        raise Review(f"duplicate cue file reference in {sidecar}")
                    seen.add(original)
                    new = os.path.relpath(dest[original], sidecar.parent)
                    lines[idx] = f'{match[1]}"{new}" WAVE'
                tracks = sum(
                    bool(re.match(r"^\s*TRACK\s+\d+\s+AUDIO\s*$", line, re.I))
                    for line in lines
                )
                if len(seen) != tracks:
                    raise Review(
                        f"cue refers to an image or contains unmapped tracks: {sidecar}"
                    )
            else:
                for idx, line in enumerate(lines):
                    if not line.strip() or line.lstrip().startswith("#"):
                        continue
                    original = reference(line.strip(), sidecar, dest)
                    seen.add(original)
                    lines[idx] = os.path.relpath(dest[original], sidecar.parent)
                target = sidecar.with_suffix(".m3u8")
            if not seen:
                raise Review(f"no track references in {sidecar}")
            content = (sep.join(lines) + sep).encode(
                "utf-8-sig" if extension == ".cue" else "utf-8"
            )
        if extension == ".lrc":
            original = by_stem.get((sidecar.parent, sidecar.stem.casefold()))
            if original is not None:
                target = dest[original].with_suffix(".lrc")
        if target in output:
            raise Review(f"two files would have the same destination: {target}")
        output.add(target)
        changes.append(Sidecar(sidecar, target, content))
    # A file cannot also be the directory that a new track must be moved into.
    if any(parent in output for path in output for parent in path.parents):
        raise Review("a filename collides with an output folder")
    return changes
