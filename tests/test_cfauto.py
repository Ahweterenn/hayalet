"""core/cfauto.py — ağsız; prefs gerçek dosyaya yazmasın diye taklit edilir."""
from types import SimpleNamespace

from hayalet.core import cfauto


def test_impersonate_for():
    assert cfauto.impersonate_for("Mozilla/5.0 (X11; Linux x86_64) Chrome/157.0 Safari/537.36") == "chrome"
    assert cfauto.impersonate_for(
        "Mozilla/5.0 (Linux; Android 15; wv) Chrome/157.0 Mobile Safari/537.36") == "chrome_android"
    assert cfauto.impersonate_for("Mozilla/5.0 ... Chrome/157.0 Edg/157.0") is None
    assert cfauto.impersonate_for("") is None


def test_parse_cookie():
    assert cfauto.parse_cookie("abc") == {"cf_clearance": "abc"}
    assert cfauto.parse_cookie("cf_clearance=abc") == {"cf_clearance": "abc"}
    assert cfauto.parse_cookie("a=1; cf_clearance=x=y") == {"a": "1", "cf_clearance": "x=y"}


def _fake_prefs(monkeypatch):
    store = {}
    monkeypatch.setattr(cfauto.prefs, "save_cf",
                        lambda h, c, ua: store.__setitem__(h, {"cookie": c, "user_agent": ua}))
    monkeypatch.setattr(cfauto.prefs, "forget_cf", lambda h: store.pop(h, None))
    monkeypatch.setattr(cfauto.prefs, "load_cf", lambda h: store.get(h))
    return store


def test_refresh_uses_registered_solver_and_apply(monkeypatch):
    store = _fake_prefs(monkeypatch)
    ua = "Mozilla/5.0 (Linux; Android 15; wv) Chrome/157.0 Mobile Safari/537.36"
    monkeypatch.setattr(cfauto, "solver", lambda url, t: ("cf_clearance=YENI", ua))
    monkeypatch.delenv("HAYALET_CF_OTOMATIK", raising=False)
    adapter = SimpleNamespace(known_domain="https://www.ornek.nl")
    assert cfauto.refresh(adapter)
    assert store["https://www.ornek.nl"]["cookie"] == "cf_clearance=YENI"

    session = SimpleNamespace(cookies={}, user_agent="x", impersonate="safari")
    assert cfauto.apply_saved(adapter, session)
    assert session.cookies == {"cf_clearance": "YENI"}
    assert session.user_agent == ua and session.impersonate == "chrome_android"


def test_refresh_failure_and_kill_switch(monkeypatch):
    _fake_prefs(monkeypatch)
    adapter = SimpleNamespace(known_domain="https://www.ornek.nl")
    monkeypatch.setattr(cfauto, "solver", lambda url, t: None)
    assert not cfauto.refresh(adapter)
    def boom(url, t):
        raise RuntimeError("webview yok")
    monkeypatch.setattr(cfauto, "solver", boom)
    assert not cfauto.refresh(adapter)
    monkeypatch.setenv("HAYALET_CF_OTOMATIK", "0")
    monkeypatch.setattr(cfauto, "solver", lambda url, t: ("cf_clearance=X", "ua"))
    assert not cfauto.refresh(adapter)
