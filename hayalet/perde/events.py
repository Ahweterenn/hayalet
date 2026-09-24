"""socket.io olay işleyicileri.

Olay adları ve yükleri Perde'nin Node sürümüyle BİREBİR aynı olmak zorunda —
istemci (`public/room.js`) değişmeden kullanılıyor. Node'daki
`socket.to(room)` "gönderen hariç" demek; python-socketio karşılığı
`skip_sid=sid`. Bu ayrım kritik: `play`/`pause`/`seek` gönderene geri
dönerse oynatıcı kendi olayını yeniden işleyip sonsuz döngüye giriyor.
"""
from __future__ import annotations

import html
import time

from hayalet.perde import rooms as R


def _esc(s) -> str:
    return html.escape(str(s or ""), quote=True)


def _client_ip(environ: dict) -> str:
    """Röle/tünel arkasında gerçek istemci X-Forwarded-For'un ilk parçasıdır."""
    xff = environ.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return environ.get("HTTP_CF_CONNECTING_IP") or environ.get("REMOTE_ADDR", "")


def register(sio, store: R.RoomStore, host_token: str) -> None:
    """Tüm olayları verilen socket.io sunucusuna bağlar."""

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

    def sys_msg(room_id, message):
        sio.emit("system-message", {"message": message}, room=room_id)

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
        room.add_user(sid, username, ip, is_host=is_host)
        sio.save_session(sid, {"room_id": room_id, "username": username})

        sio.emit("room-state", room.state(), to=sid)
        sio.emit("user-joined",
                 {"username": username, "users": room.usernames()},
                 room=room_id, skip_sid=sid)
        sio.emit("room-users", {"users": room.usernames()}, room=room_id)
        emit_leader_state(room)

    # --- video kaynağı ---------------------------------------------------
    @sio.on("set-video")
    def set_video(sid, data):
        if not isinstance(data, dict) or not data.get("roomId"):
            return
        room = store.get(str(data["roomId"]))
        if not room:
            return
        video_url = data.get("videoUrl") or data.get("url") or ""
        subs = R.normalize_subtitles(data.get("subtitles"))

        if data.get("headers"):
            room.headers = {str(k).lower(): v for k, v in data["headers"].items()}
        if data.get("subHeaders"):
            room.sub_headers = data["subHeaders"]

        # Aynı video+altyazı yeniden gelirse oynatmayı SIFIRLAMA — eklenti
        # veya yeniden bağlanma aynı yükü tekrar gönderebiliyor.
        if (str(room.video_url or "") == str(video_url or "")
                and R.subtitles_equal(room.subtitles, subs)):
            return

        room.video_url = video_url
        room.subtitles = subs
        room.current_time = 0.0
        room.is_playing = False
        sio.emit("video-changed",
                 {"videoUrl": room.video_url,
                  "subtitles": [s.as_dict() for s in subs]}, room=room.id)

    # --- oynatma denetimi -------------------------------------------------
    def _playback(event, playing):
        def handler(sid, data):
            if not isinstance(data, dict):
                return
            room = store.get(str(data.get("roomId", "")))
            if not room:
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
        sio.emit("chat-message",
                 {"username": data.get("username") or "Misafir",
                  "message": data.get("message") or "",
                  "time": time.strftime("%H:%M")}, room=room.id)

    @sio.on("change-nick")
    def change_nick(sid, data):
        room = store.get(str((data or {}).get("roomId", "")))
        if not room:
            return
        u = room.find_user(sid)
        new = str((data or {}).get("newName") or "").strip()[:40]
        if not u or not new:
            return
        old, u.username = u.username, new
        sio.emit("room-users", {"users": room.usernames()}, room=room.id)
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
            room.ban(target)
            sio.emit("kicked", {"by": leader.username}, to=target.sid)
            room.remove_user(target.sid)
            sio.emit("user-left",
                     {"username": target.username, "users": room.usernames()},
                     room=room.id)
            sys_msg(room.id,
                    f"👢 <b>{_esc(target.username)}</b> adlı kullanıcı, {who} "
                    f"tarafından odadan atıldı ve girişleri yasaklandı.")
            sio.disconnect(target.sid)
            emit_leader_state(room)

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
        sio.emit("room-users", {"users": room.usernames()}, room=room.id)
        # Ayrılan kişi beklemedeyse oda sonsuza kadar "bekliyor" kalmasın.
        if not room.buffering:
            sio.emit("room-buffering",
                     {"isBuffering": False, "username": gone.username,
                      "activeCount": 0, "currentTime": room.current_time},
                     room=room.id)
        emit_leader_state(room)
