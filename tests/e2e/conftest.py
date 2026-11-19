import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.catalog import get_catalog  # noqa: E402
from app.db import Database  # noqa: E402
from app.stories import assign_stories  # noqa: E402

MEDIA = ["el-pais", "abc", "okdiario", "eldiario-es", "maldita"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _seed(data_dir: Path) -> None:
    now = int(time.time())
    rows = []
    for i, mid in enumerate(MEDIA):
        for j in range(2):
            rows.append(
                (
                    f"https://example.com/{mid}/{j}",
                    mid,
                    # abc and okdiario carry the same headline: they must collapse into one row
                    "El Gobierno aprueba una nueva ley de vivienda y alquiler"
                    if mid in ("abc", "okdiario") and j == 0
                    else f"El Gobierno aprueba una nueva ley de vivienda y alquiler, según {mid} {j}",
                    "Medidas sobre la vivienda, el alquiler y los precios en las grandes ciudades. Afecta a 12,5 millones de personas.",
                    now - 3600 * (i * 2 + j + 1),
                    "Ana Pérez" if mid == "el-pais" and j == 0 else None,
                )
            )
    # a piece the outlet itself marks as branded (URL section): it must show the badge and sit apart
    rows.append((
        "https://elpais.com/economia/branded/seguros-hogar/2026-11-18/vivienda-y-alquiler-el-seguro.html",
        "el-pais", "Vivienda y alquiler: el seguro que protege tu hogar y tu bolsillo", "Contenido de una marca.",
        now - 600, None, None))
    db = Database(data_dir / "news.sqlite3")
    db.insert_articles(rows)
    assign_stories(db, get_catalog())


@pytest.fixture(scope="session")
def base_url():
    data_dir = Path(tempfile.mkdtemp(prefix="contraste-e2e-"))
    _seed(data_dir)
    port = _free_port()
    env = {
        **os.environ,
        "INGEST_ENABLED": "false",
        "GOOGLE_NEWS_ENABLED": "false",
        "DATA_DIR": str(data_dir),
        "RATE_LIMIT_PER_MINUTE": "100000",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--no-access-log"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            if proc.poll() is not None:
                raise RuntimeError("uvicorn exited early")
            try:
                with urllib.request.urlopen(url + "/api/salud", timeout=1):
                    break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError("server did not start")
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
