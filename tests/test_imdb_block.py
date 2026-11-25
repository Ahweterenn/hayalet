"""IMDb reddedince (403) bir süre hiç sorulmaz — ağsız."""
import pytest

from hayalet.core import imdb


class _Resp:
    def __init__(self, code):
        self.status_code = code

    def json(self):
        return {"data": {"ok": 1}}


class _Http:
    def __init__(self, code, calls):
        self.code, self.calls = code, calls

    def post(self, *a, **k):
        self.calls.append(1)
        return _Resp(self.code)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(imdb, "_blocked_until", 0.0)


def test_403_blocks_further_requests(monkeypatch):
    calls = []
    monkeypatch.setattr(imdb, "_http", lambda: _Http(403, calls))
    with pytest.raises(imdb.ImdbError):
        imdb._gql("{}")
    with pytest.raises(imdb.ImdbError):
        imdb._gql("{}")
    assert len(calls) == 1          # ikinci çağrı ağa hiç çıkmadı


def test_block_expires(monkeypatch):
    calls = []
    monkeypatch.setattr(imdb, "_http", lambda: _Http(403, calls))
    with pytest.raises(imdb.ImdbError):
        imdb._gql("{}")
    monkeypatch.setattr(imdb, "_blocked_until", 0.0)   # süre doldu
    monkeypatch.setattr(imdb, "_http", lambda: _Http(200, calls))
    assert imdb._gql("{}") == {"ok": 1}


def test_other_errors_do_not_block(monkeypatch):
    calls = []
    monkeypatch.setattr(imdb, "_http", lambda: _Http(500, calls))
    for _ in range(2):
        with pytest.raises(imdb.ImdbError):
            imdb._gql("{}")
    assert len(calls) == 2
