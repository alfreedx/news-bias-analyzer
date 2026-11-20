"""Encrypted, author-free snapshot of the headline index (restores the demo after Render wipes its disk).

File format (current, "chunked"): the magic ``CTSNAP2`` followed by gzip(SQLite file) cut into 4 MiB pieces, each
one its own Fernet token (cryptography), written as ``<4-byte big-endian length><token>`` and closed by a zero
length. Neither building nor restoring ever holds the whole file in memory. The previous format (one Fernet token
around the whole gzip) is still read. The key (SNAPSHOT_KEY) never leaves the
environment and is never logged. Everything here is written so that a failure leaves the existing database
untouched: the snapshot is unpacked into a temporary file next to the target, checked, and only then moved
into place with an atomic ``os.replace``.
"""

from __future__ import annotations

import contextlib
import gzip
import io
import logging
import os
import sqlite3
import struct
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
MAGIC = b"CTSNAP2\n"  # a legacy token starts with "gAAAAA" (Fernet version byte): the two never collide
CHUNK = 4 * 1024 * 1024  # plaintext bytes per Fernet token
MAX_TOKEN = 2 * CHUNK  # a longer block is not one of ours: refused before it is read
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


class _ChunkWriter:
    """File-like sink that encrypts everything written to it in `chunk`-sized, length-prefixed Fernet tokens."""

    def __init__(self, out, fernet: Fernet, chunk: int = CHUNK):
        self.out, self.fernet, self.chunk = out, fernet, chunk
        self.buf = bytearray()
        out.write(MAGIC)
        self.written = len(MAGIC)

    def write(self, data) -> int:
        self.buf += data
        while len(self.buf) >= self.chunk:
            self._emit(bytes(self.buf[: self.chunk]))
            del self.buf[: self.chunk]
        return len(data)

    def flush(self) -> None:  # GzipFile calls it; pieces are only cut at chunk boundaries
        pass

    def _emit(self, plain: bytes) -> None:
        token = self.fernet.encrypt(plain)
        self.out.write(struct.pack(">I", len(token)))
        self.out.write(token)
        self.written += 4 + len(token)

    def finish(self) -> int:
        if self.buf:
            self._emit(bytes(self.buf))
            self.buf.clear()
        self.out.write(struct.pack(">I", 0))
        return self.written + 4


def pack_to(db_file: Path, out, key: str | bytes, chunk: int = CHUNK) -> int:
    """gzip the SQLite file and write it encrypted, in chunks, to the binary file object ``out``.
    Streams: memory stays around one chunk. Returns the number of bytes written."""
    sink = _ChunkWriter(out, _fernet(key), chunk)
    with open(db_file, "rb") as src, gzip.GzipFile(fileobj=sink, mode="wb", compresslevel=9, mtime=0) as gz:
        while block := src.read(1024 * 1024):
            gz.write(block)
    return sink.finish()


def pack(db_file: Path, key: str | bytes) -> bytes:
    """Same as `pack_to`, returning the bytes (small databases and tests)."""
    buf = io.BytesIO()
    pack_to(db_file, buf, key)
    return buf.getvalue()


def _read_exact(fh, n: int) -> bytes:
    data = fh.read(n)
    if len(data) != n:
        raise SnapshotError("snapshot is truncated")
    return data


def _decrypted_pieces(fh, fernet: Fernet, max_blob: int):
    """Yield the gzip bytes of a snapshot read from ``fh`` (either format), one block at a time."""
    head = fh.read(len(MAGIC))
    if head != MAGIC:  # legacy: one Fernet token around the whole gzip
        rest = fh.read(max_blob + 1)
        if len(head) + len(rest) > max_blob:
            raise SnapshotError("snapshot is larger than the allowed size")
        try:
            yield fernet.decrypt(head + rest)
        except InvalidToken as exc:
            raise SnapshotError("cannot decrypt the snapshot (wrong key or corrupted file)") from exc
        return
    while True:
        (size,) = struct.unpack(">I", _read_exact(fh, 4))
        if size == 0:
            if fh.read(1):
                raise SnapshotError("unexpected data after the end of the snapshot")
            return
        if size > MAX_TOKEN:
            raise SnapshotError("snapshot is corrupted (oversized block)")
        try:
            yield fernet.decrypt(_read_exact(fh, size))
        except InvalidToken as exc:
            raise SnapshotError("cannot decrypt the snapshot (wrong key or corrupted file)") from exc


def unpack_stream(fh, key: str | bytes, dest: Path, max_size: int = MAX_UNPACKED,
                  max_blob: int = MAX_DOWNLOAD) -> int:
    """Decrypt and gunzip the snapshot read from the binary file object ``fh`` into ``dest``, refusing to write
    more than ``max_size`` bytes. Reads the chunked and the legacy format."""
    dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
    written = 0
    try:
        with open(dest, "wb") as out:
            for piece in _decrypted_pieces(fh, _fernet(key), max_blob):
                data = piece
                while data:
                    if dec.eof:
                        raise SnapshotError("snapshot has data after the end of the gzip stream")
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


def unpack(blob: bytes, key: str | bytes, dest: Path, max_size: int = MAX_UNPACKED) -> int:
    """Decrypt and gunzip ``blob`` into ``dest`` refusing to write more than ``max_size`` bytes."""
    return unpack_stream(io.BytesIO(blob), key, dest, max_size, max_blob=max(len(blob), 1))


def unpack_file(path: Path, key: str | bytes, dest: Path, max_size: int = MAX_UNPACKED,
                max_blob: int = MAX_DOWNLOAD) -> int:
    """Like `unpack`, reading the snapshot from a file instead of memory."""
    with open(path, "rb") as fh:
        return unpack_stream(fh, key, dest, max_size, max_blob)


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
        out_file = Path(out_file)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        with open(out_file, "wb") as fh:
            size = pack_to(copy2, fh, key)
    return {**counts, "bytes": size}


# --- download ----------------------------------------------------------------------------
def _fetch(url: str, sink, *, max_bytes: int, timeout: float, deadline: float | None,
           transport: httpx.BaseTransport | None) -> int:
    """GET ``url`` following at most MAX_REDIRECTS redirects, only to GitHub release hosts, over https; every
    piece of the body goes to ``sink(bytes)``. Returns the size."""
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
                size = 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise SnapshotError("snapshot is larger than the allowed size")
                    sink(chunk)
                    if deadline is not None and time.monotonic() > deadline:
                        raise SnapshotError("download timed out")
                return size
    raise SnapshotError("too many redirects")


def download(url: str, *, max_bytes: int = MAX_DOWNLOAD, timeout: float = TIMEOUT,
             deadline: float | None = None, transport: httpx.BaseTransport | None = None) -> bytes:
    """Download into memory (small payloads and tests); restores use `download_to`."""
    buf = bytearray()
    _fetch(url, buf.extend, max_bytes=max_bytes, timeout=timeout, deadline=deadline, transport=transport)
    return bytes(buf)


def download_to(url: str, dest: Path, *, max_bytes: int = MAX_DOWNLOAD, timeout: float = TIMEOUT,
                deadline: float | None = None, transport: httpx.BaseTransport | None = None) -> int:
    """Download straight into the file ``dest`` (nothing is kept in memory)."""
    with open(dest, "wb") as fh:
        return _fetch(url, fh.write, max_bytes=max_bytes, timeout=timeout, deadline=deadline, transport=transport)


# --- restore -----------------------------------------------------------------------------
def restore(url: str, key: str | bytes, dest: Path, *, deadline: float | None = None,
            transport: httpx.BaseTransport | None = None, max_unpacked: int = MAX_UNPACKED,
            max_download: int = MAX_DOWNLOAD) -> dict[str, int]:
    """Download, decrypt, verify and atomically move the snapshot to ``dest``. Raises SnapshotError.

    The download goes to a temporary file and is decrypted block by block, so the snapshot is never held twice
    in memory. ``dest`` is only replaced after every check passed; on any failure it is left exactly as it was.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".download-", suffix=".enc", dir=dest.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        download_to(url, tmp, max_bytes=max_download, deadline=deadline, transport=transport)
        return install_file(tmp, key, dest, deadline=deadline, max_unpacked=max_unpacked, max_blob=max_download)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def install(blob: bytes, key: str | bytes, dest: Path, *, deadline: float | None = None,
            max_unpacked: int = MAX_UNPACKED) -> dict[str, int]:
    """Decrypt ``blob`` into a temporary file beside ``dest``, verify it and atomically replace ``dest``."""
    return _install(lambda tmp: unpack(blob, key, tmp, max_size=max_unpacked), dest, deadline)


def install_file(path: Path, key: str | bytes, dest: Path, *, deadline: float | None = None,
                 max_unpacked: int = MAX_UNPACKED, max_blob: int = MAX_DOWNLOAD) -> dict[str, int]:
    """Like `install`, streaming the encrypted snapshot from ``path`` (never loaded whole)."""
    return _install(lambda tmp: unpack_file(path, key, tmp, max_size=max_unpacked, max_blob=max_blob), dest, deadline)


def _install(write_plain, dest: Path, deadline: float | None) -> dict[str, int]:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".restore-", suffix=".sqlite3", dir=dest.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        write_plain(tmp)
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
