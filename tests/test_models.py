"""match_episode / next_episode saf-mantık testleri (ağsız).

"Sonraki bölüm" (izlerken otomatik geçiş) ve "kaldığın yerden devam" özellikleri
bu iki fonksiyona dayanıyor — burada bir hata sessizce yanlış bölüme atlamaya ya
da hiç geçmemeye yol açabilir.
"""
from __future__ import annotations

from hayalet.core.models import Episode, match_episode, next_episode

S1 = [Episode(season=1, number=1, url="s1e1"),
      Episode(season=1, number=2, url="s1e2"),
      Episode(season=1, number=3, url="s1e3")]
S2 = [Episode(season=2, number=1, url="s2e1"),
      Episode(season=2, number=2, url="s2e2")]
ALL_EPS = S1 + S2


# --- match_episode ----------------------------------------------------------
def test_match_episode_exact_season_and_number():
    ep = match_episode(ALL_EPS, season=2, number=1)
    assert ep is not None and ep.url == "s2e1"


def test_match_episode_falls_back_to_number_only_when_season_missing():
    # Kayıtlı sezon (99) dizide yok; ama bölüm numarası (2) eşleşen bir sezon
    # (1. sezon) var — yakın-eşleşme fallback'i onu bulmalı.
    ep = match_episode(ALL_EPS, season=99, number=2)
    assert ep is not None and ep.url == "s1e2"


def test_match_episode_none_when_nothing_matches():
    assert match_episode(ALL_EPS, season=99, number=999) is None


def test_match_episode_empty_list():
    assert match_episode([], season=1, number=1) is None


# --- next_episode -------------------------------------------------------------
def test_next_episode_within_same_season():
    nxt = next_episode(ALL_EPS, S1[0])
    assert nxt is not None and nxt.url == "s1e2"


def test_next_episode_crosses_season_boundary():
    # 1. sezonun son bölümünden sonrası 2. sezonun ilk bölümü olmalı (dizinin
    # tamamı üzerinden ilerler, sezon sınırında durmaz).
    nxt = next_episode(ALL_EPS, S1[-1])
    assert nxt is not None and nxt.url == "s2e1"


def test_next_episode_none_at_series_end():
    assert next_episode(ALL_EPS, S2[-1]) is None


def test_next_episode_handles_unsorted_input():
    # episodes listesi HER ZAMAN sıralı gelmeyebilir (ör. get_episodes'ın
    # kazıma sırası) — next_episode kendi içinde (season, number) sıralamalı.
    shuffled = [S2[1], S1[0], S2[0], S1[2], S1[1]]
    nxt = next_episode(shuffled, S1[0])
    assert nxt is not None and nxt.url == "s1e2"


def test_next_episode_current_not_in_list():
    # 'current', episodes listesinde YOKSA next_episode patlamamalı, None dönmeli.
    orphan = Episode(season=5, number=5, url="orphan")
    assert next_episode(ALL_EPS, orphan) is None
