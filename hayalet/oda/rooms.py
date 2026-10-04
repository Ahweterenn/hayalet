"""Oda durumu: kullanıcılar, lider, yasaklar, oynatma konumu.

Burası bilerek SAF tutuldu — socket.io, HTTP, ağ yok. Böylece pytest ile
ağsız denenebiliyor (bkz. tests/test_oda.py). Ağ tarafı events.py'de.

Eski Node sürümündeki `rooms` sözlüğünün karşılığı; davranış birebir
korundu, bir yer hariç: **liderlik**. Orada IP + kullanıcı adı tahminiyle
bulunuyordu, burada sunucunun ürettiği bir anahtarla. Gerekçe için
`compute_leader`'a bakın.
"""
from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass, field

# Oda kimliğinde karıştırılması kolay harfler yok (0/O, 1/I/l).
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_room_id(length: int = 6) -> str:
    """Rastgele oda kimliği. İlk sürümde sabit bir kimlik kullanılıyordu; davet
    linkini bilen herkes girebildiği için tahmin edilebilir olmamalı."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def new_host_token() -> str:
    return secrets.token_urlsafe(24)


@dataclass
class User:
    sid: str
    username: str
    ip: str
    is_host: bool = False       # sunucuyu çalıştıran cihaz (bkz. compute_leader)
    joined_at: float = field(default_factory=time.time)
    color: str = ""             # profil rengi (#RRGGBB); boşsa istemci addan türetir


# Kumanda (oynat/durdur/sar) kimde: herkeste ya da yalnız ev sahibinde.
CONTROL_MODES = ("all", "host")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# Bekleyen öneri sayısı sınırı: bir misafir listeyi şişiremesin.
_MAX_SUGGESTIONS = 20
# Sonradan katılan son mesajları görsün (sohbet boş açılıyordu).
_MAX_CHAT_HISTORY = 50
# İzleme sırası sınırı.
_MAX_QUEUE = 30
# Tepkiler: yalnız bu emojiler (istemci serbest metin gönderemesin) ve kişi
# başına kısa pencerede sınırlı sayıda (ekran uçan emojiyle dolmasın).
REACTIONS = ("😂", "😍", "😮", "😢", "🔥", "👏")
_REACT_WINDOW = 3.0
_REACT_MAX = 6


def clean_color(value) -> str:
    """Yalnız #RRGGBB kabul edilir; başka her şey boş (CSS'e gidiyor)."""
    v = str(value or "").strip()
    return v if _COLOR_RE.match(v) else ""


@dataclass
class Subtitle:
    url: str
    label: str | None = None

    def as_dict(self) -> dict:
        return {"url": self.url, "label": self.label}


@dataclass
class Room:
    id: str
    video_url: str = ""
    subtitles: list[Subtitle] = field(default_factory=list)
    current_time: float = 0.0
    is_playing: bool = False
    users: list[User] = field(default_factory=list)
    buffering: list[str] = field(default_factory=list)      # sid listesi
    banned_users: list[str] = field(default_factory=list)   # küçük harfli adlar
    banned_ips: list[str] = field(default_factory=list)
    # hayalet'ten gelen istek başlıkları (proxy bunları kullanır).
    headers: dict = field(default_factory=dict)
    sub_headers: dict = field(default_factory=dict)
    control_mode: str = "all"
    # Şu an oynayan: {"kind", "title", "subtitle", "poster", "ref", "hasNext"}.
    now: dict = field(default_factory=dict)
    suggestions: list = field(default_factory=list)
    chat: list = field(default_factory=list)
    # Sıradakiler: {"id", "kind", "ref"|"url", "title", "subtitle", "poster", "by"}.
    queue: list = field(default_factory=list)
    # Tepki sınırı için kişi başına son tepki zamanları (sid -> [zaman]).
    _reacts: dict = field(default_factory=dict)

    # --- kullanıcılar ----------------------------------------------------
    def add_user(self, sid: str, username: str, ip: str,
                 is_host: bool = False, color: str = "") -> User:
        # Yeniden bağlanma: aynı sid'li eski kaydı önce at, yoksa kullanıcı
        # listede iki kez görünüyor.
        self.users = [u for u in self.users if u.sid != sid]
        u = User(sid=sid, username=username, ip=ip, is_host=is_host,
                 color=clean_color(color))
        self.users.append(u)
        return u

    def people(self) -> list[dict]:
        """Kişi listesi (ekranda gösterilen); IP gibi iç bilgi içermez."""
        leader = compute_leader(self)
        return [{"id": u.sid, "name": u.username, "color": u.color,
                 "host": u.is_host, "leader": leader is not None and u.sid == leader.sid}
                for u in self.users]

    # --- yetki -------------------------------------------------------------
    def can_control(self, sid: str) -> bool:
        """Oynat/durdur/sar yetkisi."""
        if self.control_mode == "all":
            return True
        leader = compute_leader(self)
        return leader is not None and leader.sid == sid

    def is_leader(self, sid: str) -> bool:
        leader = compute_leader(self)
        return leader is not None and leader.sid == sid

    def set_control_mode(self, mode: str) -> bool:
        if mode not in CONTROL_MODES:
            return False
        self.control_mode = mode
        return True

    def add_chat(self, message: dict) -> None:
        self.chat.append(message)
        del self.chat[:-_MAX_CHAT_HISTORY]

    # --- öneriler ----------------------------------------------------------
    def add_suggestion(self, by: User, item: dict) -> dict | None:
        """Misafir önerisi. Aynı içerik zaten bekliyorsa yenisi eklenmez."""
        key = item.get("ref") or item.get("url")
        if not key:
            return None
        for s in self.suggestions:
            if (s["item"].get("ref") or s["item"].get("url")) == key:
                return None
        s = {"id": secrets.token_urlsafe(6), "by": by.username,
             "byColor": by.color, "item": item, "at": time.time()}
        self.suggestions.append(s)
        del self.suggestions[:-_MAX_SUGGESTIONS]
        return s

    def take_suggestion(self, sid: str) -> dict | None:
        """Öneriyi listeden çıkarıp döndürür (kabul ya da ret)."""
        s = next((x for x in self.suggestions if x["id"] == sid), None)
        if s:
            self.suggestions = [x for x in self.suggestions if x["id"] != sid]
        return s

    # --- izleme sırası ----------------------------------------------------
    def queue_add(self, by: str, item: dict) -> dict | None:
        """Sıranın sonuna ekler (`by`: ekleyenin ya da önerenin adı). Aynı
        içerik zaten sıradaysa eklenmez."""
        key = item.get("ref") or item.get("url")
        if not key or len(self.queue) >= _MAX_QUEUE:
            return None
        if any((q.get("ref") or q.get("url")) == key for q in self.queue):
            return None
        q = {"id": secrets.token_urlsafe(6), "by": str(by or ""),
             **{k: item[k] for k in ("kind", "ref", "url", "title", "subtitle", "poster")
                if item.get(k)}}
        self.queue.append(q)
        return q

    def queue_take(self, qid: str | None = None) -> dict | None:
        """Sıradan çıkarıp döndürür; qid yoksa baştakini."""
        if not self.queue:
            return None
        q = self.queue[0] if qid is None else next(
            (x for x in self.queue if x["id"] == qid), None)
        if q:
            self.queue = [x for x in self.queue if x["id"] != q["id"]]
        return q

    def queue_move(self, qid: str, delta: int) -> bool:
        i = next((n for n, x in enumerate(self.queue) if x["id"] == qid), None)
        if i is None:
            return False
        j = max(0, min(len(self.queue) - 1, i + int(delta)))
        if i == j:
            return False
        self.queue.insert(j, self.queue.pop(i))
        return True

    # --- tepkiler ---------------------------------------------------------
    def allow_reaction(self, sid: str, emoji: str, now: float | None = None) -> bool:
        if emoji not in REACTIONS:
            return False
        now = time.time() if now is None else now
        recent = [t for t in self._reacts.get(sid, []) if now - t < _REACT_WINDOW]
        if len(recent) >= _REACT_MAX:
            self._reacts[sid] = recent
            return False
        recent.append(now)
        self._reacts[sid] = recent
        return True

    def remove_user(self, sid: str) -> User | None:
        gone = next((u for u in self.users if u.sid == sid), None)
        self.users = [u for u in self.users if u.sid != sid]
        self.buffering = [s for s in self.buffering if s != sid]
        self._reacts.pop(sid, None)
        return gone

    def find_user(self, sid: str) -> User | None:
        return next((u for u in self.users if u.sid == sid), None)

    def find_by_name(self, name: str) -> User | None:
        low = (name or "").strip().lower()
        return next((u for u in self.users if u.username.lower() == low), None)

    def usernames(self) -> list[str]:
        return [u.username for u in self.users]

    # --- yasak -----------------------------------------------------------
    def is_banned(self, username: str, ip: str) -> bool:
        return ((username or "").lower() in self.banned_users
                or (ip or "") in self.banned_ips)

    def ban(self, user: User) -> None:
        if user.username.lower() not in self.banned_users:
            self.banned_users.append(user.username.lower())
        # Tünel/röle arkasında herkes aynı IP'den görünebilir; o durumda IP
        # yasağı yanlışlıkla başkalarını da keser. Sadece gerçekten ayırt
        # edici olduğunda ekle.
        if user.ip and self._ip_is_distinct(user.ip):
            self.banned_ips.append(user.ip)

    def _ip_is_distinct(self, ip: str) -> bool:
        return sum(1 for u in self.users if u.ip == ip) <= 1

    # --- buffering -------------------------------------------------------
    def start_buffering(self, sid: str) -> bool:
        """True dönerse 'oda beklemeye girdi' duyurusu yapılmalı."""
        if sid in self.buffering:
            return False
        self.buffering.append(sid)
        return len(self.buffering) == 1

    def end_buffering(self, sid: str) -> bool:
        """True dönerse 'oda devam ediyor' duyurusu yapılmalı."""
        if sid not in self.buffering:
            return False
        self.buffering = [s for s in self.buffering if s != sid]
        return not self.buffering

    # --- durum ------------------------------------------------------------
    def state(self) -> dict:
        return {
            "videoUrl": self.video_url,
            "subtitles": [s.as_dict() for s in self.subtitles],
            "currentTime": self.current_time,
            "isPlaying": self.is_playing,
            "isBuffering": bool(self.buffering),
            "users": self.usernames(),
            "people": self.people(),
            "controlMode": self.control_mode,
            "now": dict(self.now),
            "chat": list(self.chat),
            "queue": list(self.queue),
        }


def compute_leader(room: Room) -> User | None:
    """Odanın lideri (yönetim komutlarını ve senkron kalp atışını o gönderir).

    Eski Node sürümü lideri IP + kullanıcı adı eşleşmesiyle tahmin
    ediyordu. Telefon sunucusunda bu BOZULUR: bir röle/tünel arkasında bütün
    izleyiciler aynı IP'den gelir ve rastgele biri lider olabilir. Onun yerine
    sunucu açılışta bir anahtar üretiyor, uygulamanın kendi ekranı o anahtarla
    bağlanıyor ve `is_host` işaretini alıyor — tahmin yok.

    Ev sahibi odada değilse eski davranışa düşülür: en erken katılan kalır.
    """
    if not room.users:
        return None
    host = next((u for u in room.users if u.is_host), None)
    if host:
        return host
    return min(room.users, key=lambda u: u.joined_at)


def normalize_subtitles(raw) -> list[Subtitle]:
    """`set-video` yükünü tek biçime indirger.

    İstemci hem ["http://..."] hem [{"url":..., "label":...}] gönderebiliyor;
    Eski sürümün normalize adımı burada birebir korundu.
    """
    out: list[Subtitle] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, str) and item:
            out.append(Subtitle(url=item, label=None))
        elif isinstance(item, dict) and item.get("url"):
            out.append(Subtitle(url=str(item["url"]),
                                label=item.get("label") or None))
    return out


def subtitles_equal(a: list[Subtitle], b: list[Subtitle]) -> bool:
    if len(a) != len(b):
        return False
    return all(x.url == y.url and (x.label or "") == (y.label or "")
               for x, y in zip(a, b))


class RoomStore:
    """Bellekteki oda tablosu. Kalıcı kayıt yok — sunucu kapanınca odalar da
    gider; eski sürümde de böyleydi ve birlikte izleme için sorun değil."""

    def __init__(self) -> None:
        self._rooms: dict[str, Room] = {}

    def get_or_create(self, room_id: str) -> Room:
        if room_id not in self._rooms:
            self._rooms[room_id] = Room(id=room_id)
        return self._rooms[room_id]

    def get(self, room_id: str) -> Room | None:
        return self._rooms.get(room_id)

    def all(self) -> dict[str, Room]:
        return dict(self._rooms)
