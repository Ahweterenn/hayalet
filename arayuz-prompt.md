# hayalet — arayüz tasarımı için hazır prompt

Aşağıdaki bloğun tamamını Gemini / GPT / Claude'a yapıştır. Sonunda ne istediğini
söyleyen bölüm var; oradan istediğini kısaltabilir ya da çoğaltabilirsin.

---

Sen kıdemli bir mobil ürün tasarımcısısın. Android için bir video uygulamasının
arayüzünü baştan tasarlayacaksın. Aşağıda ürünün ne olduğu, hangi teknik kısıtlar
altında çalıştığı, şu an nasıl göründüğü ve nereye gitmek istediğim yazıyor.
Uydurma özellik ekleme; ancak "bu veriyi de göstermek istersen şu gerekir" diye
not düşebilirsin.

## ÜRÜN

"hayalet" — Türkçe dizi/film izleme ve **çevrimdışı indirme** uygulaması.
Reklamsız, hesapsız, tamamen kişisel kullanım için. İçerik iki kaynak siteden
geliyor (bir dizi sitesi + bir film/dizi sitesi); kullanıcı için bu ayrım büyük
ölçüde görünmez olmalı, sonuçlar tek birleşik liste halinde geliyor.

Ana kullanım döngüsü üç şey: **bul → izle** ya da **bul → indir → çevrimdışı izle**.

## TEKNİK KISITLAR (tasarımı bunlar şekillendirir)

- Saf Android, **Java**. XML layout dosyası YOK — tüm arayüz kodla kuruluyor.
  Bu yüzden tasarım, tekrar kullanılabilir küçük bileşenler halinde tarif
  edilmeli (bir "tasarım sistemi" olarak), sayfa sayfa piksel resmi olarak değil.
- Video oynatma **Media3 / ExoPlayer** ile, ayrı bir tam ekran Activity'de.
- Arka uçta (arama, kaynak çözme, indirme) gömülü bir Python katmanı var. Önemli
  sonucu: **her ağ işlemi yavaş.** Arama 5–20 saniye, bir bölümün kaynağını çözmek
  ~10 saniye, bir bölümü indirmek saatler sürebiliyor. Yani her ekranın
  "yükleniyor" ve "hata" durumu birinci sınıf tasarlanmalı, sonradan eklenen bir
  spinner olmamalı.
- Hedef cihaz: 1080 x 2340 px, koyu tema, tek elle kullanım. Arayüz dili **Türkçe**.
- İndirilenler tamamen çevrimdışı oynuyor (yerel dosyadan) — uçakta/internetsiz
  çalışması gereken tek akış bu.

## MEVCUT TASARIM SİSTEMİ (korunacak, genişletilecek)

Palet (uygulamanın logosundan türetildi — çay bardağından çıkan hayalet, lacivert
zemin + çay turuncusu):

- Zemin `#121620`
- Yüzey / kart `#1B2130`
- Yükseltilmiş yüzey `#242C3E`
- Vurgu (turuncu) `#EA5814`
- Metin `#F2F4F7`
- Soluk metin `#8B93A5`

Var olan bileşenler:

- `primary(label)` — dolu turuncu hap buton (birincil eylem), yükseklik 34dp,
  köşe 18dp, büyük harfe çevirmiyor
- `ghost(label)` — çerçeveli hap buton (ikincil eylem)
- `card()` — 14dp yuvarlak, `#1B2130` zeminli yatay kart
- `title` 15.5sp / `sub` 12.5sp soluk / `header` 11.5sp büyük harf harf-aralıklı
- `Row` — **tüm listeler tek satır bileşeniyle çiziliyor**: başlık + alt başlık +
  sağda rozet + isteğe bağlı ilerleme çubuğu + en fazla iki buton (biri turuncu,
  biri çerçeveli). Arama sonucu, bölüm listesi, indirilenler — hepsi bu.

## ŞU ANKİ EKRANLAR (üç görünüm, tek Activity)

1. **Arama / sonuçlar** — üstte logo + "hayalet" yazısı + sağda [İndirilenler]
   butonu. Altında yuvarlak arama alanı + turuncu [Ara]. Altında durum satırı
   ("Aramak için bir şey yaz." / "12 sonuç" / "Hata: ..."). Altında sonuç kartları:
   başlık, alt satırda "Film · hdfilmcehennemi" gibi tür + kaynak.
2. **Bölüm listesi** — üstte sezon hap şeridi (Sezon 1 / Sezon 2 / ⬇ Sezonu indir),
   altında bölüm kartları, her satırda [İzle] [İndir]. Seçilen şey bir filmse
   sezon şeridi yok, tek satır var.
3. **İndirilenler** — üstte özet ("3 indirilmiş · 1 sürüyor"), diziye göre
   gruplanmış satırlar. Süren iş: "%4 · 73/1583 parça · 480p" + ince turuncu
   ilerleme çubuğu + [Durdur]. Durmuş/hatalı iş: [Devam et] [Kaldır]. Bitmiş iş:
   [İzle] + boyut.
4. **Oynatıcı** (ayrı tam ekran) — üst şeritte başlık + [Kalite] [Ses] [Altyazı]
   butonları (yalnızca birden fazla seçenek varsa çıkıyor), kontrollerle birlikte
   görünüp kayboluyor. Kaldığı yerden devam ediyor, bölüm bitince sonraki bölüme
   geçebiliyor.
5. **Kalite seçimi** — indirmeye basınca çıkan liste. Kalite seçenekleri sabit
   değil, **kaynağın gerçekten sunduğu** varyantlar ("1080p ~3.0 Mbps",
   "480p ~1.3 Mbps"); bazı yapımda tek seçenek var, bazısında dört.

**Bu arayüzün asıl sorunu:** uygulama açılınca kullanıcıyı **boş bir arama kutusu**
karşılıyor. Ne izleyeceğini bilmeyen birine hiçbir şey sunmuyor. Netflix'i açan
insan gezinmeye başlar, burada önce bir şey yazmak zorunda.

## ELDEKİ VERİ (tasarım bunun ötesine geçemez)

Şu an her yapım için elimizde olanlar:

- **Ad** — bazen "Türkçe Ad - Original Title" biçiminde tek bir uzun metin
  (ör. "Yüzüklerin Efendisi Kralın Dönüşü - The Lord of the Rings: The Return of
  the King"). Uzun; satırda kırpma/iki satır kararı gerekiyor.
- **Tür** — Dizi / Film
- **Kaynak site** — iki siteden hangisi
- **Diziyse**: sezon + bölüm numaraları (bölüm adı çoğu zaman yok)
- **Kaynak çözüldüğünde**: kalite varyantları, ses parçaları (Türkçe dublaj +
  orijinal), Türkçe altyazı
- **İndirme işi**: durum (sırada / çözülüyor / indiriliyor / bitti / hata /
  durduruldu), yüzde, inen parça / toplam parça, seçilen kalite, boyut

**Şu an OLMAYAN ama eklenebilecek** (kaynak sayfalarda mevcut, kazınması gereken):
poster görseli, yapım yılı, IMDb puanı, özet metni, tür etiketleri (aksiyon,
komedi...), kategori sayfaları.

## GİTMEK İSTEDİĞİM YER

- **Gerçek bir ana sayfa**: raflar halinde keşif — "Kaldığın yerden devam",
  "Son eklenenler", "Popüler", "Vizyondakiler".
- **Alt sekmeler**: Ana sayfa · Diziler · Filmler · İndirilenler. Dizi ve film
  kendi sayfasında, kendi düzeniyle (dizide sezon/bölüm mantığı var, filmde yok —
  ikisi aynı listede iyi durmuyor).
- **Kategori / tür sayfaları**: türe, yıla, puana göre gezinme.
- **Sonuç satırında poster + yıl + puan** — şu an sadece isim var, "Spider-Man"
  arayınca çıkan 10 sonuçtan hangisi olduğunu ayırt etmek zor.
- **Ayarlar sayfası**: indirme klasörü, varsayılan kalite, altyazı görünümü,
  önbellek temizleme gibi şeyler için bir yer (şu an hiç yok).
- Arama kalacak ama artık uygulamanın **tek** yolu değil, bir yol olacak.

## SENDEN İSTEDİĞİM

1. **Gezinme haritası** — hangi ekrandan hangisine gidiliyor, geri tuşu nereye
   düşüyor, alt sekmeler arasında geçince durum korunuyor mu.
2. **Ekran ekran düzen** — her ekran için:
   - dikey ASCII wireframe (telefon oranında, tepeden aşağı)
   - içindeki bileşenlerin listesi ve hangi veriyi gösterdiği
   - **boş / yükleniyor / hata / çevrimdışı** varyantları (bu ürün için kritik,
     ağ işlemleri saniyeler sürüyor)
   - o ekranda kullanıcının yapabileceği eylemler ve nereye götürdükleri
   Ekranlar: Ana sayfa, Diziler, Filmler, Kategori/tür gezinme, Arama + sonuçlar,
   Yapım detayı (dizi: sezon/bölüm; film: tek eylem), İndirilenler, Ayarlar,
   Oynatıcı üst/alt kontrol şeridi.
3. **Tasarım sistemine eklenmesi gereken yeni bileşenler** — mevcut liste
   (primary/ghost/card/title/sub/header/Row) yeni ekranlara yetmiyor. Neye
   ihtiyaç var, ne işe yarıyor, hangi ölçülerle: yatay kaydırmalı raf, poster
   kartı, alt sekme çubuğu, filtre çipi, iskelet (skeleton) yükleme durumu,
   bölüm satırı, "devam et" ilerleme şeridi vb.
4. **Ölçek kararları** — kenar boşluğu, kartlar arası aralık, poster en-boy oranı,
   köşe yarıçapı, tipografi basamakları (dp/sp cinsinden somut sayılar ver;
   mevcut değerlerle uyumlu kalsın).
5. **Aşamalı uygulama planı** — hepsini bir seferde yapmayacağım. Hangi sırayla
   yapılırsa her adımda kullanılabilir bir uygulama kalır? İlk adım en az emekle
   en çok fark yaratan olsun.
6. Emin olmadığın yerlerde **varsayımını açıkça yaz** ("posterin 2:3 olduğunu
   varsaydım"), soru sorup beklemek yerine varsayımla devam et.

Türkçe yaz. Kod isteme yok — tasarım ve gerekçe istiyorum; her önemli kararın
yanına neden öyle olduğunu bir cümleyle ekle.

---

# REVİZYON PROMPTU (ilk görsel çıktıktan sonra aynı sohbete yapıştır)

Tasarım güzel oldu, düzeni ve paletini koruyalım. Ama uygulamanın **gerçek
verisiyle** karşılaştırınca bir kısmı elimizde olmayan bilgiye dayanıyor, bir
kısmı da bizde var olan önemli şeyleri atlamış. Aşağıdakileri uygulayıp aynı
9 ekranlık görseli **yeniden üret**.

## ÖNCE ŞU KURAL

Elimizde OLMAYAN veriyi ekranın merkezine koyma. Şu an gerçekten sahip olduğumuz
tek şey: **yapım adı**, **tür (Dizi/Film)**, **kaynak site**, diziyse **sezon ve
bölüm numaraları**; kaynak çözüldüğünde **kalite varyantları**, **ses parçaları
(Türkçe dublaj + orijinal)**, **Türkçe altyazı**; indirme işi için **durum,
yüzde, inen/toplam parça, kalite, boyut**.

Poster ve yıl muhtemelen eklenebilir (kaynak listede duruyor olabilir, henüz
doğrulanmadı). IMDb puanı, özet, oyuncu, yönetmen, bölüm adı, tür etiketi:
**şu an YOK**, her biri ayrı sayfa kazıması demek.

Bu yüzden tasarımı iki katmanlı yap: **poster ve puan olmadan da bozulmayan bir
temel**, üstüne "veri gelirse şuraya oturur" diye işaretlenmiş yerler.

## ÇIKAR / GERİYE AL

1. **IMDb puan rozetlerini listelerden kaldır.** Elimizde yok ve liste için her
   yapımın detay sayfasını ayrı ayrı çekmek gerekir — 10 sonuçlu bir arama 10 kat
   yavaşlar. Puan yalnızca yapım detayı ekranında, opsiyonel bir satır olarak
   kalsın.
2. **"5 sezon · 62 bölüm" bilgisini liste satırlarından çıkar.** Bölüm sayısını
   öğrenmek dizinin sayfasını çekmeyi gerektiriyor (saniyeler); listede
   gösterilemez. Detay ekranında kalsın.
3. **Bölüm adlarını kaldır** ("Pilot", "The Cat's in the Bag.."). Kaynaklarımız
   bölüm adı vermiyor, sadece numara. Bölüm satırı yalnız "3. Bölüm" ile de
   dolu ve dengeli görünmeli.
4. **Özet / Yönetmen / Oyuncular bloklarını "sonraki aşama" olarak işaretle**,
   detay ekranında yer tutucu olarak göster ama ana kompozisyonu onlara dayama.
5. **Ayarlardan "Eşzamanlı indirme sayısı"nı kaldır.** İndirici bilerek tek işçi
   + sıralı kuyruk: paralel indirme bandı bölüştürüp ikisini de geciktiriyor,
   sırayla inince ilk bölüm çok daha erken izlenebiliyor.
6. **Ayarlardan "İndirilenleri tara"yı kaldır.** Liste zaten her açılışta diskten
   okunuyor.
7. **Oynatıcıdan Cast (yayınla) ikonunu kaldır.** Desteklenmiyor. Yanındaki
   PiP (küçük ekran) ikonu KALSIN, o destekleniyor.
8. **İndirilenler başlığındaki "128 GB Kullanılıyor" rakamını** uygulamanın kendi
   kapladığı alanı gösterecek şekilde küçült/yeniden yaz (birkaç GB mertebesinde).

## EKLE (bizde var, tasarımda yok)

1. **"Sezonu indir"** — dizi detayında, sezon çiplerinin yanında toplu indirme
   eylemi. Mevcut ve en çok kullanılan özelliklerden biri.
2. **Duraklamış / hatalı indirme durumu** — indirilenler satırında sadece
   [Durdur] değil, **[Devam et]** ve **[Kaldır]** da olmalı. Yarım kalan indirme
   silinmiyor, kaldığı yerden sürüyor; arayüzde görünmezse bu özellik kullanılamaz.
   Üç ayrı satır durumu çiz: indiriliyor / duraklamış-hatalı / bitmiş.
3. **Yükleniyor (iskelet) hâli — her liste ve raf için.** Bu üründe en kritik
   eksik: arama 5–20 saniye, bir bölümün kaynağını çözmek ~10 saniye sürüyor.
   Kullanıcı zamanının ciddi kısmını beklerken geçirecek. Arama için ayrıca
   "Aranıyor… bu 5–20 saniye sürebilir" gibi beklentiyi yöneten bir metin.
4. **Hata hâli + [Tekrar dene]** — arama, raf yükleme ve kaynak çözme için.
5. **Çevrimdışı hâli** — Ana/Diziler/Filmler sekmelerinde "İnternet yok,
   indirilenlerden izleyebilirsin" + İndirilenler'e kestirme. İndirilenler sekmesi
   çevrimdışı tam çalışıyor, bunu görsel olarak belli et.
6. **Ayarlar'a giriş yolu** — şu an Ayarlar ekranı var ama hiçbir yerden
   erişilemiyor (alt sekmeler: Ana/Diziler/Filmler/İndirilenler). Ana sayfa
   başlığına bir ⚙ ikonu koy.
7. **Kaynak site bilgisi** — kullanıcıdan gizlemek doğru, ama bir yapım
   çalışmadığında hangi kaynaktan geldiği teşhis için lazım. Yapım detayında
   küçük, soluk tek satır yeter.

## DÜZELT

1. **Puan rozeti çift görünüyor** — hem posterin üstünde hem satırın sağında.
   (Zaten kaldırılıyor, ama aynı hatayı başka rozetlerde tekrarlama: bir bilgi,
   bir yer.)
2. **"Akkiyon" → "Aksiyon"** (kategori ekranı).
3. **Yapım adları gerçekte uzun ve iki dilli**: örneğin "Yüzüklerin Efendisi
   Kralın Dönüşü - The Lord of the Rings: The Return of the King" tek bir metin
   olarak geliyor. Tasarımdaki tüm adlar kısa ve tek dilli; kart ve satırların
   bu uzunlukta bozulmadığını göster (iki satır + kırpma kuralı belirle,
   Türkçe kısmı öne al).
4. **Kalite seçimi çelişkili**: Ayarlarda "varsayılan kalite" var ama akışta her
   indirmede kalite soruluyor. Birini seç ve tutarlı çiz — önerim: varsayılan
   sessizce uygulansın, satırda küçük "480p" etiketi görünsün, uzun basınca
   kalite diyaloğu açılsın.

## ÇIKTI

Aynı biçimde, 9 ekranlık tek görsel olarak yeniden üret. Ek olarak, değişen her
karar için tek cümlelik gerekçe listesi ver.

---

# TİPOGRAFİK POSTER KARTI PROMPTU

Karar: poster görseli kazımıyoruz (görsel yükleyici + disk önbelleği + her raf
açılışında onlarca ağ isteği demek; çevrimdışıyken de raflar boş kalırdı).
Yerine adın kendisini kapak gibi gösteren tipografik kart. Aşağıdaki prompt bunu
görselleştirtmek için.

---

# BİRLEŞİK / SIFIRDAN PROMPT (bağlamı olmayan yeni bir AI için)

Önceki turlarda beğenilen her karar + tipografik kart kararı tek metinde
toplandı. Hiçbir ön bilgi gerektirmez; tek başına yapıştırılır.
(Metnin tamamı aşağıda — sohbette de aynısı verildi.)
