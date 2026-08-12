"""Dizipal site adapter'ı — ince sarmalayıcı, sıfır mantık değişikliği.

Tüm gerçek iş zaten `core/catalog.py`, `core/resolver.py`, `core/merge.py`'de
var; bu dosya sadece onları SiteAdapter arayüzüne uydurur. Var olan Dizipal
davranışının birebir aynı kalmasını garantilemek için buraya yeni mantık
EKLENMEZ — iki istisna dışında: (1) çoklu-site aramada sonucun kaynağını ayırt
etmek için Series.site damgalama, (2) aynı yapımın "X" / "X Türkçe Dublaj"
kayıtlarının tek satıra indirilmesi (bkz. _collapse_dubs).

Not: Burada bir zamanlar "Movies" tipi sonuçlar da eleniyordu (filmler
hdfilmcehennemi'nin işi diye). O süzgeç KALDIRILDI — yalnızca Dizipal'de bulunan
Türk filmlerini tamamen erişilemez kılıyordu; gerekçesi bkz. search().
"""
from __future__ import annotations

import html as _html
import re
from urllib.parse import urlparse

from hayalet import config
from hayalet.core import catalog, merge, resolver
from hayalet.core.merge import MergedStream
from hayalet.core.models import Episode, Series
from hayalet.core.network import Network
from hayalet.core.session import SessionState
from hayalet.core.sites import register


# --- katalog gezinme -------------------------------------------------------
# Ana sayfadaki "Trend Diziler" şeridi BİLEREK kullanılmıyor: o kartlarda yalnız
# poster ve sıra numarası var, yapım adı hiç geçmiyor (canlı doğrulandı) — poster
# göstermediğimiz için adsız kart işe yaramaz. Dizi listeleme sayfasındaki
# kartlarda ise ad `title="... izle"` niteliğinde duruyor.
_LIST_PATH = "/yabanci-dizi-izle"
_CARD_RE = re.compile(
    r'<a[^>]+href="([^"]*?/series/[a-z0-9\-]+)"[^>]*?title="([^"]*?)\s*izle"', re.S)
_HOME_ROW_LIMIT = 20


def _collapse_dubs(results: list[Series]) -> list[Series]:
    """Aynı yapımın "X" ve "X Türkçe Dublaj" kayıtlarını tek satıra indirir.

    Dizipal her diziyi iki ayrı kayıt olarak tutuyor; ikisini de listelemek
    sonuçların yarısını tekrara harcıyordu. Tekrar zararsız da değil: kullanıcı
    hangisini seçerse seçsin `merge.build_merged` karşı sürümü zaten kendisi
    bulup Türkçe dublaj + orijinal sesi TEK akışta birleştiriyor — yani iki satır
    birebir aynı sonucu veriyor.

    Orijinal kayıt tercih edilir (dublaj etiketi başlığı kirletiyor); yalnız
    dublajı olan yapım elenmez, olduğu gibi kalır. Bu ELEME BİLEREK adapter
    katmanında: `catalog.search` her iki kaydı da döndürmeye devam etmeli, yoksa
    `catalog.find_counterpart` eşleştiremez.

    Anahtara film/dizi ayrımı da giriyor: Dizipal'de aynı ada sahip AYRI bir
    dizi ve film olabiliyor ("Sıfır Bir" hem dizi hem film) — bunlar tekrar
    değil, iki farklı yapım; birleştirilirse biri kaybolur.
    """
    order: list[tuple[bool, str]] = []
    picked: dict[tuple[bool, str], Series] = {}
    for r in results:
        key = (bool(catalog.is_movie(r)),
               catalog.base_title(r.name).casefold() or r.slug)
        if key not in picked:
            order.append(key)
            picked[key] = r
        elif catalog.is_dubbed(picked[key]) and not catalog.is_dubbed(r):
            picked[key] = r
    return [picked[k] for k in order]


class DizipalAdapter:
    name = "dizipal"
    known_domain = config.KNOWN_DOMAIN

    def resolve_domain(self, net: Network, session: SessionState,
                       override: str | None = None, use_cache: bool = True) -> str:
        return resolver.resolve(net, session, override=override, use_cache=use_cache)

    # Dizipal'in "Movies" kategorisi ARTIK ELENMİYOR. Eskiden "filmler
    # hdfilmcehennemi'nin işi, aynı film iki kaynaktan çıkıp kafa karıştırmasın"
    # diye atılıyordu; ama bu gerekçe yalnızca iki sitede birden bulunan filmler
    # için geçerli. Türk filmleri (Sıfır Bir, Adana İşi, Çakallarla Dans…)
    # hdfilmcehennemi'de HİÇ yok — canlı doğrulandı — yani süzgeç onları tamamen
    # erişilemez kılıyordu ("dizisi çıkıyor, filmi çıkmıyor" şikayeti). Kayıtlar
    # boru hattında sağlam çalışıyor: get_episodes tek bölüm veriyor,
    # build_stream gerçek master.m3u8 çözüyor (canlı doğrulandı). Aynı yapımın
    # iki siteden birden çıkması zaten dizilerde de oluyor ve zararsız — üstelik
    # bir kaynak ölüyse diğeri elde kalıyor.
    def search(self, net: Network, session: SessionState, query: str) -> list[Series]:
        results = _collapse_dubs(catalog.search(net, session, query))
        for r in results:
            r.site = self.name
        return results

    def suggest(self, net: Network, session: SessionState, query: str) -> list[Series]:
        results = _collapse_dubs(catalog.suggest(net, session, query))
        for r in results:
            r.site = self.name
        return results

    def browse(self, net: Network, session: SessionState, kind: str) -> list[Series]:
        """Dizi listeleme sayfası (arama yapmadan katalog).

        Yalnızca "dizi": Dizipal'in film kategorisi search/suggest'te de eleniyor
        (filmler hdfilmcehennemi'nin işi), gezinmede de tutarlı kalsın.
        """
        if kind != "dizi":
            return []
        page = net.get(session.base_url + _LIST_PATH, referer=session.base_url).text
        out: list[Series] = []
        seen: set[str] = set()
        for href, title in _CARD_RE.findall(page):
            slug = urlparse(href).path.strip("/")
            name = _html.unescape(title).strip()
            if not slug or not name or slug in seen:
                continue
            seen.add(slug)
            out.append(Series(name=name, slug=slug, type="Series", site=self.name))
        return _collapse_dubs(out)

    def home_rows(self, net: Network, session: SessionState) -> list[dict]:
        items = self.browse(net, session, "dizi")[:_HOME_ROW_LIMIT]
        return [{"title": "Diziler", "items": items}] if items else []

    def get_episodes(self, net: Network, session: SessionState,
                     series: Series) -> list[Episode]:
        return catalog.get_episodes(net, session, series)

    def build_stream(self, net: Network, session: SessionState,
                     episode: Episode, series: Series) -> MergedStream:
        return merge.build_merged(net, session, episode, series)


register(DizipalAdapter())
