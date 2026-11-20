"""Encrypted, author-free snapshot of the headline index (restores the demo after Render wipes its disk).

File format: gzip(SQLite file), then Fernet-encrypted (cryptography). The key (SNAPSHOT_KEY) never leaves the
environment and is never logged. Everything here is written so that a failure leaves the existing database
untouched: the snapshot is unpacked into a temporary file next to the target, checked, and only then moved
into place with an atomic ``os.replace``.
"""

from __future__ import annotations

import contextlib
import gzip
import logging
import os
import sqlite3
import tempfile
import time
import zlib
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger("newsbias.snapshot")

SNAPSHOT_NAME = "contraste-db.sqlite3.gz.enc"
TIMEOUT = 60.0
MAX_DOWNLOAD = 80 * 1024 * 1024
MAX_UNPACKED = 300 * 1024 * 1024
MAX_REDIRECTS = 5
ALLOWED_REDIRECT_HOSTS = frozenset({"github.com", "objects.githubusercontent.com",
                                    "release-assets.githubusercontent.com"})
REQUIRED_TABLES = frozenset({"articles", "articles_fts", "feed_state", "stories", "story_articles"})


class SnapshotError(Exception):
    """Any reason a snapshot cannot be built or restored (messages never contain the key)."""


# --- crypto ---------------------------------------------------------------------
def _fernet(key: str | bytes) -> Fernet:
    try:
        return Fernet(key.strip())
    except (ValueError, TypeError) as exc:
        raise SnapshotError("SNAPSHOT_KEY is not a valid Fernet key") from exc


def pack(db_file: Path, key: str | bytes) -> bytes:
    """gzip the SQLite file and encrypt it."""
    return _fernet(key).encrypt(gzip.compress(Path(db_file).read_bytes(), compresslevel=9, mtime=0))


def unpack(blob: bytes, key: str | bytes, dest: Path, max_size: int = MAX_UNPACKED) -> int:
    """Decrypt and gunzip ``blob`` into ``dest`` refusing to write more than ``max_size`` bytes."""
    try:
        gz = _fernet(key).decrypt(blob)
    except InvalidToken as exc:
        raise SnapshotError("cannot decrypt the snapshot (wrong key or corrupted file)") from exc
    dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
    written = 0
    try:
        with open(dest, "wb") as out:
            data = gz
            while data:
                chunk = dec.decompress(data, 1024 * 1024)
                written += len(chunk)
                if written > max_size:
                    raise SnapshotError(f"unpacked snapshot exceeds {max_size} bytes")
                out.write(chunk)
                data = dec.unconsumed_tail
            tail = dec.flush()
            written += len(tail)
            if written > max_size:
                raise SnapshotError(f"unpacked snapshot exceeds {max_size} bytes")
            out.write(tail)
    except zlib.error as exc:
        raise SnapshotError("snapshot is not valid gzip data") from exc
    if not dec.eof:
        raise SnapshotError("snapshot gzip stream is truncated")
    return written


# --- database checks ---------------------------------------------------------------
def verify_db(path: Path) -> dict[str, int]:
    """Integrity check plus required tables; returns {'articles': n, 'stories': n}."""
    try:
        conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=15)
    except sqlite3.Error as exc:
        raise SnapshotError(f"not a SQLite database: {exc}") from exc
    try:
        try:
            if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise SnapshotError("integrity_check failed")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            missing = REQUIRED_TABLES - tables
            if missing:
                raise SnapshotError(f"snapshot lacks tables: {', '.join(sorted(missing))}")
            return {"articles": conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
                    "stories": conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0]}
        except sqlite3.Error as exc:
            raise SnapshotError(f"database check failed: {exc}") from exc
    finally:
        conn.close()


def count_articles(path: Path) -> int:
    """Articles in a local database (0 when it does not exist or cannot be read)."""
    if not Path(path).is_file():
        return 0
    try:
        conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return 0
    try:
        return conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


# --- export ----------------------------------------------------------------------------
def export_db(src: Path, out_file: Path, key: str | bytes) -> dict[str, int]:
    """Write the encrypted, author-free snapshot of ``src`` to ``out_file``. Returns counts and size."""
    _fernet(key)  # fail early on a bad key
    with tempfile.TemporaryDirectory(prefix="contraste-snap-") as tmp:
        copy1, copy2 = Path(tmp) / "copy1.sqlite3", Path(tmp) / "copy2.sqlite3"
        conn = sqlite3.connect(src, timeout=30)
        try:
            conn.execute("VACUUM INTO ?", (str(copy1),))
        finally:
            conn.close()
        conn = sqlite3.connect(copy1)
        try:
            conn.execute("UPDATE articles SET author = NULL WHERE author IS NOT NULL")
            conn.commit()
            # second VACUUM so the removed names do not linger in free pages
            conn.execute("VACUUM INTO ?", (str(copy2),))
        finally:
            conn.close()
        counts = verify_db(copy2)
        blob = pack(copy2, key)
    out_file = Path(out_file)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_bytes(blob)
    return {**counts, "bytes": len(blob)}


# --- download ----------------------------------------------------------------------------
def download(url: str, *, max_bytes: int = MAX_DOWNLOAD, timeout: float = TIMEOUT,
             deadline: float | None = None, transport: httpx.BaseTransport | None = None) -> bytes:
    """GET ``url`` following at most MAX_REDIRECTS redirects, only to GitHub release hosts, over https."""
    current = url
    with httpx.Client(timeout=timeout, follow_redirects=False, transport=transport,
                      headers={"User-Agent": "contraste-snapshot/1.0"}) as client:
        for hop in range(MAX_REDIRECTS + 1):
            parts = urlsplit(current)
            if parts.scheme != "https" or not parts.hostname:
                raise SnapshotError("snapshot URL must be https")
            if hop > 0 and parts.hostname.lower() not in ALLOWED_REDIRECT_HOSTS:
                raise SnapshotError(f"redirect to a host that is not allowed: {parts.hostname}")
            with client.stream("GET", current) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        raise SnapshotError("redirect without Location")
                    current = urljoin(current, location)
                    continue
                if resp.status_code != 200:
                    raise SnapshotError(f"download failed with HTTP {resp.status_code}")
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise SnapshotError("snapshot is larger than the allowed size")
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        raise SnapshotError("snapshot is larger than the allowed size")
                    if deadline is not None and time.monotonic() > deadline:
                        raise SnapshotError("download timed out")
                return bytes(buf)
    raise SnapshotError("too many redirects")


# --- restore -----------------------------------------------------------------------------
def restore(url: str, key: str | bytes, dest: Path, *, deadline: float | None = None,
            transport: httpx.BaseTransport | None = None, max_unpacked: int = MAX_UNPACKED,
            max_download: int = MAX_DOWNLOAD) -> dict[str, int]:
    """Download, decrypt, verify and atomically move the snapshot to ``dest``. Raises SnapshotError.

    ``dest`` is only replaced after every check passed; on any failure it is left exactly as it was.
    """
    blob = download(url, max_bytes=max_download, deadline=deadline, transport=transport)
    return install(blob, key, dest, deadline=deadline, max_unpacked=max_unpacked)


def install(blob: bytes, key: str | bytes, dest: Path, *, deadline: float | None = None,
            max_unpacked: int = MAX_UNPACKED) -> dict[str, int]:
    """Decrypt ``blob`` into a temporary file beside ``dest``, verify it and atomically replace ``dest``."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".restore-", suffix=".sqlite3", dir=dest.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        unpack(blob, key, tmp, max_size=max_unpacked)
        counts = verify_db(tmp)
        if deadline is not None and time.monotonic() > deadline:
            raise SnapshotError("restore timed out before it could be applied")
        for suffix in ("-wal", "-shm"):  # stale WAL files belong to the database being replaced
            with contextlib.suppress(FileNotFoundError):
                Path(f"{dest}{suffix}").unlink()
        os.replace(tmp, dest)
        return counts
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def restore_if_needed(url: str, key: str, dest: Path, *, min_articles: int = 2000,
                      total_timeout: float = 90.0, transport: httpx.BaseTransport | None = None) -> bool:
    """Startup hook: restore only when a key is set and the local index is small. Never raises."""
    if not url or not key:
        return False
    have = count_articles(dest)
    if have >= min_articles:
        log.info("snapshot restore skipped: %d articles already stored", have)
        return False
    try:
        counts = restore(url, key, dest, deadline=time.monotonic() + total_timeout, transport=transport)
    except SnapshotError as exc:
        log.warning("snapshot restore failed (%s); continuing with the local index", exc)
        return False
    except Exception as exc:  # network or disk errors: startup must go on
        log.warning("snapshot restore failed (%s); continuing with the local index", type(exc).__name__)
        return False
    log.info("snapshot restored: %d articles, %d stories", counts["articles"], counts["stories"])
    return True
