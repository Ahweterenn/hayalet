"""Uçtan uca medya kontrolü: arama → bölüm → stream → master/ses/altyazı GERÇEKTEN iniyor mu.

`--doctor` yalnız adres çıkıyor mu diye bakar; bu ise o adresleri indirip
içeriğini doğrular (boş 404'ü "çalışıyor" sanmaz). Masaüstünde ve telefonda
(hayalet_app.dbg_probe) aynı kod çalışır. Ağ bağlantılarını çağıran kurar:
`run(contexts)` -> {site: (ok, satır)}.
"""
from __future__ import annotations

import concurrent.futures as cf

from hayalet.core.network import media_headers
from hayalet.core.sites import SITES

# Her sitenin kataloğunda bulunması muhtemel, videosu olan yapımlar.
QUERIES = {
    "dizipal": ["from", "dark"], "hdfilmcehennemi": ["interstellar", "matrix"],
    "diziyou": ["from", "dark"], "dizibox": ["dark", "from"],
    "dizisol": ["cukur", "dark"], "selcukflix": ["dark", "from"],
    "asyawatch": ["vincenzo", "goblin"], "webdramaturkey": ["vincenzo", "goblin"],
    "dizimay": ["love", "a"], "yabancidizi": ["venom", "dark"],
    "fullhdfilmizlesene": ["interstellar", "matrix"], "animecix": ["one piece", "naruto"],
}


def _get(net, url, ref, rng=None):
    h = media_headers(ref)
    if rng:
        h["Range"] = f"bytes=0-{rng - 1}"
    try:
        r = net.get(url, referer=ref, headers=h, retries=1, timeout=20)
        return r.status_code, r.content[:65536]
    except Exception as e:
        return str(e)[:50], b""


def check_site(name, net, session) -> tuple[bool, str]:
    from hayalet.core.m3u8_parser import list_variants
    a = SITES[name]
    notes = []
    for q in QUERIES.get(name, ["a"]):
        try:
            res = a.search(net, session, q)
        except Exception as e:
            notes.append(f"arama '{q}': {str(e)[:60]}")
            continue
        for s in res[:4]:
            try:
                eps = a.get_episodes(net, session, s)
                if not eps:
                    continue
                st = a.build_stream(net, session, eps[0], s)
            except Exception:
                continue
            out, ok = [s.name[:30]], True
            if getattr(st, "kind", "hls") == "file":
                code, b = _get(net, st.video_master_url, st.video_referer, 2048)
                good = code in (200, 206) and len(b) > 100
                ok &= good
                out.append("mp4 " + ("✔" if good else f"✕{code}"))
            else:
                code, b = _get(net, st.video_master_url, st.video_referer)
                good = b"#EXTM3U" in b
                ok &= good
                out.append("video " + ("✔" if good else f"✕{code}"))
                try:
                    v = list_variants(net, session, st.video_master_url, st.video_referer)
                    out.append("/".join(str(x.height) for x in v[:3] if x.height) + "p")
                except Exception:
                    pass
            auds = st.audios or []
            bad = [x.name for x in auds[:3] if b"#EXTM3U" not in _get(net, x.url, x.referer)[1]]
            ok &= not bad
            out.append(f"ses {len(auds)}" + (f" ✕{bad}" if bad else "") if auds else "ses gömülü")
            if st.subtitle_url:
                code, b = _get(net, st.subtitle_url, st.subtitle_referer or st.video_referer)
                good = b"WEBVTT" in b[:300] or b"-->" in b
                ok &= good
                out.append("TR altyazı " + ("✔" if good else f"✕{code}"))
            else:
                out.append("altyazı yok")
            return ok, " | ".join(out)
    return False, "hiçbir sonuçta stream çıkmadı " + "; ".join(notes)[:120]


def run(contexts: dict) -> dict:
    """contexts: {site: (net, session)} — bağlanamayan site listede olmayabilir."""
    out = {}
    with cf.ThreadPoolExecutor(6) as ex:
        futs = {ex.submit(check_site, n, *contexts[n]): n for n in contexts}
        for f in cf.as_completed(futs):
            n = futs[f]
            try:
                out[n] = f.result()
            except Exception as e:
                out[n] = (False, f"{type(e).__name__}: {str(e)[:80]}")
    for n in SITES:
        out.setdefault(n, (False, "BAĞLANILAMADI"))
    return out
