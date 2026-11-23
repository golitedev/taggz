"""Host-side launcher: bind mounts are derived from the TOML config."""

import argparse
import os
import subprocess
from pathlib import Path

from .config import load_config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("taggz.toml"))
    parser.add_argument("--image", default="taggz:local")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("releases", nargs="*")
    args = parser.parse_args()
    try:
        config_file = args.config.expanduser().resolve(strict=True)
        config = load_config(config_file)
        if any(
            "," in str(path) for path in (config_file, config.staging, config.library)
        ):
            raise ValueError("Docker bind paths cannot contain commas")
        command = [
            "docker",
            "run",
            "--rm",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--mount",
            f"type=bind,src={config_file},dst=/config.toml,readonly",
            "--mount",
            f"type=bind,src={config.library},dst=/library,readonly,bind-propagation=rprivate,bind-recursive=readonly",
            "--mount",
            f"type=bind,src={config.staging},dst=/staging,bind-propagation=rprivate,bind-recursive=disabled"
            + ("" if args.apply else ",readonly"),
        ]
        if args.offline:
            command.extend(["--network", "none"])
        if (
            config.discogs
            and not args.offline
            and config.discogs_token_env in os.environ
        ):
            command.extend(["--env", config.discogs_token_env])
        command.extend([args.image, "--config", "/config.toml", "--container"])
        for flag in ("apply", "offline", "json"):
            if getattr(args, flag):
                command.append(f"--{flag}")
        return subprocess.call([*command, "--", *args.releases])
    except (OSError, KeyError, TypeError, ValueError) as error:
        parser.exit(2, f"taggz-docker: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
