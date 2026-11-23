import argparse
import json
import os
import shutil
import subprocess
import sys
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path

from mutagen import MutagenError

from .catalog import Catalog
from .config import load_config
from .match import propose
from .release import Release, Review, read_release
from .sidecars import plan_sidecars
from .storage import RESERVED, Staging, replaygain_skip_reason, staging_lock, vacant


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


def input_evidence(release: Release) -> dict:
    return {
        "artist": release.artist,
        "title": release.title,
        "date": release.date,
        "medium": release.medium,
        "barcode": release.barcode,
        "catalog_number": release.catalog,
        "track_count": len(release.tracks),
        "disc_count": release.tracks[-1].disc,
        "audio": [
            {"bit_depth": bits, "sample_rate_hz": rate}
            for bits, rate in sorted({(t.bits, t.rate) for t in release.tracks})
        ],
        "rip_logs": release.rip_logs,
        "cue_sheets": sorted(
            str(p) for p in release.files if p.suffix.casefold() == ".cue"
        ),
        "cue_isrc_count": len(release.cue_isrcs),
        "cue_notes": release.cue_notes,
        "disc_ids": [{"disc": toc.disc, "id": toc.id} for toc in release.tocs],
    }


def run(args) -> tuple[list[dict], int]:
    cfg = load_config(args.config, container=args.container)
    if args.apply and args.offline:
        raise ValueError("--apply requires online catalog lookups")
    if args.apply and cfg.replaygain and not shutil.which("metaflac"):
        raise ValueError("metaflac is required when ReplayGain is enabled")
    catalog = Catalog(cfg, offline=args.offline)
    state = Staging(cfg.staging)
    discogs_mode = (
        "barcode, catalog and linked-release lookup"
        if os.environ.get(cfg.discogs_token_env)
        else "linked-release lookup only"
    )
    providers = {
        name: "offline" if args.offline else mode if enabled else "disabled"
        for name, enabled, mode in (
            (
                "Qobuz",
                cfg.qobuz,
                "album metadata lookup"
                if catalog.qobuz_client.configured
                else "credentials not configured",
            ),
            ("MusicBrainz", cfg.musicbrainz, "release and CD disc ID lookup"),
            ("Discogs", cfg.discogs, discogs_mode),
        )
    }
    reports = []
    status = 0
    with staging_lock(cfg.staging) if args.apply else nullcontext():
        if args.apply:
            state.setup()
        paths = release_paths(cfg.staging, args.releases)
        for number, path in enumerate(paths, 1):
            catalog.errors.clear()
            if not args.json:
                print(
                    f"[{number}/{len(paths)}] Inspecting {path.name}",
                    file=sys.stderr,
                    flush=True,
                )
            report = {
                "source": path.name,
                "phase": "read staged files",
                "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "candidates": [],
                "sources": [],
                "providers": providers,
            }
            try:
                release = read_release(path)
                report["input"] = input_evidence(release)
                report["phase"] = "match release"
                if args.offline:
                    raise Review("online catalog lookups disabled")
                proposal = propose(release, catalog, report)
                report.update(proposal.report())
                report["phase"] = "plan sidecars"
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
                skip = replaygain_skip_reason(release) if cfg.replaygain else ""
                report["replaygain"] = cfg.replaygain and not skip
                if skip:
                    report["replaygain_skip_reason"] = skip
                report["provider_errors"] = sorted(catalog.errors)
                report["phase"] = "prepare completed destination"
                if not vacant(state.completed / proposal.folder):
                    raise Review(
                        f"completed destination already exists: {proposal.folder}"
                    )
                if args.apply:
                    if not args.json:
                        print(
                            f"[{number}/{len(paths)}] Applying verified match: {proposal.folder}",
                            file=sys.stderr,
                            flush=True,
                        )
                    state.apply(
                        proposal, sidecars, report, replaygain=report["replaygain"]
                    )
                else:
                    report["phase"] = "preview ready"
                reports.append(report)
            except (OSError, MutagenError, Review, subprocess.TimeoutExpired) as error:
                for key in ("destination", "backup", "report_file"):
                    report.pop(key, None)
                report.update(
                    {
                        "outcome": "needs-review",
                        "reason": str(error),
                        "provider_errors": sorted(catalog.errors),
                    }
                )
                status = max(status, 1)
                if args.apply:
                    try:
                        state.route_review(path, report)
                    except (OSError, Review, ValueError) as route_error:
                        report.pop("destination", None)
                        report.pop("report_file", None)
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
        if item := row.get("input"):
            formats = ", ".join(
                f"{a['bit_depth']}-bit/{a['sample_rate_hz'] / 1000:g} kHz"
                for a in item["audio"]
            )
            print(
                f"  Input: {item['medium'] or 'unknown medium'}, {item['track_count']} tracks "
                f"on {item['disc_count']} disc(s), {formats}"
            )
            for log, evidence in item["rip_logs"].items():
                print(f"  Rip log: {log}: {evidence}")
            if item["cue_sheets"]:
                print(
                    f"  Cue sheets: {', '.join(item['cue_sheets'])} ({item['cue_isrc_count']} ISRCs)"
                )
            for sheet, reason in item["cue_notes"].items():
                print(f"  Cue sheet {sheet}: {reason}")
            for key, name in (
                ("barcode", "Barcode"),
                ("catalog_number", "Catalog number"),
            ):
                if item[key]:
                    print(f"  {name}: {item[key]}")
        if row["outcome"] != "completed":
            print(f"  {row['outcome']} during {row['phase']}: {row['reason']}")
            if row["phase"] == "match release" and not row["candidates"]:
                print("  No comparable candidates returned.")
        else:
            print(f"  {'Completed' if apply else 'Would complete'}: {row['folder']}")
            print(f"  Evidence: {row['evidence']}")
            for item in row["tracks"]:
                fields = ", ".join(sorted(item["fields"]))
                print(
                    f"  {item['old']} -> {item['new']}"
                    + (f"  [{fields}]" if fields else "")
                )
                if item["withheld"]:
                    print(
                        f"    Conflicting tags left unset: {', '.join(item['withheld'])}"
                    )
            for note in row["notes"]:
                print(f"  {note}")
            for item in row["sidecars"]:
                action = "rewrote" if apply else "would rewrite"
                if not item["rewritten"]:
                    action = "renamed" if apply else "would rename"
                print(f"  Sidecar ({action}): {item['old']} -> {item['new']}")
            if verified := row.get("verification"):
                print(
                    f"  Verified {verified['audio_frame_hashes_matched']} audio frame hashes, "
                    f"tags, artwork and {verified['sidecars_verified']} sidecars."
                )
            if row["replaygain"]:
                print(f"  ReplayGain: {'calculated' if apply else 'will calculate'}")
            elif reason := row.get("replaygain_skip_reason"):
                print(f"  ReplayGain skipped: {reason}")
        for candidate in row["candidates"]:
            print(
                f"  Candidate {candidate['decision']}: {candidate['artist']} — {candidate['title']} "
                f"({candidate['date'] or 'date unknown'}, {candidate['medium'] or 'unknown medium'}, "
                f"{candidate['track_count']} tracks)"
            )
            print(f"    {candidate['source']}")
            print(f"    {candidate['reason']}")
            if edition_reason := candidate.get("edition_reason"):
                print(f"    Edition check: {edition_reason}")
            if candidate["exact_cd_toc"]:
                print("    Exact CD disc ID match")
            facts = []
            if candidate["compared_durations"]:
                facts.append(
                    f"{candidate['compared_durations']} durations compared "
                    f"(largest difference {candidate['maximum_duration_difference_seconds']:.3f}s)"
                )
                facts.append(f"{candidate['matching_isrcs']} matching ISRCs")
            if facts:
                print("    " + ", ".join(facts))
        if held := row.get("withheld_release_fields"):
            print(f"  Unverified edition tags left unset: {', '.join(held)}")
        print(
            "  Lookups: "
            + ", ".join(f"{name} ({mode})" for name, mode in row["providers"].items())
        )
        for error in row.get("provider_errors", []):
            print(f"  {error}")
        if row.get("routing_error"):
            print(f"  Routing failed: {row['routing_error']}")
        if row.get("destination"):
            print(f"  Destination: {row['destination']}")
        if row.get("backup"):
            print(f"  Backup: {row['backup']}")
        if row.get("report_file"):
            print(f"  Report: {row['report_file']}")
        if not apply and row["outcome"] == "completed":
            print("  Use --apply to write this release.")
    if reports:
        counts = {
            key: sum(row["outcome"] == key for row in reports)
            for key in ("completed", "needs-review", "error")
        }
        completed = "completed" if apply else "ready to apply"
        print(
            f"{len(reports)} releases: {counts['completed']} {completed}, "
            f"{counts['needs-review']} need review, {counts['error']} errors"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify, tag, and organize staged FLAC releases."
    )
    parser.add_argument("--config", type=Path, default=Path("taggz.toml"))
    parser.add_argument(
        "--apply", action="store_true", help="apply changes and route releases"
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="inspect local files without catalog lookups",
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
