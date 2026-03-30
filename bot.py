bot.py dosyasında bir bug var. /currency USD veya /currency EUR yaptıktan sonra /prices komutu "Kalkis havaalani bulunamadi: IST" hatası veriyor. Sebebi: para birimi değiştirince search_airport fonksiyonu da etkileniyor olabilir, ya da search_one_way parametreleri yanlış gidiyor.
Şu 2 düzeltmeyi yap:
Fix 1: search_airport fonksiyonunu para biriminden bağımsız yap
search_airport fonksiyonu HER ZAMAN Türkiye market parametreleriyle çalışmalı. Para birimi değişse bile havaalanı araması Türkçe kalmalı. search_airport fonksiyonunda market/locale/currency parametresi KULLANMA. Sadece query ve placeTypes gönder. Şu anki hali budur, değişmemesi gereken kısım:
pythondef search_airport(query):
    ...
    url = "https://flights-sky.p.rapidapi.com/flights/auto-complete"
    params = {"query": query}
    ...
Eğer search_airport içine locale, market veya currency eklendiyse KALDIR. Bu fonksiyon sadece havaalanı adı araması yapıyor, para birimiyle ilgisi yok.
Fix 2: search_one_way'de para birimi parametrelerini doğru ayarla
search_one_way fonksiyonunda ACTIVE_CURRENCY'ye göre sadece "currency" parametresini değiştir. "market" ve "locale" parametrelerini HER ZAMAN Türkiye olarak bırak. Çünkü biz Türkiye'den arama yapıyoruz, sadece fiyatın gösterildiği para birimini değiştirmek istiyoruz.
Yani search_one_way şu şekilde olmalı:
pythondef search_one_way(origin_sky_id, dest_sky_id, depart_date):
    if not api_limiter.allow():
        log.warning("Rate limit asildi, ucus aramasi atlaniyor")
        return None
    url = "https://flights-sky.p.rapidapi.com/flights/search-one-way"
    params = {
        "fromEntityId": origin_sky_id,
        "toEntityId": dest_sky_id,
        "departDate": depart_date,
        "currency": ACTIVE_CURRENCY,
        "market": "TR",
        "locale": "tr-TR",
    }
    ...
DİKKAT: market ve locale HER ZAMAN "TR" ve "tr-TR" olarak SABIT kalsın. Sadece "currency" satırı ACTIVE_CURRENCY'yi kullansın. CURRENCIES dict'indeki market ve locale değerlerini search_one_way'de KULLANMA.
Fix 3: Doğrulama
Değişikliklerden sonra şu akış çalışmalı:

/currency USD → "Para birimi degistirildi: USD ($)"
/prices IST JFK 2026-06-05 → Havaalanı bulunur, fiyatlar USD olarak gösterilir
/currency TRY → "Para birimi degistirildi: TRY (₺)"
/prices IST JFK 2026-06-05 → Fiyatlar TRY olarak gösterilir

Sadece değişen satırları göster, "şu satırı bul → şununla değiştir" formatında anlat.
