"""Command line helpers: ``python -m app.cli ingest`` fetches every feed once."""

from __future__ import annotations

import asyncio
import sys

from .catalog import get_catalog
from .config import settings
from .db import Database
from .feeds import ingest_all


def main(argv: list[str]) -> int:
    if argv[:1] != ["ingest"]:
        print("Uso: python -m app.cli ingest")
        return 2
    db = Database(settings.db_path)
    added = asyncio.run(ingest_all(get_catalog(), db))
    stats = db.stats()
    print(f"{added} titulares nuevos · {stats['articles']} en total de {stats['media']} medios")
    for f in sorted(stats["feeds"], key=lambda f: f["media_id"]):
        print(f"  {f['media_id']:<24} {f['last_status']:<14} {f['last_items']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
