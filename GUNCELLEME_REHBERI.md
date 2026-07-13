# EVENTIX Güncelleme ve Kurulum Rehberi

Bu paket, mevcut çalışan uygulamanın tasarımını, fontlarını, renklerini, giriş yapısını, mevcut modüllerini ve mevcut kayıt anahtarlarını değiştirmeden hazırlanmıştır. Değişiklikler yalnızca yeni İhale Takip Sistemi, ana sayfa panelleri ve bu yeni alanlara veri sağlayan ayrı senkronizasyon katmanıyla sınırlıdır.

## 1. Önemli teknik tespit

Yüklenen sürümde mevcut organizasyon, teklif, sözleşme, fatura ve diğer kayıtlar Firebase’den değil tarayıcının `localStorage` alanından okunmaktadır. `firebase-admin` bağımlılığı bulunmasına rağmen eski modüller Firebase’e bağlı değildir.

Bu güncellemede mevcut kayıtlar Firebase’e taşınmamış, mevcut veri akışı değiştirilmemiştir. Firebase yalnızca yeni EKAP ve ekonomi verilerinin ortak bir kaynaktan okunması için kullanılır. Böylece mevcut çalışan kayıtlarınız ve ekranlarınız etkilenmez.

## 2. Yapılan değişiklikler

1. Sol menüye **İhale Takip Sistemi** eklendi.
2. İhale listesine şu alanlar eklendi:
   - EKAP No
   - Kurum
   - İhale Adı
   - Yayın Tarihi
   - Son Başvuru Tarihi
   - Yaklaşık Maliyet
   - Uygunluk
   - Durum
   - Kaynak bağlantısı
   - Açıklama ve takip notları
3. Ana sayfaya **Canlı Ekonomi Paneli** eklendi.
4. Ana sayfadaki **Fatura & Tahsilat Özeti** kutusu küçültüldü.
5. Ana sayfadaki **Kritik Riskler & Uyarılar** kutusu kaldırıldı.
6. **Yaklaşan Organizasyonlar** kutusu, kaldırılan risk kutusunun bulunduğu alt sağ alana taşındı.
7. Ana sayfaya sistem verileriyle hesaplanan **EKAP RADARI** eklendi.
8. Ana sayfaya sistem verileriyle hesaplanan **BAŞARI PANOSU** eklendi.
9. GitHub Actions üzerinden:
   - Her gün Türkiye saatiyle 12.00’de ihale senkronizasyonu,
   - Her 15 dakikada bir ekonomi verisi senkronizasyonu
   için ayrı ve güvenli bir otomasyon eklendi.
10. Veri kaynağı tanımlı değilse sistem hata vermez, işlemi atlar ve sahte veri göstermez.

## 3. EKAP Radarı hesaplama yöntemi

- **Bugün Yayınlanan:** Yayın tarihi bugünün tarihi olan ihale sayısıdır.
- **Uygun İhale:** Yetkili veri kaynağından gelen ve anahtar kelime eşleşmesi bulunan ihalelerdir.
- **Yaklaşan Son Tarih:** Son başvuru tarihine 0–7 gün kalan ihalelerdir.

Varsayılan uygunluk anahtar kelimeleri organizasyon şirketinin faaliyet alanlarına göre hazırlanmıştır. GitHub Secrets içindeki `EKAP_KEYWORDS` değeriyle değiştirilebilir.

## 4. Başarı Panosu hesaplama yöntemi

- **Bu Ay Organizasyon:** Başlangıç tarihi veya kayıt tarihi içinde bulunulan ayda olan organizasyon sayısıdır.
- **Tahsilat:** Bu ay kesilen faturaların tahsil edilen tutarının toplam fatura tutarına oranıdır.
- **Karlılık:** Bu ayın gelir ve gider kayıtlarından hesaplanan yaklaşık kâr marjıdır.
- **Yeni Müşteri:** Bu ay CRM’e eklenen müşteri sayısıdır.

Bu değerler sabit değildir; mevcut sistem kayıtları değiştikçe otomatik güncellenir.

## 5. Kuruluma başlamadan önce

### 5.1. Yedek alın

1. GitHub repository sayfanızı açın.
2. **Code** düğmesine basın.
3. **Download ZIP** seçeneğiyle mevcut sürümü bilgisayarınıza indirin.
4. Dosyayı `EVENTIX-ESKI-SURUM` adıyla saklayın.

### 5.2. Güvenlik işlemi

Yüklenen eski paketteki `.streamlit/secrets.toml.example` dosyasında gerçek anahtara benzeyen Firebase özel anahtar bilgileri bulunuyordu. Güncel pakette bu değerler kaldırılmış ve yerlerine örnek alanlar konulmuştur.

Firebase Console üzerinden eski servis hesabı özel anahtarını iptal edip yeni bir anahtar oluşturmanız önerilir. Eski anahtar daha önce GitHub’a yüklendiyse yalnızca dosyayı silmek yeterli değildir; anahtar mutlaka Firebase/Google Cloud tarafında iptal edilmelidir.

## 6. Güncel dosyaları GitHub’a yükleme

1. Güncel ZIP dosyasını bilgisayarınıza indirin ve klasöre çıkarın.
2. GitHub repository sayfanızı açın.
3. **Add file** düğmesine basın.
4. **Upload files** seçeneğini seçin.
5. Güncel klasörün içindeki bütün dosya ve klasörleri yükleme alanına sürükleyin.
6. Aynı isimli dosyalar için GitHub güncelleme yapacaktır.
7. Commit açıklamasına `İhale takip sistemi ve dashboard güncellemesi` yazın.
8. **Commit changes** düğmesine basın.

Mevcut repository’yi silmeyin ve yeni repository açmayın. Böylece Streamlit bağlantısı bozulmaz.

## 7. Streamlit Secrets ayarları

Streamlit Cloud’da uygulamanızı açın:

1. Sağ alt veya sağ üst menüden **Settings** bölümünü açın.
2. **Secrets** alanına girin.
3. Mevcut Gemini ayarlarını silmeyin.
4. Aşağıdaki Firebase alanlarının bulunduğunu kontrol edin:
   - `FIREBASE_ENABLED = true`
   - `FIREBASE_SERVICE_ACCOUNT_JSON`
5. `FIREBASE_SERVICE_ACCOUNT_JSON` alanına yeni oluşturduğunuz servis hesabının eksiksiz JSON içeriğini yapıştırın.
6. Kaydedin ve uygulamayı yeniden başlatın.

Gerçek özel anahtar hiçbir zaman repository içindeki `.streamlit/secrets.toml.example` dosyasına yazılmamalıdır.

## 8. GitHub Actions Secrets ayarları

GitHub repository içinde:

1. **Settings** bölümüne girin.
2. Sol menüden **Secrets and variables** seçeneğini açın.
3. **Actions** seçeneğine girin.
4. **New repository secret** düğmesine basın.
5. Aşağıdaki secret’ları tek tek ekleyin.

### Zorunlu

- `FIREBASE_SERVICE_ACCOUNT_JSON`: Yeni Firebase servis hesabının tam JSON içeriği.
- `EKAP_FEED_URL`: Yetkili/izinli ihale veri kaynağının JSON adresi.
- `ECONOMY_FEED_URL`: Yetkili ekonomi veri kaynağının JSON adresi.

### İsteğe bağlı

- `EKAP_FEED_TOKEN`: İhale veri kaynağı erişim anahtarı gerektiriyorsa.
- `ECONOMY_FEED_TOKEN`: Ekonomi veri kaynağı erişim anahtarı gerektiriyorsa.
- `EKAP_KEYWORDS`: Uygun ihale eşleştirmesinde kullanılacak virgülle ayrılmış kelimeler.

## 9. Veri kaynağı hakkında önemli açıklama

Bu paket, doğrulanmamış bir internet sayfasını izinsiz şekilde kazımaz. Gerçek otomatik tarama için EKAP’tan veya yetkili bir hizmet sağlayıcıdan izinli bir JSON veri akışı gerekir.

`EKAP_FEED_URL` tanımlanmadığında:

- Günlük 12.00 otomasyonu çalışır,
- Veri kaynağının tanımlı olmadığını görür,
- Güvenli biçimde işlemi atlar,
- Mevcut uygulamayı etkilemez,
- Sahte ihale eklemez.

Aynı davranış ekonomi veri kaynağı için de geçerlidir.

## 10. İlk manuel test

GitHub’da:

1. Repository içindeki **Actions** sekmesini açın.
2. Sol taraftan **EVENTIX EKAP and Economy Sync** iş akışını seçin.
3. **Run workflow** düğmesine basın.
4. Tekrar **Run workflow** seçeneğine basın.
5. İşlem tamamlandığında yeşil onay işareti görünmelidir.

Veri kaynakları henüz eklenmediyse işlem hata vermeden “atlandı” mesajıyla tamamlanır.

## 11. Uygulamada kontrol edilecek alanlar

Streamlit uygulamasını normal bağlantısından açın ve sırasıyla kontrol edin:

1. Kullanıcı girişi eskisi gibi çalışıyor mu?
2. Sol menüde **İhale Takip Sistemi** görünüyor mu?
3. Mevcut organizasyon, teklif, sözleşme ve fatura kayıtları duruyor mu?
4. Ana sayfada **Canlı Ekonomi Paneli** görünüyor mu?
5. **EKAP RADARI** ve **BAŞARI PANOSU** görünüyor mu?
6. **Kritik Riskler & Uyarılar** alt kutusu kaldırılmış mı?
7. **Yaklaşan Organizasyonlar** alt sağ alana taşınmış mı?
8. **Fatura & Tahsilat Özeti** daha küçük görünüyor mu?
9. İhale Takip Sistemi içinde manuel kayıt ekleme, Excel içe aktarma ve dışa aktarma çalışıyor mu?
10. Veri kaynakları tanımlıysa “Verileri Yenile” düğmesinden sonra yeni değerler geliyor mu?

## 12. Otomatik çalışma saatleri

- İhale taraması: Her gün Türkiye saatiyle **12.00**.
- Ekonomi verisi: Yaklaşık **15 dakikada bir**.
- Uygulama açıkken Streamlit tarafı yeni Firestore verilerini yaklaşık **60 saniyede bir** kontrol eder.

GitHub Actions zamanlamaları yoğunluk nedeniyle birkaç dakika gecikebilir.

## 13. Veri kaynaklarının beklenen alanları

İhale veri kaynağında en az şu bilgiler bulunmalıdır:

- EKAP numarası
- Kurum/idare
- İhale adı
- Yayın tarihi
- Son başvuru tarihi
- Yaklaşık maliyet
- Durum

Ekonomi veri kaynağında her gösterge için en az şu bilgiler bulunmalıdır:

- Kod veya sembol
- Görünen ad
- Değer
- Değişim yüzdesi

Senkronizasyon dosyası yaygın Türkçe ve İngilizce alan adlarını otomatik eşleştirmeye çalışır.

## 14. Geri alma işlemi

Bir sorun görülürse:

1. GitHub repository’de **Commits** sayfasını açın.
2. Güncellemeden önceki commit’i bulun.
3. Eski yedek ZIP içindeki dosyaları tekrar **Upload files** ile yükleyin.
4. Commit açıklamasına `Önceki stabil sürüme dönüş` yazın.
5. Streamlit Cloud’da **Reboot app** işlemi yapın.

Mevcut kayıt anahtarları değiştirilmediği için tarayıcıdaki eski kayıtlar korunur.

## 15. Değiştirilen ve eklenen dosyalar

### Değiştirilen

- `app.py`
- `index.html`
- `.streamlit/secrets.toml.example`

### Eklenen

- `.github/workflows/public-data-sync.yml`
- `automation/sync_public_data.py`
- `automation/requirements.txt`
- `GUNCELLEME_REHBERI.md`

## 16. Teknik güvenlik sınırı

Mevcut modüllerin veri yapısı, localStorage anahtarları, formlar, giriş sistemi, fontlar, görseller ve genel stil sınıfları korunmuştur. Yeni özellikler `tenders`, `economy`, `ekap_tenders` ve `system_public` alanlarıyla izole edilmiştir.
