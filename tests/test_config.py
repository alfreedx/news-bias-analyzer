from app.config import Settings


def test_render_hostname_is_allowed_and_used_as_site_url(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_HOSTNAME", "contraste.onrender.com")
    monkeypatch.setenv("ALLOWED_HOSTS", "localhost")
    monkeypatch.delenv("SITE_URL", raising=False)
    s = Settings()
    assert s.allowed_hosts == ["localhost", "contraste.onrender.com"]
    assert s.site_url == "https://contraste.onrender.com"


def test_explicit_site_url_wins_and_no_render_means_defaults(monkeypatch):
    monkeypatch.delenv("RENDER_EXTERNAL_HOSTNAME", raising=False)
    monkeypatch.setenv("ALLOWED_HOSTS", "a.example")
    monkeypatch.setenv("SITE_URL", "https://a.example")
    s = Settings()
    assert s.allowed_hosts == ["a.example"]
    assert s.site_url == "https://a.example"
