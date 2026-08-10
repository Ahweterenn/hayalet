"""safe_filename saf-mantık testleri (ağsız).

Windows'ta yasak bir karakter kaçarsa indirme, dosya oluşturma aşamasında
sessizce patlar — bu basit regex'in davranışı burada sabitlenir.
"""
from __future__ import annotations

from hayalet.core.utils import safe_filename

_FORBIDDEN = '<>:"/\\|?*'


def test_safe_filename_strips_all_forbidden_characters():
    name = 'Dizi<Adı>:"Test"/Bölüm\\1|2?3*4'
    result = safe_filename(name)
    assert not any(c in result for c in _FORBIDDEN)


def test_safe_filename_strips_control_characters():
    name = "Dizi\x00Adı\x1fTest"
    result = safe_filename(name)
    assert "\x00" not in result and "\x1f" not in result


def test_safe_filename_collapses_and_strips_whitespace():
    assert safe_filename("  Dizi   Adı   ") == "Dizi Adı"


def test_safe_filename_empty_input_falls_back_to_video():
    assert safe_filename("") == "video"
    assert safe_filename(None) == "video"
    assert safe_filename("   ") == "video"


def test_safe_filename_preserves_turkish_characters():
    # Türkçe karakterler dosya adında geçerli — regex bunları temizlememeli.
    assert safe_filename("Kuruluş Şükrü Çöl Öğüt") == "Kuruluş Şükrü Çöl Öğüt"


def test_safe_filename_typical_episode_title():
    assert safe_filename("Breaking Bad - S01E01") == "Breaking Bad - S01E01"
