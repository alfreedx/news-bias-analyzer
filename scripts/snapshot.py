"""Encrypted, author-free snapshot of the headline index (see app/snapshot.py and SECURITY.md).

  python scripts/snapshot.py build --out FILE [--from FILE]
      Decrypts the previous snapshot (if any), runs one ingestion cycle + story assignment + retention purge
      on it, and writes the new encrypted snapshot (authors removed). Needs SNAPSHOT_KEY in the environment.
  python scripts/snapshot.py restore --url URL --to PATH
      Downloads (URL may also be a local file), decrypts and verifies a snapshot and moves it
      into place; on any failure PATH is untouched.

The key is read only from the SNAPSHOT_KEY environment variable and is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import snapshot  # noqa: E402
from app.catalog import get_catalog  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.feeds import ingest_all  # noqa: E402
from app.stories import assign_stories  # noqa: E402

log = logging.getLogger("snapshot")


def _key() -> str:
    key = os.environ.get("SNAPSHOT_KEY", "").strip()
    if not key:
        print("SNAPSHOT_KEY no está definida", file=sys.stderr)
        raise SystemExit(2)
    return key


def build(out: Path, previous: Path | None) -> int:
    key = _key()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="contraste-build-") as tmp:
        work = Path(tmp) / "news.sqlite3"
        if previous is not None and previous.is_file():
            try:
                snapshot.unpack_file(previous, key, work)
                snapshot.verify_db(work)
            except snapshot.SnapshotError as exc:
                # Never replace good history with a fresh near-empty index because of a wrong key.
                print(f"No se puede abrir la instantánea anterior: {exc}", file=sys.stderr)
                return 1
            print(f"Instantánea anterior: {snapshot.count_articles(work)} titulares")
        else:
            print("Sin instantánea anterior: se empieza de cero")
        db = Database(work)
        added = asyncio.run(ingest_all(get_catalog(), db))  # also applies the retention purge
        stats = assign_stories(db, get_catalog())
        purged = db.purge_older_than(settings.retention_days)
        print(f"Ingesta: {added} titulares nuevos, historias {stats}, {purged} purgados")
        try:
            result = snapshot.export_db(work, out, key)
        except snapshot.SnapshotError as exc:
            print(f"No se pudo exportar: {exc}", file=sys.stderr)
            return 1
    print(f"Instantánea: {result['articles']} titulares, {result['stories']} historias, "
          f"{result['bytes'] / 1_000_000:.2f} MB cifrados, {time.monotonic() - started:.1f} s")
    return 0


def restore(url: str, dest: Path) -> int:
    key = _key()
    started = time.monotonic()
    try:
        local = Path(url)
        if not url.startswith(("http://", "https://")) and local.is_file():  # handy for local tests
            counts = snapshot.install_file(local, key, dest)
        else:
            counts = snapshot.restore(url, key, dest, deadline=started + 120)
    except Exception as exc:
        name = str(exc) if isinstance(exc, snapshot.SnapshotError) else type(exc).__name__
        log.warning("restauración fallida (%s); la base existente no se ha tocado", name)
        print(f"Restauración fallida: {name}", file=sys.stderr)
        return 1
    print(f"Restaurado en {dest}: {counts['articles']} titulares, {counts['stories']} historias, "
          f"{time.monotonic() - started:.1f} s")
    return 0


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="build the encrypted snapshot")
    b.add_argument("--out", required=True, type=Path)
    b.add_argument("--from", dest="previous", type=Path, help="previous encrypted snapshot to extend")
    r = sub.add_parser("restore", help="restore a snapshot into a database file")
    r.add_argument("--url", required=True)
    r.add_argument("--to", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "build":
        return build(args.out, args.previous)
    return restore(args.url, args.to)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
