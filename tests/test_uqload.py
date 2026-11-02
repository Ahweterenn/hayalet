"""_uqload çözücüsü (ağsız): açılmış jwplayer kurulumundan m3u8 + altyazı çıkarımı."""
import pytest

from hayalet.core.extractor import ExtractError
from hayalet.sites import _uqload

# Gerçek uqload embed'inin packer'ı açılınca çıkan jwplayer kurulumunun biçimi.
UNPACKED = (
    'jwplayer("vplayer").setup({sources:[{file:"https://strm5.uqload.vc/hls2/02/'
    '04679/abc_n/master.m3u8?t=TOK&s=1&e=14400&v=1&i=95.7&sp=0"}],'
    'tracks:[{file:"https://strm5.uqload.vc/vtt/02/04679/abc_sli.vtt",kind:"captions"}]});'
)


def test_extract_m3u8_ve_altyazi():
    master, sub = _uqload._extract(UNPACKED)
    assert master.endswith("sp=0") and "master.m3u8" in master
    assert sub and sub.endswith("abc_sli.vtt")


def test_extract_altyazisiz():
    master, sub = _uqload._extract('setup({sources:[{file:"https://x/y.m3u8?a=1"}]})')
    assert master == "https://x/y.m3u8?a=1" and sub is None


def test_extract_m3u8_yoksa_hata():
    with pytest.raises(ExtractError, match="m3u8"):
        _uqload._extract('setup({sources:[{file:"https://x/y.mp4"}]})')
