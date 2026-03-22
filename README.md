# ✈️ Uçuş Fiyat Takip Botu

Telegram üzerinden uçuş fiyatlarını otomatik takip eden bot.
Serper API ile Google Flights fiyatlarını çeker, fiyat düşüşünde anında uyarı gönderir.

## Özellikler

- 🔍 Otomatik fiyat kontrolü (ayarlanabilir aralıklarla)
- - 📊 Günlük fiyat raporu
  - - 🔻 Fiyat düşüş uyarısı
    - - ➕ Telegram'dan dinamik rota ekleme/çıkarma
      - - 📈 7 günlük ortalama takibi
       
        - ## Kurulum
       
        - ### 1. Telegram Bot
        - 1. @BotFather → `/newbot` → Token'ı kaydet
          2. 2. Botla sohbet başlat, mesaj gönder
             3. 3. `https://api.telegram.org/bot<TOKEN>/getUpdates` → chat.id'yi al
               
                4. ### 2. Serper API
                5. 1. https://serper.dev → Kayıt ol → API Key al
                  
                   2. ### 3. Railway'e Deploy
                  
                   3. ```bash
                      # Repo'yu GitHub'a pushla
                      git init
                      git add .
                      git commit -m "initial commit"
                      git remote add origin https://github.com/SENIN_USERNAME/flight-tracker.git
                      git push -u origin main
                      ```

                      1. https://railway.app → GitHub ile giriş
                      2. 2. "New Project" → "Deploy from GitHub Repo" → Bu repo'yu seç
                         3. 3. Variables sekmesinde ekle:
                            4.    - `SERPER_API_KEY`
                                  -    - `TELEGRAM_BOT_TOKEN`
                                       -    - `TELEGRAM_CHAT_ID`
                                            -    - `CHECK_INTERVAL_HOURS` (opsiyonel, varsayılan: 12)
                                                 -    - `PRICE_DROP_THRESHOLD` (opsiyonel, varsayılan: 5)
                                                  
                                                      - ## Telegram Komutları
                                                  
                                                      - | Komut | Açıklama |
                                                      - |-------|----------|
                                                      - | `/add IST DPS 2026-03-30` | Yeni rota ekle |
                                                      - | `/remove 3` | Rota sil (ID ile) |
                                                      - | `/list` | Aktif rotaları listele |
                                                      - | `/check` | Manuel fiyat kontrolü |
                                                      - | `/help` | Yardım |
                                                  
                                                      - ## API Kullanımı
                                                  
                                                      - Serper ücretsiz tier: **2.500 sorgu/ay**
                                                      - - 5 rota × 2 kontrol/gün = 10 sorgu/gün = ~300/ay ✅
                                                        - - Daha fazla rota için kontrol sıklığını azaltabilirsin
                                                         
                                                          - ## Not
                                                         
                                                          - Havalimanı kodları IATA formatında: IST, SAW, DPS, CDG, JFK vb.
