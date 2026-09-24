"""Oda durumu: kullanıcılar, lider, yasaklar, oynatma konumu.

Burası bilerek SAF tutuldu — socket.io, HTTP, ağ yok. Böylece pytest ile
ağsız denenebiliyor (bkz. tests/test_perde_rooms.py). Ağ tarafı events.py'de.

Perde'nin Node sürümündeki `rooms` sözlüğünün karşılığı; davranış birebir
korundu, bir yer hariç: **liderlik**. Orada IP + kullanıcı adı tahminiyle
bulunuyordu, burada sunucunun ürettiği bir anahtarla. Gerekçe için
`compute_leader`'a bakın.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

# Oda kimliğinde karıştırılması kolay harfler yok (0/O, 1/I/l).
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_room_id(length: int = 6) -> str:
    """Rastgele oda kimliği. Perde'de sabit 'PERDE' kullanılıyordu; davet
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
    # Eklentiden/hayalet'ten gelen istek başlıkları (proxy bunları kullanır).
    headers: dict = field(default_factory=dict)
    sub_headers: dict = field(default_factory=dict)

    # --- kullanıcılar ----------------------------------------------------
    def add_user(self, sid: str, username: str, ip: str,
                 is_host: bool = False) -> User:
        # Yeniden bağlanma: aynı sid'li eski kaydı önce at, yoksa kullanıcı
        # listede iki kez görünüyor.
        self.users = [u for u in self.users if u.sid != sid]
        u = User(sid=sid, username=username, ip=ip, is_host=is_host)
        self.users.append(u)
        return u

    def remove_user(self, sid: str) -> User | None:
        gone = next((u for u in self.users if u.sid == sid), None)
        self.users = [u for u in self.users if u.sid != sid]
        self.buffering = [s for s in self.buffering if s != sid]
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
        }


def compute_leader(room: Room) -> User | None:
    """Odanın lideri (yönetim komutlarını ve senkron kalp atışını o gönderir).

    Perde'nin Node sürümü lideri IP + kullanıcı adı eşleşmesiyle tahmin
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
    Perde'nin normalize adımı burada birebir korundu.
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
    gider; Perde'de de böyleydi ve birlikte izleme için sorun değil."""

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
