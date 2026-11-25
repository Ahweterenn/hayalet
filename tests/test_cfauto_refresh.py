"""cfauto.refresh: eşzamanlı yenilemede çözücü bir kez çalışır, eski kayıt
yenisi gelene kadar okunabilir kalır — ağsız, sahte çözücüyle."""
import threading
import time

import pytest

from hayalet.core import cfauto, prefs


class _Adapter:
    known_domain = "https://www.ornek.test"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(prefs, "_FILE", tmp_path / "prefs.json")
    monkeypatch.delenv("HAYALET_CF_OTOMATIK", raising=False)
    yield
    cfauto.solver = None


def test_concurrent_refresh_runs_solver_once():
    calls = []

    def solver(url, timeout):
        calls.append(url)
        time.sleep(0.3)
        return f"cf_clearance=yeni{len(calls)}", "UA"

    cfauto.solver = solver
    results = []
    ts = [threading.Thread(target=lambda: results.append(cfauto.refresh(_Adapter())))
          for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert results == [True] * 4
    assert len(calls) == 1
    assert prefs.load_cf(_Adapter.known_domain)["cookie"] == "cf_clearance=yeni1"


def test_old_cookie_stays_readable_while_solving():
    prefs.save_cf(_Adapter.known_domain, "cf_clearance=eski", "UA")
    seen = []

    def solver(url, timeout):
        rec = prefs.load_cf(url)
        seen.append(rec and rec["cookie"])
        return "cf_clearance=yeni", "UA"

    cfauto.solver = solver
    assert cfauto.refresh(_Adapter())
    assert seen == ["cf_clearance=eski"]
    assert prefs.load_cf(_Adapter.known_domain)["cookie"] == "cf_clearance=yeni"


def test_failed_refresh_forgets_stale_record():
    prefs.save_cf(_Adapter.known_domain, "cf_clearance=eski", "UA")
    cfauto.solver = lambda url, timeout: None
    assert not cfauto.refresh(_Adapter())
    assert prefs.load_cf(_Adapter.known_domain) is None
