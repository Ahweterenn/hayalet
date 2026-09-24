"""Perde: birlikte izleme sunucusu (Python'a taşınmış hâli).

Özgün hâli Node.js'te çalışıyor (github.com/Ahweterenn/Perde). Burası aynı
socket.io sözleşmesini konuşan Python karşılığı; amaç odayı açan kişinin PC
yerine telefonundan sunucu olabilmesi.

Şu an yalnızca `selftest` dolu — telefonda yığının gerçekten ayağa kalktığını
doğrulamak için. Oda/senkron mantığı ve proxy sonraki fazlarda gelecek
(bkz. depo kökündeki perde-plan.md).
"""
