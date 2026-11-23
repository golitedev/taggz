import argparse
import json
import shutil
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path

from mutagen import MutagenError

from .catalog import Catalog
from .config import load_config
from .match import Library, propose
from .release import Review, read_release
from .sidecars import plan_sidecars
from .storage import RESERVED, Staging, staging_lock


def release_paths(root: Path, names: list[str]) -> list[Path]:
    if not names:
        return [
            path
            for path in sorted(root.iterdir())
            if path.name not in RESERVED
            and not path.name.startswith(".")
            and path.is_dir()
            and not path.is_symlink()
        ]
    paths = []
    for name in dict.fromkeys(names):
        path = root / name
        if (
            Path(name).name != name
            or name.startswith(".")
            or name in RESERVED
            or path.is_symlink()
            or not path.is_dir()
        ):
            raise ValueError(f"not a release folder directly in staging: {name!r}")
        paths.append(path)
    return paths


def run(args) -> tuple[list[dict], int]:
    cfg = load_config(args.config, container=args.container)
    if args.apply and cfg.replaygain and not shutil.which("metaflac"):
        raise ValueError("metaflac is required when ReplayGain is enabled")
    catalog = Catalog(cfg, offline=args.offline)
    state = Staging(cfg.staging)
    reports = []
    status = 0
    with staging_lock(cfg.staging) if args.apply else nullcontext():
        if args.apply:
            state.setup()
        library = Library(cfg.library)
        for path in release_paths(cfg.staging, args.releases):
            sources = []
            library_matches = []
            catalog.errors.clear()
            try:
                release = read_release(path)
                proposal = propose(release, library, catalog)
                sources, library_matches = proposal.sources, proposal.library
                report = proposal.report()
                sidecars = plan_sidecars(proposal)
                report["sidecars"] = [
                    {
                        "old": str(item.old),
                        "new": str(item.new),
                        "rewritten": item.contents is not None,
                    }
                    for item in sidecars
                    if item.old != item.new or item.contents is not None
                ]
                report["replaygain"] = cfg.replaygain
                if catalog.errors:
                    report["provider_errors"] = sorted(catalog.errors)
                if args.apply:
                    state.apply(proposal, sidecars, report, replaygain=cfg.replaygain)
                reports.append(report)
            except (OSError, MutagenError, Review, subprocess.TimeoutExpired) as error:
                report = {
                    "source": path.name,
                    "outcome": "needs-review",
                    "reason": str(error),
                    "sources": getattr(error, "sources", sources),
                    "library_matches": getattr(
                        error, "library_matches", library_matches
                    ),
                    "provider_errors": sorted(catalog.errors),
                }
                status = max(status, 1)
                if args.apply:
                    try:
                        state.route_review(path, report)
                    except (OSError, Review, ValueError) as route_error:
                        report["outcome"] = "error"
                        report["routing_error"] = str(route_error)
                        status = 2
                reports.append(report)
    return reports, status


def render(reports: list[dict], apply: bool) -> None:
    if not reports:
        print("No release folders in staging.")
    for row in reports:
        print(f"{row['source']}")
        if row["outcome"] != "completed":
            print(f"  {row['outcome']}: {row['reason']}")
            for source in row["sources"]:
                print(f"  Candidate: {source}")
        else:
            print(f"  {'Completed' if apply else 'Would complete'}: {row['folder']}")
            print(f"  Evidence: {row['evidence']}")
            for item in row["tracks"]:
                fields = ", ".join(sorted(item["fields"]))
                print(
                    f"  {item['old']} -> {item['new']}"
                    + (f"  [{fields}]" if fields else "")
                )
            for source in row["sources"]:
                print(f"  Source: {source}")
            for note in row["notes"]:
                print(f"  {note}")
        for name in row["library_matches"]:
            print(f"  Library: {name}")
        for error in row.get("provider_errors", []):
            print(f"  {error}")
        if row.get("routing_error"):
            print(f"  Routing failed: {row['routing_error']}")
        if row.get("destination"):
            print(f"  Destination: {row['destination']}")
        if row.get("backup"):
            print(f"  Backup: {row['backup']}")
        if not apply and row["outcome"] == "completed":
            print("  Use --apply to write this release.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify, tag, and organize staged FLAC releases."
    )
    parser.add_argument("--config", type=Path, default=Path("taggz.toml"))
    parser.add_argument(
        "--apply", action="store_true", help="apply changes and route releases"
    )
    parser.add_argument(
        "--offline", action="store_true", help="compare with the read-only library only"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="show all planned tag values and evidence as JSON",
    )
    parser.add_argument("--container", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "releases",
        nargs="*",
        help="release folder names directly in staging; default: all",
    )
    args = parser.parse_args()
    try:
        reports, status = run(args)
    except (OSError, KeyError, TypeError, ValueError) as error:
        print(f"taggz: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(
            json.dumps(
                {"mode": "apply" if args.apply else "preview", "releases": reports},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        render(reports, args.apply)
    return status
