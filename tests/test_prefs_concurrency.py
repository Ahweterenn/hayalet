"""prefs.json aynı anda birçok iş parçacığından yazılınca veri kaybolmamalı — ağsız.

Telefonda açılışta raflar, cf yenileme ve devam kaydı aynı anda yazıyordu;
dosya yazılırken okuyan {} görüyor, {}'nin üstüne yazan da her şeyi siliyordu.
"""
import threading

from hayalet.core import prefs


def test_concurrent_writes_lose_nothing_and_readers_never_see_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(prefs, "_FILE", tmp_path / "prefs.json")
    prefs.save(keep="me")
    empty_reads = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            if not prefs.load():
                empty_reads.append(1)

    def writer(i):
        for j in range(40):
            prefs.save(**{f"k{i}": j})
            prefs.save_cf(f"site{i}.example", f"cf_clearance={j}", "UA")

    r = threading.Thread(target=reader)
    r.start()
    ws = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for w in ws:
        w.start()
    for w in ws:
        w.join()
    stop.set()
    r.join()

    data = prefs.load()
    assert data["keep"] == "me"
    assert all(data[f"k{i}"] == 39 for i in range(8))
    assert all(prefs.load_cf(f"site{i}.example")["cookie"] == "cf_clearance=39" for i in range(8))
    assert not empty_reads
    assert not list(tmp_path.glob("*.tmp"))     # geçici dosya kalmamalı
