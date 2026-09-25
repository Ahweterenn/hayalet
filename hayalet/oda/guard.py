"""Proxy hedefi güvenlik süzgeci (SSRF + DNS-rebinding koruması).

Telefonda bu korumalar PC'dekinden DAHA kritik: sunucu ev ağının içinde
çalışıyor ve davet linkindeki herkes `/api/proxy?url=...` çağırabiliyor.
Süzgeç olmasa uzaktaki bir izleyici modeme, yazıcıya ya da telefonun kendi
yerel servislerine istek attırabilirdi.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

_V4_MAPPED = re.compile(r"^::ffff:(\d+\.\d+\.\d+\.\d+)$", re.I)


def is_blocked_host(hostname: str | None) -> bool:
    """Özel/loopback/link-local hedefleri engeller.

    Eski Node sürümünün regex listesi yerine `ipaddress` kullanılıyor: aynı işi yapar
    ama IPv6 ve sıra dışı gösterimleri (0x7f.1, ::ffff:127.0.0.1, 2130706433)
    da yakalar — regex bunları kaçırıyordu.
    """
    host = (hostname or "").strip().lower().strip("[]")
    if not host:
        return True
    m = _V4_MAPPED.match(host)
    if m:
        host = m.group(1)
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False        # alan adı: asıl kontrol resolve_and_pin'de
    return _ip_blocked(ip)


def _ip_blocked(ip: ipaddress._BaseAddress) -> bool:
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


class BlockedTarget(Exception):
    pass


def resolve_and_pin(hostname: str) -> str:
    """Alan adını çözer, çıkan IP'yi de doğrular ve onu döndürür.

    Neden IP'yi döndürüyoruz: kontrol ile bağlantı arasında ikinci bir DNS
    sorgusu FARKLI (özel) bir adres dönebilir — klasik DNS-rebinding. Bağlantı
    burada doğrulanan IP'ye kurulmalı, `Host` başlığı ve TLS SNI ise özgün
    alan adında kalmalı ki sanal barındırma ve sertifika doğrulaması bozulmasın.
    """
    bare = (hostname or "").strip("[]")
    if not bare:
        raise BlockedTarget("boş host")
    try:
        ip = ipaddress.ip_address(bare)
        if _ip_blocked(ip):
            raise BlockedTarget(f"engelli adres: {bare}")
        return bare
    except ValueError:
        pass
    if is_blocked_host(bare):
        raise BlockedTarget(f"engelli ad: {bare}")
    try:
        info = socket.getaddrinfo(bare, None)
    except socket.gaierror as e:
        raise BlockedTarget(f"çözülemedi: {bare} ({e})")
    for family, _, _, _, sockaddr in info:
        addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _ip_blocked(ip):
            raise BlockedTarget(f"engelli adrese çözüldü: {bare} -> {addr}")
        return addr
    raise BlockedTarget(f"kullanılabilir adres yok: {bare}")


def check_target(url: str) -> str:
    """Proxy hedefini baştan sona doğrular; sabitlenecek IP'yi döndürür."""
    try:
        p = urlparse(url)
    except Exception:
        raise BlockedTarget("ayrıştırılamayan URL")
    if p.scheme not in ("http", "https"):
        raise BlockedTarget("yalnızca http/https")
    if not p.hostname:
        raise BlockedTarget("host yok")
    # Kendi üstüne zincirlenme: url= yine bizim proxy'mizi gösteriyorsa
    # istek N kez kendi üzerinde dolaşır — ucuz bir yük büyütme vektörü.
    if re.search(r"/api/proxy(/|\?|$)", p.path or "", re.I):
        raise BlockedTarget("döngüsel proxy isteği")
    if is_blocked_host(p.hostname):
        raise BlockedTarget(f"engelli hedef: {p.hostname}")
    return resolve_and_pin(p.hostname)
