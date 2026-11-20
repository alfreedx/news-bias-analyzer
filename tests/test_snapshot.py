import asyncio
import dataclasses
import logging
import sqlite3
import time

import httpx
import pytest
from cryptography.fernet import Fernet

from app import main, snapshot
from app.db import Database

URL = "https://github.com/o/r/releases/download/data-snapshot/contraste-db.sqlite3.gz.enc"


@pytest.fixture
def key():
    return Fernet.generate_key().decode()


def make_source(path, n=5, author="Ana Pérez"):
    db = Database(path)
    now = int(time.time())
    db.insert_articles([(f"https://x.test/{i}", "m", f"Titular {i}", "resumen", now - i, author, None)
                        for i in range(n)])
    with db.connect() as c:
        c.execute("INSERT INTO stories(id, created, updated, title) VALUES (1, ?, ?, 't')", (now, now))
        c.execute("INSERT INTO story_articles VALUES (1, 1)")
    return db


def serving(blob, status=200):
    return httpx.MockTransport(lambda request: httpx.Response(status, content=blob))


def read(path, sql):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def build_blob(tmp_path, key, **kw):
    make_source(tmp_path / "src.sqlite3", **kw)
    out = tmp_path / "snap.enc"
    snapshot.export_db(tmp_path / "src.sqlite3", out, key)
    return out.read_bytes()


def test_roundtrip_counts_and_no_authors(tmp_path, key):
    blob = build_blob(tmp_path, key)
    assert b"SQLite" not in blob and b"Ana" not in blob  # encrypted
    dest = tmp_path / "restored.sqlite3"
    counts = snapshot.restore(URL, key, dest, transport=serving(blob))
    assert counts == {"articles": 5, "stories": 1}
    assert read(dest, "SELECT COUNT(*), COUNT(author) FROM articles") == [(5, 0)]
    # the source keeps its authors: only the copy is stripped
    assert read(tmp_path / "src.sqlite3", "SELECT COUNT(author) FROM articles") == [(5,)]


def test_author_text_not_left_in_free_pages(tmp_path, key):
    make_source(tmp_path / "src.sqlite3", author="UnicoNombreRaro Zzyzx")
    out = tmp_path / "snap.enc"
    snapshot.export_db(tmp_path / "src.sqlite3", out, key)
    plain = tmp_path / "plain.sqlite3"
    snapshot.unpack(out.read_bytes(), key, plain)
    assert b"UnicoNombreRaro" not in plain.read_bytes()


def test_wrong_key_fails_and_leaves_db_untouched(tmp_path, key):
    blob = build_blob(tmp_path, key)
    dest = tmp_path / "live.sqlite3"
    make_source(dest, n=2)
    before = dest.read_bytes()
    with pytest.raises(snapshot.SnapshotError):
        snapshot.restore(URL, Fernet.generate_key().decode(), dest, transport=serving(blob))
    assert dest.read_bytes() == before
    assert not list(tmp_path.glob(".restore-*"))


def test_invalid_key_is_reported_without_echoing_it(tmp_path):
    with pytest.raises(snapshot.SnapshotError) as exc:
        snapshot.export_db(tmp_path / "none", tmp_path / "o", "not-a-key-SECRETVALUE")
    assert "SECRETVALUE" not in str(exc.value)


def test_corrupted_data_fails(tmp_path, key):
    blob = build_blob(tmp_path, key)
    dest = tmp_path / "live.sqlite3"
    make_source(dest, n=2)
    before = dest.read_bytes()
    for bad in (blob[:-20], b"garbage", blob[:50] + b"X" + blob[51:]):
        with pytest.raises(snapshot.SnapshotError):
            snapshot.restore(URL, key, dest, transport=serving(bad))
    assert dest.read_bytes() == before


def test_valid_encryption_but_not_a_database(tmp_path, key):
    import gzip
    junk = Fernet(key).encrypt(gzip.compress(b"this is not sqlite" * 100))
    dest = tmp_path / "live.sqlite3"
    with pytest.raises(snapshot.SnapshotError):
        snapshot.restore(URL, key, dest, transport=serving(junk))
    assert not dest.exists()


def test_missing_tables_rejected(tmp_path, key):
    other = tmp_path / "other.sqlite3"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE articles (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    with pytest.raises(snapshot.SnapshotError, match="lacks tables"):
        snapshot.verify_db(other)


def test_integrity_failure_rejected(tmp_path, key):
    src = tmp_path / "src.sqlite3"
    make_source(src, n=300)
    data = bytearray(src.read_bytes())
    for i in range(8192, min(len(data), 8192 * 3)):  # wreck some b-tree pages
        data[i] = 0xFF
    bad = tmp_path / "bad.sqlite3"
    bad.write_bytes(bytes(data))
    with pytest.raises(snapshot.SnapshotError):
        snapshot.verify_db(bad)


def test_unpacked_size_cap(tmp_path, key):
    import gzip
    bomb = Fernet(key).encrypt(gzip.compress(b"\0" * 5_000_000))
    with pytest.raises(snapshot.SnapshotError, match="exceeds"):
        snapshot.unpack(bomb, key, tmp_path / "o", max_size=1_000_000)


def test_download_size_cap(tmp_path):
    with pytest.raises(snapshot.SnapshotError, match="larger"):
        snapshot.download(URL, max_bytes=100, transport=serving(b"x" * 500))
    # also when the server does not declare the length
    def handler(request):
        return httpx.Response(200, stream=httpx.ByteStream(b"x" * 500))
    with pytest.raises(snapshot.SnapshotError, match="larger"):
        snapshot.download(URL, max_bytes=100, transport=httpx.MockTransport(handler))


def test_http_error_and_http_scheme(tmp_path):
    with pytest.raises(snapshot.SnapshotError, match="HTTP 404"):
        snapshot.download(URL, transport=serving(b"", 404))
    with pytest.raises(snapshot.SnapshotError, match="https"):
        snapshot.download("http://github.com/x", transport=serving(b"x"))


@pytest.mark.parametrize("host,ok", [
    ("objects.githubusercontent.com", True),
    ("release-assets.githubusercontent.com", True),
    ("github.com", True),
    ("evil.example", False),
    ("github.com.evil.example", False),
    ("githubusercontent.com", False),
])
def test_redirect_host_allowlist(host, ok):
    def handler(request):
        if request.url.host == "github.com" and request.url.path.endswith(".enc"):
            return httpx.Response(302, headers={"location": f"https://{host}/asset"})
        return httpx.Response(200, content=b"payload")
    transport = httpx.MockTransport(handler)
    if ok:
        assert snapshot.download(URL, transport=transport) == b"payload"
    else:
        with pytest.raises(snapshot.SnapshotError, match="not allowed"):
            snapshot.download(URL, transport=transport)


def test_redirect_loop_is_bounded():
    def handler(request):
        return httpx.Response(302, headers={"location": URL})
    with pytest.raises(snapshot.SnapshotError, match="too many"):
        snapshot.download(URL, transport=httpx.MockTransport(handler))


def test_restore_if_needed_conditions(tmp_path, key):
    blob = build_blob(tmp_path, key, n=5)
    dest = tmp_path / "live.sqlite3"
    # no key: nothing happens (and nothing is requested)
    def boom(request):
        raise AssertionError("must not download")
    assert snapshot.restore_if_needed(URL, "", dest, transport=httpx.MockTransport(boom)) is False
    # big local index: skipped
    make_source(dest, n=5)
    assert snapshot.restore_if_needed(URL, key, dest, min_articles=3, transport=httpx.MockTransport(boom)) is False
    # small local index: restored
    assert snapshot.restore_if_needed(URL, key, dest, min_articles=100, transport=serving(blob)) is True
    # no database at all: restored too
    fresh = tmp_path / "fresh.sqlite3"
    assert snapshot.restore_if_needed(URL, key, fresh, transport=serving(blob)) is True
    assert read(fresh, "SELECT COUNT(*) FROM articles") == [(5,)]


def test_restore_if_needed_never_raises_and_never_logs_key(tmp_path, key, caplog):
    dest = tmp_path / "live.sqlite3"
    make_source(dest, n=2)
    before = dest.read_bytes()
    def broken(request):
        raise httpx.ConnectError("boom")
    with caplog.at_level(logging.DEBUG):
        assert snapshot.restore_if_needed(URL, key, dest, transport=httpx.MockTransport(broken)) is False
        assert snapshot.restore_if_needed(URL, key, dest, transport=serving(b"junk")) is False
        wrong = Fernet.generate_key().decode()
        assert snapshot.restore_if_needed(URL, wrong, dest, transport=serving(build_blob(tmp_path, key))) is False
    assert dest.read_bytes() == before
    assert key not in caplog.text and wrong not in caplog.text
    assert "snapshot restore failed" in caplog.text


def test_startup_restore_only_with_key(monkeypatch, tmp_path, caplog):
    calls = []
    monkeypatch.setattr(main.snapshot, "restore_if_needed", lambda *a, **k: calls.append(a) or True)
    base = main.settings
    monkeypatch.setattr(main, "settings", dataclasses.replace(base, snapshot_key="", snapshot_url=URL))
    assert asyncio.run(main.restore_snapshot()) is False and calls == []
    secret = Fernet.generate_key().decode()
    monkeypatch.setattr(main, "settings", dataclasses.replace(base, snapshot_key=secret, snapshot_url=URL,
                                                              data_dir=tmp_path))
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(main.restore_snapshot()) is True
    assert len(calls) == 1 and calls[0][0] == URL
    assert secret not in caplog.text


def test_startup_survives_snapshot_crash(monkeypatch, tmp_path):
    def crash(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(main.snapshot, "restore_if_needed", crash)
    monkeypatch.setattr(main, "settings", dataclasses.replace(
        main.settings, snapshot_key=Fernet.generate_key().decode(), snapshot_url=URL, data_dir=tmp_path))
    assert asyncio.run(main.restore_snapshot()) is False


def test_default_snapshot_url_derives_from_repo_url():
    from app.config import Settings
    s = Settings()
    assert s.snapshot_url.endswith("/releases/download/data-snapshot/contraste-db.sqlite3.gz.enc")
    assert s.snapshot_url.startswith(s.repo_url)
    assert "SECRETX" not in repr(dataclasses.replace(s, snapshot_key="SECRETX"))


# --- chunked format ---------------------------------------------------------------------
def legacy_blob(tmp_path, key):
    """The old format: one Fernet token around the whole gzip."""
    import gzip
    make_source(tmp_path / "old-src.sqlite3")
    raw = (tmp_path / "old-src.sqlite3").read_bytes()
    return Fernet(key).encrypt(gzip.compress(raw, mtime=0))


def test_new_format_is_chunked_and_magic_tagged(tmp_path, key):
    blob = build_blob(tmp_path, key)
    assert blob.startswith(snapshot.MAGIC)
    assert not blob.startswith(b"gAAAAA")


def test_multi_chunk_roundtrip_with_small_chunks(tmp_path, key):
    import io
    src = tmp_path / "src.sqlite3"
    make_source(src, n=400)
    buf = io.BytesIO()
    snapshot.pack_to(src, buf, key, chunk=4096)  # many tokens
    blob = buf.getvalue()
    # walk the framing: every block is length-prefixed and the stream ends with a zero length
    pos, blocks = len(snapshot.MAGIC), 0
    while True:
        size = int.from_bytes(blob[pos:pos + 4], "big")
        pos += 4
        if size == 0:
            break
        pos += size
        blocks += 1
    assert blocks > 3 and pos == len(blob)
    out = tmp_path / "plain.sqlite3"
    snapshot.unpack(blob, key, out)
    assert out.read_bytes() == src.read_bytes()


def test_default_chunk_is_4_mib():
    assert snapshot.CHUNK == 4 * 1024 * 1024


def test_legacy_single_token_format_still_restores(tmp_path, key):
    blob = legacy_blob(tmp_path, key)
    assert blob.startswith(b"gAAAAA")
    dest = tmp_path / "restored.sqlite3"
    assert snapshot.restore(URL, key, dest, transport=serving(blob)) == {"articles": 5, "stories": 1}
    plain = tmp_path / "p.sqlite3"
    snapshot.unpack(blob, key, plain)
    assert read(plain, "SELECT COUNT(*) FROM articles") == [(5,)]


def test_unpack_file_and_install_file_stream_from_disk(tmp_path, key):
    blob = build_blob(tmp_path, key)
    enc = tmp_path / "snap.enc"
    enc.write_bytes(blob)
    dest = tmp_path / "live.sqlite3"
    assert snapshot.install_file(enc, key, dest) == {"articles": 5, "stories": 1}
    legacy = tmp_path / "legacy.enc"
    legacy.write_bytes(legacy_blob(tmp_path, key))
    assert snapshot.install_file(legacy, key, tmp_path / "live2.sqlite3") == {"articles": 5, "stories": 1}


def test_restore_never_materialises_the_blob_in_memory(tmp_path, key, monkeypatch):
    blob = build_blob(tmp_path, key)
    called = []
    monkeypatch.setattr(snapshot, "download", lambda *a, **k: called.append("download"))
    monkeypatch.setattr(snapshot, "unpack", lambda *a, **k: called.append("unpack"))
    snapshot.restore(URL, key, tmp_path / "live.sqlite3", transport=serving(blob))
    assert called == []  # only the file-based, streaming paths ran
    assert not list(tmp_path.glob(".download-*")) and not list(tmp_path.glob(".restore-*"))


@pytest.mark.parametrize("mutate", [
    lambda b: b[:-4],  # missing terminator
    lambda b: b[:-10],  # cut inside the last block
    lambda b: b + b"X",  # trailing garbage
    lambda b: b[:30] + bytes([b[30] ^ 1]) + b[31:],  # flipped bit inside a token
    lambda b: snapshot.MAGIC + (10**9).to_bytes(4, "big") + b"x" * 20,  # absurd block size
])
def test_chunked_corruption_is_rejected(tmp_path, key, mutate):
    blob = build_blob(tmp_path, key)
    dest = tmp_path / "live.sqlite3"
    make_source(dest, n=2)
    before = dest.read_bytes()
    with pytest.raises(snapshot.SnapshotError):
        snapshot.restore(URL, key, dest, transport=serving(mutate(blob)))
    assert dest.read_bytes() == before


def test_chunked_wrong_key_and_missing_chunk(tmp_path, key):
    import io
    src = tmp_path / "src.sqlite3"
    make_source(src, n=400)
    buf = io.BytesIO()
    snapshot.pack_to(src, buf, key, chunk=4096)
    blob = buf.getvalue()
    with pytest.raises(snapshot.SnapshotError, match="cannot decrypt"):
        snapshot.unpack(blob, Fernet.generate_key().decode(), tmp_path / "o")
    # drop the first block: still decryptable, but the gzip stream is broken
    pos = len(snapshot.MAGIC)
    size = int.from_bytes(blob[pos:pos + 4], "big")
    without_first = snapshot.MAGIC + blob[pos + 4 + size:]
    with pytest.raises(snapshot.SnapshotError):
        snapshot.unpack(without_first, key, tmp_path / "o2")


def test_chunked_size_caps(tmp_path, key):
    import gzip
    import io
    # unpacked cap
    bomb = tmp_path / "bomb.bin"
    bomb.write_bytes(b"\0" * 5_000_000)
    buf = io.BytesIO()
    snapshot.pack_to(bomb, buf, key, chunk=64 * 1024)
    with pytest.raises(snapshot.SnapshotError, match="exceeds"):
        snapshot.unpack(buf.getvalue(), key, tmp_path / "o", max_size=1_000_000)
    # encrypted-size cap for the legacy single token and for downloads
    legacy = Fernet(key).encrypt(gzip.compress(b"x" * 1000))
    with pytest.raises(snapshot.SnapshotError, match="larger"):
        snapshot.unpack_file(_write(tmp_path / "l.enc", legacy), key, tmp_path / "o3", max_blob=50)
    with pytest.raises(snapshot.SnapshotError, match="larger"):
        snapshot.restore(URL, key, tmp_path / "live.sqlite3", transport=serving(b"x" * 500), max_download=100)


def _write(path, data):
    path.write_bytes(data)
    return path
