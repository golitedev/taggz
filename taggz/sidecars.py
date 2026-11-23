import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .match import Proposal
from .release import CUE_FILE, Review, audio_reference, cue_tracks, text_file


@dataclass(frozen=True)
class Sidecar:
    old: Path
    new: Path
    contents: bytes | None = None


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
            if extension == ".cue":
                for entry in cue_tracks(lines, sidecar, proposal.release.tracks):
                    prefix = CUE_FILE.fullmatch(lines[entry.file_line])[1]
                    new = os.path.relpath(dest[entry.track.path], sidecar.parent)
                    lines[entry.file_line] = f'{prefix}"{new}" WAVE'
            else:
                found = False
                for idx, line in enumerate(lines):
                    if not line.strip() or line.lstrip().startswith("#"):
                        continue
                    original = audio_reference(
                        line.strip(), sidecar, proposal.release.tracks
                    )
                    found = True
                    lines[idx] = os.path.relpath(dest[original.path], sidecar.parent)
                if not found:
                    raise Review(f"no track references in {sidecar}")
                target = sidecar.with_suffix(".m3u8")
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
    folders = {
        path for path, info in proposal.release.files.items() if stat.S_ISDIR(info[2])
    }
    folders.update(parent for path in output for parent in path.parents)
    if output & folders:
        raise Review("a filename collides with an output folder")
    return changes
