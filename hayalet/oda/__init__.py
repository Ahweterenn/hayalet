"""Birlikte izleme odası: sunucu, senkron, proxy ve tünel.

İlk sürümü Node.js ile yazılmıştı. Burası aynı
socket.io sözleşmesini konuşan Python karşılığı; amaç odayı açan kişinin PC
yerine telefonundan sunucu olabilmesi.

Şu an yalnızca `selftest` dolu — telefonda yığının gerçekten ayağa kalktığını
doğrulamak için. Oda/senkron mantığı ve proxy sonraki fazlarda gelecek
(bkz. depo kökündeki oda-notlari.md).
"""
