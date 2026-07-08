"""Cihaz/tarayıcı kimliği havuzu — her çalıştırmada rastgele biri seçilir.

Amaç: bu aracı çalıştıran herkesin sabit, tek bir User-Agent + TLS imzası
göndermesini önlemek (bkz. yapılcaklar.txt "ban önlemi"). Her girdide
impersonate profili ile User-Agent aynı tarayıcı/cihaz ailesinden — tutarsız
bir kombinasyon (ör. iPhone UA + masaüstü Chrome TLS imzası) gerçek bir
cihazdan daha şüpheli görünür, o yüzden ikisi birlikte tanımlanır.
"""
from __future__ import annotations

import random

PERSONAS = [
    {
        "impersonate": "chrome",
        "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        "label": "Windows · Chrome",
    },
    {
        "impersonate": "edge",
        "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"),
        "label": "Windows · Edge",
    },
    {
        "impersonate": "firefox",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
        "label": "Windows · Firefox",
    },
    {
        "impersonate": "safari",
        "user_agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                       "(KHTML, like Gecko) Version/18.0 Safari/605.1.15"),
        "label": "macOS · Safari",
    },
    {
        "impersonate": "chrome_android",
        "user_agent": ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"),
        "label": "Android · Chrome",
    },
    {
        "impersonate": "safari_ios",
        "user_agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
                       "Mobile/15E148 Safari/604.1"),
        "label": "iPhone · Safari",
    },
]


def random_persona() -> dict:
    return random.choice(PERSONAS)
