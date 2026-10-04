"""socket.io olay işleyicileri.

Olay adları ve yükleri oda sayfasıyla (web/oda.js) birebir eşleşmeli.
"Gönderen hariç" yayın python-socketio'da `skip_sid=sid`. Bu ayrım kritik:
`play`/`pause`/`seek` gönderene geri dönerse oynatıcı kendi olayını yeniden
işleyip sonsuz döngüye giriyor.

Yetki üç katmanlı:
  * lider (ev sahibi): içerik koyar, önerileri onaylar, kişi çıkarır,
    kumanda modunu değiştirir;
  * kumanda: `controlMode` "all" ise herkes, "host" ise yalnız lider
    oynatır/durdurur/sarar;
  * misafir: içerik ÖNERİR (katalogdan ya da link), izler, konuşur.
"""
from __future__ import annotations

import html
import threading
import time

from hayalet.oda import rooms as R

_MAX_CHAT = 1000


def _youtube_title(url: str) -> str | None:
    """YouTube linki ise ekranda gösterilecek kısa ad, değilse None."""
    low = url.lower()
    if "youtube.com/" in low or "youtu.be/" in low:
        return "YouTube"
    return None


_yt_meta_cache: dict[str, tuple[str, str]] = {}


def _youtube_meta(url: str) -> tuple[str, str]:
    """YouTube videosunun başlığı ve küçük resmi (oEmbed, anahtarsız).
    Sıra, öneri ve birlikte izleme geçmişinde "YouTube" yerine gerçek ad
    görünsün diye. Alınamazsa ("", "") — içerik yine açılır."""
    if not _youtube_title(url):
        return "", ""
    if url in _yt_meta_cache:
        return _yt_meta_cache[url]
    meta = ("", "")
    try:
        from curl_cffi import requests as cr
        r = cr.get("https://www.youtube.com/oembed",
                   params={"url": url, "format": "json"}, timeout=4,
                   impersonate="chrome")
        if r.status_code == 200:
            d = r.json()
            meta = (str(d.get("title") or "")[:120], str(d.get("thumbnail_url") or ""))
    except Exception:
        pass
    _yt_meta_cache[url] = meta
    return meta


def _esc(s) -> str:
    return html.escape(str(s or ""), quote=True)


def _client_ip(environ: dict) -> str:
    """Röle/tünel arkasında gerçek istemci X-Forwarded-For'un ilk parçasıdır."""
    xff = environ.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return environ.get("HTTP_CF_CONNECTING_IP") or environ.get("REMOTE_ADDR", "")


def register(sio, store: R.RoomStore, host_token: str,
             catalog=None, on_identity=None) -> dict:
    """Tüm olayları verilen socket.io sunucusuna bağlar.

    `catalog` (bkz. catalog.Catalog) verilirse odanın içinden arama/bölüm
    seçme açılır; `on_identity(resolved)` çözülen akışın kimliğini (UA,
    çerez) proxy'ye taşır. Dönen sözlük sunucunun kendi tetikleyebildiği
    işlemleri taşır (uygulamadan "şu bölümü aç").
    """

    def leader_of(room):
        return R.compute_leader(room)

    def emit_leader_state(room):
        leader = leader_of(room)
        if not leader:
            return
        for u in room.users:
            sio.emit("role-updated",
                     {"isLeader": u.sid == leader.sid,
                      "leaderUsername": leader.username}, to=u.sid)

    def emit_people(room):
        sio.emit("room-users", {"users": room.usernames()}, room=room.id)
        sio.emit("people", {"people": room.people()}, room=room.id)

    def emit_suggestions(room):
        """Öneriler yalnız lidere gider; misafirler başkasının önerisini görmez."""
        leader = leader_of(room)
        if leader:
            sio.emit("suggestions", {"items": room.suggestions}, to=leader.sid)

    def sys_msg(room_id, message):
        sio.emit("system-message", {"message": message}, room=room_id)

    def to_sid(sid, event, payload):
        sio.emit(event, payload, to=sid)

    def room_of(data):
        return store.get(str((data or {}).get("roomId", "")))

    def background(fn, *args):
        """Yavaş iş (arama, akış çözme) olay iş parçacığını kilitlemesin."""
        start = getattr(sio, "start_background_task", None)
        if start:
            start(fn, *args)
        else:
            threading.Thread(target=fn, args=args, daemon=True).start()

    def put_video(room, url, subtitles, now, headers=None, start=0.0):
        """Odaya yeni içerik koyar ve herkese duyurur. `start`: birlikte izleme
        geçmişinden "kaldığınız yerden devam" (saniye)."""
        if headers is not None:
            room.headers = {str(k).lower(): v for k, v in headers.items()}
        room.video_url = url
        room.subtitles = R.normalize_subtitles(subtitles or [])
        room.current_time = max(0.0, float(start or 0))
        room.is_playing = False
        room.now = dict(now or {})
        sio.emit("video-changed",
                 {"videoUrl": room.video_url,
                  "subtitles": [s.as_dict() for s in room.subtitles],
                  "now": room.now, "startTime": room.current_time}, room=room.id)
        if room.now.get("kind") == "youtube":
            meta_to_now(room, url, keep_title=room.now.get("title") not in ("", "YouTube"))

    def emit_queue(room):
        sio.emit("queue", {"items": room.queue}, room=room.id)

    def link_now(url, title=""):
        yt = _youtube_title(url)
        return {"kind": "youtube" if yt else "link", "title": title or yt or "Bağlantı",
                "subtitle": "", "poster": "", "hasNext": False}

    def later_meta(url, apply):
        """YouTube başlığı/küçük resmi arka planda: içerik beklemeden açılır,
        ad gelince `apply(başlık, resim)` yerine koyar ve duyurur."""
        if not _youtube_title(url):
            return

        def work():
            title, thumb = _youtube_meta(url)
            if title or thumb:
                apply(title, thumb)
        background(work)

    def meta_to_now(room, url, keep_title):
        def apply(title, thumb):
            if room.video_url != url or room.now.get("kind") != "youtube":
                return      # bu arada başka içerik açıldı
            if title and not keep_title:
                room.now.update(title=title, subtitle="YouTube")
            if thumb:
                room.now["poster"] = thumb
            sio.emit("now", {"now": room.now}, room=room.id)
        later_meta(url, apply)

    def meta_to_item(item, emit_fn, keep_title):
        def apply(title, thumb):
            if title and not keep_title:
                item.update(title=title, subtitle="YouTube")
            if thumb:
                item["poster"] = thumb
            emit_fn()
        later_meta(item.get("url", ""), apply)

    def play_item(room, item, sid=None):
        """Sıradan ya da öneriden gelen içeriği açar."""
        if item.get("kind") == "hayalet" and item.get("ref") and catalog is not None:
            background(play_ref, room, item["ref"], sid)
        elif item.get("url"):
            put_video(room, item["url"], [], link_now(item["url"], item.get("title", "")))

    def play_ref(room, ref, sid=None, start=0.0):
        """Katalog ref'ini çözüp odaya koyar (arka planda çalışır)."""
        sio.emit("content-loading", {"loading": True}, room=room.id)
        try:
            r = catalog.resolve(ref)
        except Exception as e:  # noqa: BLE001 - sebep kullanıcıya iletilir
            sio.emit("content-loading", {"loading": False}, room=room.id)
            # Uygulamadan gelen istekte (sid yok) hata ev sahibinin ekranına.
            # to=None HERKESE yayın demek; o yüzden açıkça hedef seçiliyor.
            target = sid or (leader_of(room).sid if leader_of(room) else None)
            if target:
                to_sid(target, "catalog-error", {"message": f"Açılamadı: {e}"})
            return
        if on_identity:
            try:
                on_identity(r)
            except Exception:
                pass
        put_video(room, r.url, r.subtitles, r.now,
                  headers={"referer": r.referer, "user-agent": r.user_agent},
                  start=start)
        sio.emit("content-loading", {"loading": False}, room=room.id)

    # --- katılma ---------------------------------------------------------
    @sio.on("join-room")
    def join_room(sid, data):
        if not isinstance(data, dict) or not data.get("roomId"):
            return
        room_id = str(data["roomId"])
        raw_name = data.get("username")
        # Node sürümünde string olmayan bir ad tüm süreci çökertiyordu.
        username = (raw_name.strip()[:40]
                    if isinstance(raw_name, str) and raw_name.strip() else "Misafir")
        environ = sio.get_environ(sid) or {}
        ip = _client_ip(environ)

        room = store.get_or_create(room_id)
        if room.is_banned(username, ip):
            sio.emit("kicked", {"by": "Sistem (eski yasaklı)"}, to=sid)
            sio.disconnect(sid)
            return

        # Ev sahibi: sunucunun ürettiği anahtarla bağlanan. Liderlik bununla
        # belirleniyor; IP tahmini röle arkasında yanlış kişiyi seçiyordu.
        qs = environ.get("QUERY_STRING", "") or ""
        # Anahtar iki yoldan gelebilir: el sıkışmanın sorgu dizesi ya da
        # join yükü. İkincisi şart — yeniden bağlanmalarda sorgu dizesi
        # korunmayabiliyor ve ev sahibi liderliğini kaybediyordu.
        yuk_token = (data or {}).get("hostToken")
        is_host = bool(host_token) and (f"hostToken={host_token}" in qs
                                        or yuk_token == host_token)

        sio.enter_room(sid, room_id)
        room.add_user(sid, username, ip, is_host=is_host, color=data.get("color"))
        sio.save_session(sid, {"room_id": room_id, "username": username})

        state = room.state()
        state["catalog"] = catalog is not None
        sio.emit("room-state", state, to=sid)
        sio.emit("user-joined",
                 {"username": username, "users": room.usernames()},
                 room=room_id, skip_sid=sid)
        emit_people(room)
        emit_leader_state(room)
        emit_suggestions(room)

    # --- video kaynağı ---------------------------------------------------
    @sio.on("set-video")
    def set_video(sid, data):
        """Link ile içerik (HLS/MP4/YouTube). Lider koyar, misafir önerir."""
        if not isinstance(data, dict) or not data.get("roomId"):
            return
        room = store.get(str(data["roomId"]))
        if not room:
            return
        video_url = str(data.get("videoUrl") or data.get("url") or "").strip()
        subs = R.normalize_subtitles(data.get("subtitles"))
        title = str(data.get("title") or "").strip()[:120]

        if not room.is_leader(sid):
            u = room.find_user(sid)
            if u and video_url.startswith(("http://", "https://")):
                item = {"kind": "link", "url": video_url,
                        "title": title or _youtube_title(video_url) or video_url[:80]}
                if room.add_suggestion(u, item):
                    emit_suggestions(room)
                    to_sid(sid, "suggestion-sent", {"title": item["title"]})
                    meta_to_item(item, lambda: emit_suggestions(room), keep_title=bool(title))
            return

        if data.get("headers"):
            room.headers = {str(k).lower(): v for k, v in data["headers"].items()}
        if data.get("subHeaders"):
            room.sub_headers = data["subHeaders"]

        # Aynı video+altyazı yeniden gelirse oynatmayı SIFIRLAMA — yeniden
        # bağlanma aynı yükü tekrar gönderebiliyor.
        if (str(room.video_url or "") == str(video_url or "")
                and R.subtitles_equal(room.subtitles, subs)):
            return

        put_video(room, video_url, data.get("subtitles"), link_now(video_url, title))

    # --- oynatma denetimi -------------------------------------------------
    def _playback(event, playing):
        def handler(sid, data):
            if not isinstance(data, dict):
                return
            room = store.get(str(data.get("roomId", "")))
            if not room:
                return
            if not room.can_control(sid):
                # Kumanda ev sahibinde: istemci kendi hareketini geri alsın.
                to_sid(sid, "control-denied",
                       {"currentTime": room.current_time,
                        "isPlaying": room.is_playing})
                return
            t = data.get("currentTime")
            if isinstance(t, (int, float)):
                room.current_time = float(t)
            if playing is not None:
                room.is_playing = playing
            sio.emit(event, {"currentTime": room.current_time},
                     room=room.id, skip_sid=sid)
        return handler

    sio.on("play")(_playback("play", True))
    sio.on("pause")(_playback("pause", False))
    sio.on("seek")(_playback("seek", None))

    @sio.on("sync-heartbeat")
    def heartbeat(sid, data):
        if not isinstance(data, dict):
            return
        room = store.get(str(data.get("roomId", "")))
        if not room or not room.users:
            return
        leader = leader_of(room)
        # Yalnız lider odanın saatini yazabilir; herkes yazsaydı en geride
        # kalan izleyici sürekli diğerlerini geri sarardı.
        if not leader or leader.sid != sid:
            return
        t = data.get("currentTime")
        if isinstance(t, (int, float)):
            room.current_time = float(t)
        room.is_playing = bool(data.get("isPlaying"))
        sio.emit("sync-heartbeat",
                 {"currentTime": room.current_time, "isPlaying": room.is_playing},
                 room=room.id, skip_sid=sid)

    # --- bekleme (buffering) ---------------------------------------------
    @sio.on("buffering-start")
    def buffering_start(sid, data):
        room = store.get(str((data or {}).get("roomId", "")))
        if not room:
            return
        t = (data or {}).get("currentTime")
        if isinstance(t, (int, float)):
            room.current_time = float(t)
        if room.start_buffering(sid):
            u = room.find_user(sid)
            sio.emit("room-buffering",
                     {"isBuffering": True,
                      "username": u.username if u else "?",
                      "activeCount": len(room.buffering),
                      "currentTime": room.current_time}, room=room.id)

    @sio.on("buffering-end")
    def buffering_end(sid, data):
        room = store.get(str((data or {}).get("roomId", "")))
        if not room:
            return
        t = (data or {}).get("currentTime")
        if isinstance(t, (int, float)):
            room.current_time = float(t)
        if room.end_buffering(sid):
            u = room.find_user(sid)
            sio.emit("room-buffering",
                     {"isBuffering": False,
                      "username": u.username if u else "?",
                      "activeCount": 0,
                      "currentTime": room.current_time}, room=room.id)

    # --- sohbet ------------------------------------------------------------
    @sio.on("chat-message")
    def chat(sid, data):
        if not isinstance(data, dict) or not data.get("roomId"):
            return
        room = store.get(str(data["roomId"]))
        if not room:
            return
        message = str(data.get("message") or "").strip()[:_MAX_CHAT]
        if not message:
            return
        # Ad ve renk sunucudaki kayıttan: istemcinin bildirdiği ada güvenilseydi
        # herkes başkasının adıyla yazabilirdi.
        u = room.find_user(sid)
        payload = {"id": sid,
                   "username": u.username if u else "Misafir",
                   "color": u.color if u else "",
                   "message": message,
                   "time": time.strftime("%H:%M")}
        room.add_chat(payload)
        sio.emit("chat-message", payload, room=room.id)

    # --- tepkiler ------------------------------------------------------------
    @sio.on("reaction")
    def reaction(sid, data):
        """Videonun üstünde uçan emoji. Geçmişe yazılmaz; sonradan katılan görmez."""
        room = room_of(data)
        if not room:
            return
        u = room.find_user(sid)
        emoji = str((data or {}).get("emoji") or "")
        if not u or not room.allow_reaction(sid, emoji):
            return
        sio.emit("reaction", {"id": sid, "emoji": emoji,
                              "username": u.username, "color": u.color}, room=room.id)

    # --- izleme sırası ---------------------------------------------------------
    # Sırayı herkes görür, yalnız ev sahibi değiştirir. Misafir sıraya ekletmek
    # için öneri gönderir; ev sahibi öneriyi "Sıraya ekle" ile alır.
    def _queue_item(data) -> dict | None:
        d = data or {}
        title = str(d.get("title") or "").strip()[:120]
        if d.get("ref") and catalog is not None:
            return {"kind": "hayalet", "ref": str(d["ref"]), "title": title,
                    "subtitle": str(d.get("subtitle") or "")[:80],
                    "poster": str(d.get("poster") or "")}
        url = str(d.get("url") or "").strip()
        if url.startswith(("http://", "https://")):
            return {"kind": "link", "url": url,
                    "title": title or _youtube_title(url) or url[:80]}
        return None

    @sio.on("queue-add")
    def queue_add(sid, data):
        room = room_of(data)
        if not room or not room.is_leader(sid):
            return
        item = _queue_item(data)
        u = room.find_user(sid)
        q = room.queue_add(u.username if u else "", item) if item else None
        if q:
            emit_queue(room)
            to_sid(sid, "queue-added", {"title": q["title"]})
            if q.get("url"):
                meta_to_item(q, lambda: emit_queue(room),
                             keep_title=bool(str((data or {}).get("title") or "").strip()))

    @sio.on("queue-remove")
    def queue_remove(sid, data):
        room = room_of(data)
        if room and room.is_leader(sid) and room.queue_take(str((data or {}).get("id", ""))):
            emit_queue(room)

    @sio.on("queue-move")
    def queue_move(sid, data):
        room = room_of(data)
        d = data or {}
        if room and room.is_leader(sid) and isinstance(d.get("delta"), int) \
                and room.queue_move(str(d.get("id", "")), d["delta"]):
            emit_queue(room)

    @sio.on("queue-play")
    def queue_play(sid, data):
        """Sıradan birini hemen aç; `id` yoksa baştakini (bölüm/video bitince)."""
        room = room_of(data)
        if not room or not room.is_leader(sid):
            return
        qid = (data or {}).get("id")
        item = room.queue_take(str(qid) if qid else None)
        if not item:
            return
        emit_queue(room)
        play_item(room, item, sid)

    # --- oda ayarları --------------------------------------------------------
    @sio.on("room-settings")
    def room_settings(sid, data):
        room = room_of(data)
        if not room or not room.is_leader(sid):
            return
        mode = (data or {}).get("controlMode")
        if mode is not None and room.set_control_mode(str(mode)):
            sio.emit("room-settings", {"controlMode": room.control_mode}, room=room.id)
            sys_msg(room.id, "🎮 Kumanda artık herkeste." if mode == "all"
                    else "🎮 Kumanda artık yalnız ev sahibinde.")
            emit_people(room)

    @sio.on("kick-user")
    def kick_user(sid, data):
        room = room_of(data)
        if not room or not room.is_leader(sid):
            return
        target = room.find_user(str((data or {}).get("id", "")))
        if not target or target.sid == sid:
            return
        _kick(room, room.find_user(sid), target)

    def _kick(room, leader, target):
        room.ban(target)
        sio.emit("kicked", {"by": leader.username}, to=target.sid)
        room.remove_user(target.sid)
        sio.emit("user-left",
                 {"username": target.username, "users": room.usernames()},
                 room=room.id)
        sys_msg(room.id,
                f"👢 <b>{_esc(target.username)}</b> odadan çıkarıldı.")
        sio.disconnect(target.sid)
        emit_people(room)
        emit_leader_state(room)

    # --- katalog (odanın içinden içerik seçme) --------------------------------
    @sio.on("catalog-search")
    def catalog_search(sid, data):
        room = room_of(data)
        if not room or catalog is None:
            return
        q, req = str((data or {}).get("q", "")), (data or {}).get("reqId")

        def work():
            try:
                items = catalog.search(q)
                to_sid(sid, "catalog-results", {"reqId": req, "items": items})
            except Exception as e:  # noqa: BLE001
                to_sid(sid, "catalog-error", {"reqId": req, "message": f"Arama başarısız: {e}"})
        background(work)

    @sio.on("catalog-detail")
    def catalog_detail(sid, data):
        room = room_of(data)
        if not room or catalog is None:
            return
        ref, req = (data or {}).get("ref"), (data or {}).get("reqId")

        def work():
            try:
                to_sid(sid, "catalog-detail", {"reqId": req, **catalog.detail(ref)})
            except Exception as e:  # noqa: BLE001
                to_sid(sid, "catalog-error", {"reqId": req, "message": str(e)})
        background(work)

    @sio.on("catalog-play")
    def catalog_play(sid, data):
        """Lider oynatır; misafirin seçimi öneri olur."""
        room = room_of(data)
        if not room or catalog is None:
            return
        ref = str((data or {}).get("ref", ""))
        if room.is_leader(sid):
            background(play_ref, room, ref, sid)
            return
        u = room.find_user(sid)
        title = str((data or {}).get("title") or "")[:120]
        subtitle = str((data or {}).get("subtitle") or "")[:80]
        if u and ref and room.add_suggestion(u, {"kind": "hayalet", "ref": ref,
                                                 "title": title, "subtitle": subtitle,
                                                 "poster": str((data or {}).get("poster") or "")}):
            emit_suggestions(room)
            to_sid(sid, "suggestion-sent", {"title": title})

    @sio.on("next-episode")
    def next_ep(sid, data):
        room = room_of(data)
        if not room or catalog is None or not room.is_leader(sid):
            return
        nxt = catalog.next_of(room.now.get("ref", ""))
        if nxt:
            background(play_ref, room, nxt, sid)

    @sio.on("prefetch-next")
    def prefetch_next(sid, data):
        """Lider bölümün son dakikalarına girdi: sonrakini şimdiden çöz ki
        geçişte oda "Açılıyor…" diye beklemesin."""
        room = room_of(data)
        if not room or catalog is None or not room.is_leader(sid):
            return
        pf = getattr(catalog, "prefetch_next", None)
        if pf:
            background(pf, room.now.get("ref", ""))

    @sio.on("suggestion-accept")
    def suggestion_accept(sid, data):
        room = room_of(data)
        if not room or not room.is_leader(sid):
            return
        s = room.take_suggestion(str((data or {}).get("id", "")))
        emit_suggestions(room)
        if not s:
            return
        item = s["item"]
        if (data or {}).get("toQueue"):
            if room.queue_add(s["by"], item):
                emit_queue(room)
                sys_msg(room.id, f"📋 <b>{_esc(s['by'])}</b> önerisi sıraya eklendi: {_esc(item.get('title', ''))}")
            return
        sys_msg(room.id, f"✅ <b>{_esc(s['by'])}</b> önerisi açılıyor: {_esc(item.get('title', ''))}")
        play_item(room, item, sid)

    @sio.on("suggestion-dismiss")
    def suggestion_dismiss(sid, data):
        room = room_of(data)
        if not room or not room.is_leader(sid):
            return
        room.take_suggestion(str((data or {}).get("id", "")))
        emit_suggestions(room)

    @sio.on("change-nick")
    def change_nick(sid, data):
        room = store.get(str((data or {}).get("roomId", "")))
        if not room:
            return
        u = room.find_user(sid)
        new = str((data or {}).get("newName") or "").strip()[:40]
        if not u or not new:
            return
        color = R.clean_color((data or {}).get("color"))
        if color:
            u.color = color
        old, u.username = u.username, new
        emit_people(room)
        sys_msg(room.id,
                f"✏️ <b>{_esc(old)}</b> ismini <b>{_esc(new)}</b> olarak değiştirdi.")
        emit_leader_state(room)

    # --- yönetim komutları -------------------------------------------------
    @sio.on("admin-command")
    def admin_command(sid, data):
        if not isinstance(data, dict):
            return
        room = store.get(str(data.get("roomId", "")))
        if not room:
            return
        leader = leader_of(room)
        if not leader or leader.sid != sid:
            sio.emit("system-message",
                     {"message": "⛔ Bu komutu yalnızca oda sahibi kullanabilir."},
                     to=sid)
            return
        cmd = str(data.get("command") or "")
        args = data.get("args") or []
        who = _esc(leader.username)

        if cmd == "clearvideo":
            room.video_url = ""
            room.subtitles = []
            room.current_time = 0.0
            room.is_playing = False
            sio.emit("room-state", room.state(), room=room.id)
            sys_msg(room.id, f"🎬 Video {who} tarafından kapatıldı.")
        elif cmd == "clearall":
            room.chat = []
            sio.emit("clear-chat", {}, room=room.id)
            sys_msg(room.id, f"🧹 Sohbet geçmişi {who} tarafından temizlendi.")
        elif cmd == "announce":
            sys_msg(room.id, f"📢 DUYURU: {_esc(args[0] if args else '')}")
        elif cmd == "kick":
            target = room.find_by_name(args[0] if args else "")
            if not target:
                sio.emit("system-message",
                         {"message": f"❌ Kullanıcı bulunamadı: {_esc(args[0] if args else '')}"},
                         to=sid)
                return
            if target.sid == leader.sid:
                sio.emit("system-message",
                         {"message": "❌ Kendini atamazsın."}, to=sid)
                return
            _kick(room, leader, target)

    # --- WebRTC sinyalleşmesi ---------------------------------------------
    # Sunucu yalnız taşıyıcı: ses/görüntü doğrudan taraflar arasında akıyor
    # (STUN/TURN istemcide tanımlı), bu yüzden telefona yük bindirmiyor.
    for ev in ("webrtc-offer", "webrtc-answer", "webrtc-ice-candidate",
               "webrtc-hangup", "webrtc-call-request", "webrtc-call-accept",
               "webrtc-call-reject"):
        def relay(sid, data, _ev=ev):
            room_id = str((data or {}).get("roomId", ""))
            if store.get(room_id):
                sio.emit(_ev, data, room=room_id, skip_sid=sid)
        sio.on(ev)(relay)

    # --- ayrılma -----------------------------------------------------------
    @sio.event
    def disconnect(sid, *args):
        sess = {}
        try:
            sess = sio.get_session(sid) or {}
        except Exception:
            pass
        room = store.get(str(sess.get("room_id", "")))
        if not room:
            return
        gone = room.remove_user(sid)
        if not gone:
            return
        sio.emit("user-left",
                 {"username": gone.username, "users": room.usernames()},
                 room=room.id)
        emit_people(room)
        # Ayrılan kişi beklemedeyse oda sonsuza kadar "bekliyor" kalmasın.
        if not room.buffering:
            sio.emit("room-buffering",
                     {"isBuffering": False, "username": gone.username,
                      "activeCount": 0, "currentTime": room.current_time},
                     room=room.id)
        emit_leader_state(room)
        emit_suggestions(room)

    def app_play(room_id, ref, start=0.0):
        """Uygulamanın kendi gönderdiği bölüm (katalog ref'i)."""
        room = store.get(room_id)
        if room is not None and catalog is not None:
            background(play_ref, room, ref, None, start)

    def app_play_link(room_id, url, title="", start=0.0):
        """Uygulamadan bağlantı (birlikte izleme geçmişinden YouTube/link)."""
        room = store.get(room_id)
        if room is not None and str(url).startswith(("http://", "https://")):
            put_video(room, url, [], link_now(url, title), start=start)

    return {"play": app_play, "play_link": app_play_link}
