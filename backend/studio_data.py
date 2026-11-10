"""Import an offline knowledge snapshot for Studio; never overwrite a destination."""

import argparse
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="Closed application's data directory")
    parser.add_argument("--destination", type=Path, default=Path(__file__).parent / ".data" / "studio")
    args = parser.parse_args()
    source, destination = args.source.resolve(), args.destination.resolve()
    if not (source / "idol.sqlite3").is_file():
        parser.error("Source must contain idol.sqlite3")
    if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        parser.error("Source and destination must be separate directories")
    if destination.exists():
        parser.error("Destination already exists; use a new --destination and set IDOL_STUDIO_DATA_DIR")
    destination.mkdir(parents=True)
    # backup() also captures SQLite WAL contents. Close Idol first for vector consistency.
    with (
        closing(sqlite3.connect((source / "idol.sqlite3").as_uri() + "?mode=ro", uri=True)) as src,
        closing(sqlite3.connect(destination / "idol.sqlite3")) as dst,
    ):
        src.backup(dst)
    for name in ("vectors", "models"):
        if (source / name).is_dir():
            shutil.copytree(source / name, destination / name)
    print(f"Studio snapshot created: {destination}")
    print("API Key and Agent Server checkpoints are not copied. Configure IDOL_API_KEY separately.")


if __name__ == "__main__":
    main()
