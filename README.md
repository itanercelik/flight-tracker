# ✈️ Uçuş Fiyat Takip Botu

Telegram üzerinden uçuş fiyatlarını otomatik takip eden bot. RapidAPI (Flights Sky) ile uçuş fiyatlarını çeker, fiyat düşüşünde anında uyarı gönderir.

## Özellikler

- 🔍 Otomatik fiyat kontrolü (ayarlanabilir aralıklarla)
- - 📊 Günlük fiyat raporu
  - - 🔻 Fiyat düşüş uyarısı
    - - ➕ Telegram'dan dinamik rota ekleme/çıkarma
      - - 🔒 Chat ID bazlı yetkilendirme
        - - ⏱ API rate limiting koruması
         
          - ## Kurulum
         
          - ### 1. Telegram Bot
         
          - 1. @BotFather ile `/newbot` komutu kullanarak bot oluştur, Token'ı kaydet
            2. 2. Botla sohbet başlat, bir mesaj gönder
               3. 3. `https://api.telegram.org/bot<TOKEN>/getUpdates` adresinden `chat.id` değerini al
                 
                  4. ### 2. RapidAPI
                 
                  5. 1. [RapidAPI](https://rapidapi.com) adresine kayıt ol
                     2. 2. [Flights Sky API](https://rapidapi.com/ntd119/api/flights-sky) sayfasına git ve abone ol
                        3. 3. API Key'ini al
                          
                           4. ### 3. Railway'e Deploy
                          
                           5. ```bash
                              git init
                              git add .
                              git commit -m "initial commit"
                              git remote add origin https://github.com/KULLANICI_ADIN/flight-tracker.git
                              git push -u origin main
                              ```

                              1. [Railway](https://railway.app) adresine GitHub ile giriş yap
                              2. 2. "New Project" > "Deploy from GitHub Repo" > Bu repo'yu seç
                                 3. 3. Variables sekmesinde aşağıdakileri ekle:
                                   
                                    4. | Değişken | Açıklama |
                                    5. |----------|----------|
                                    6. | `RAPIDAPI_KEY` | RapidAPI anahtarın |
                                    7. | `TELEGRAM_BOT_TOKEN` | BotFather'dan aldığın token |
                                    8. | `TELEGRAM_CHAT_ID` | Telegram chat ID'n (birden fazla ise virgülle ayır) |
                                    9. | `CHECK_INTERVAL_HOURS` | Kontrol sıklığı - saat (opsiyonel, varsayılan: 12) |
                                    10. | `PRICE_DROP_THRESHOLD` | Fiyat düşüş eşiği - yüzde (opsiyonel, varsayılan: 5) |
                                   
                                    11. ## Telegram Komutları
                                   
                                    12. | Komut | Açıklama |
                                    13. |-------|----------|
                                    14. | `/add IST DPS 2026-03-30` | Yeni rota ekle |
                                    15. | `/remove 3` | Rota sil (ID ile) |
                                    16. | `/list` | Aktif rotaları listele |
                                    17. | `/check` | Manuel fiyat kontrolü |
                                    18. | `/prices IST ADB 2026-05-08` | Anlık fiyat sorgula (kaydetmeden) |
                                    19. | `/help` | Yardım |
                                   
                                    20. ## API Kullanımı
                                   
                                    21. Flights Sky API ücretsiz tier: **100 istek/ay**
                                   
                                    22. 5 rota x 2 kontrol/gün = 10 sorgu/gün = ~300/ay
                                   
                                    23. Daha fazla rota için kontrol sıklığını azaltabilir veya ücretli plana geçebilirsin.
                                   
                                    24. ## Güvenlik
                                   
                                    25. Bot yalnızca `TELEGRAM_CHAT_ID` ile tanımlanan kullanıcıların komutlarını kabul eder. Birden fazla kullanıcı için virgülle ayırarak ekleyebilirsin: `123456789,987654321`
                                   
                                    26. ## Not
                                   
                                    27. Havalimanı kodları IATA formatında girilmelidir: IST, SAW, ADB, DPS, CDG, JFK vb.
