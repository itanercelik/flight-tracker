import os
import json
import time
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import requests
from apscheduler.schedulers.blocking import BlockingScheduler

# ─── Config ───────────────────────────────────────────────────────
SERPER_API_KEY = os.environ.get("SERPER_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
CHECK_INTERVAL_HOURS = int(os.environ.get("CHECK_INTERVAL_HOURS", "12"))
PRICE_DROP_THRESHOLD = float(os.environ.get("PRICE_DROP_THRESHOLD", "5"))  # yüzde

DB_PATH = Path("data/flights.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ─── Database ─────────────────────────────────────────────────────
def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            date TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(origin, destination, date)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route_id INTEGER NOT NULL,
            price REAL,
            currency TEXT DEFAULT 'TRY',
            source_url TEXT,
            checked_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (route_id) REFERENCES routes(id)
        )
    """)
    conn.commit()
    conn.close()

def get_db():
    return sqlite3.connect(DB_PATH)

# ─── Route Management ────────────────────────────────────────────
def add_route(origin: str, destination: str, date: str) -> str:
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO routes (origin, destination, date) VALUES (?, ?, ?)",
            (origin.upper(), destination.upper(), date)
        )
        conn.commit()
        return f"✅ Rota eklendi: {origin.upper()} → {destination.upper()} ({date})"
    except Exception as e:
        return f"❌ Hata: {e}"
    finally:
        conn.close()

def remove_route(route_id: int) -> str:
    conn = get_db()
    conn.execute("UPDATE routes SET active = 0 WHERE id = ?", (route_id,))
    conn.commit()
    conn.close()
    return f"🗑 Rota #{route_id} silindi."

def list_routes() -> str:
    conn = get_db()
    rows = conn.execute(
        "SELECT id, origin, destination, date FROM routes WHERE active = 1"
    ).fetchall()
    conn.close()
    if not rows:
        return "📭 Aktif rota yok. /add ile ekle."
    lines = ["📋 **Aktif Rotalar:**\n"]
    for r in rows:
        lines.append(f"  `#{r[0]}` {r[1]} → {r[2]} ({r[3]})")
    return "\n".join(lines)

# ─── Serper API - Flight Price Fetch ─────────────────────────────
def fetch_price_serper(origin: str, destination: str, date: str) -> dict | None:
    """
    Serper Google Search API ile uçuş fiyatı çeker.
    Google Flights sonuçlarını parse eder.
    """
    query = f"flights from {origin} to {destination} on {date} price TRY"
    
    headers = {
        "X-API-KEY": SERPER_API_KEY,
        "Content-Type": "application/json"
    }
    
    payload = {
        "q": query,
        "gl": "tr",
        "hl": "tr",
        "num": 5
    }
    
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers=headers,
            json=payload,
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        
        # Serper flights modülü varsa kullan
        if "flights" in data:
            flights = data["flights"]
            if flights:
                best = flights[0]
                return {
                    "price": best.get("price"),
                    "currency": "TRY",
                    "airline": best.get("airline", ""),
                    "source": "serper_flights"
                }
        
        # Organic sonuçlardan fiyat parse et
        price = parse_price_from_results(data)
        if price:
            return {
                "price": price,
                "currency": "TRY",
                "airline": "",
                "source": "serper_organic"
            }
        
        # Google Flights doğrudan arama
        return fetch_price_serper_flights(origin, destination, date)
        
    except Exception as e:
        log.error(f"Serper API hatası: {e}")
        return None

def fetch_price_serper_flights(origin: str, destination: str, date: str) -> dict | None:
    """Serper'ın Google Flights endpoint'ini dene."""
    headers = {
        "X-API-KEY": SERPER_API_KEY,
        "Content-Type": "application/json"
    }
    
    payload = {
        "origin": origin,
        "destination": destination,
        "date": date,
        "gl": "tr",
        "hl": "tr",
        "currency": "TRY",
        "type": "1"  # one-way
    }
    
    try:
        resp = requests.post(
            "https://google.serper.dev/flights",
            headers=headers,
            json=payload,
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        
        flights = data.get("flights", [])
        if flights:
            best = min(flights, key=lambda f: f.get("price", float("inf")))
            return {
                "price": best.get("price"),
                "currency": "TRY",
                "airline": best.get("airline", ""),
                "duration": best.get("duration", ""),
                "source": "serper_flights_direct"
            }
    except Exception as e:
        log.warning(f"Serper Flights endpoint hatası: {e}")
    
    return None

def parse_price_from_results(data: dict) -> float | None:
    """Organic search sonuçlarından fiyat bilgisi çıkarmaya çalış."""
    import re
    
    texts = []
    for item in data.get("organic", []):
        texts.append(item.get("title", ""))
        texts.append(item.get("snippet", ""))
    
    for item in data.get("answerBox", {}).get("answers", []):
        texts.append(str(item))
    
    if data.get("answerBox", {}).get("snippet"):
        texts.append(data["answerBox"]["snippet"])
    
    for text in texts:
        # TRY/TL fiyat pattern'leri
        patterns = [
            r'([\d.,]+)\s*(?:TRY|TL|₺)',
            r'(?:TRY|TL|₺)\s*([\d.,]+)',
            r'([\d.]+,\d{2})\s*(?:lira|Lira)',
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                price_str = match.group(1).replace(".", "").replace(",", ".")
                try:
                    price = float(price_str)
                    if 100 < price < 500000:  # makul fiyat aralığı
                        return price
                except ValueError:
                    continue
    return None

# ─── Telegram Messaging ──────────────────────────────────────────
def send_telegram(text: str, parse_mode: str = "Markdown"):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        log.error(f"Telegram gönderim hatası: {e}")

def send_price_alert(route: tuple, current_price: float, avg_price: float, drop_pct: float):
    origin, dest, date = route[1], route[2], route[3]
    msg = (
        f"🔻 *FİYAT DÜŞTÜ!*\n\n"
        f"✈️ {origin} → {dest}\n"
        f"📅 {date}\n"
        f"💰 Şu anki: *{current_price:,.0f} TRY*\n"
        f"📊 7 Gün Ort: {avg_price:,.0f} TRY\n"
        f"🔴 %{drop_pct:.1f} düşüş"
    )
    send_telegram(msg)

def send_daily_report(results: list):
    now = datetime.now().strftime("%d %B %Y %A %H:%M")
    lines = [f"📊 *Günlük Fiyat Raporu*\n🕐 {now}\n"]
    
    for r in results:
        emoji = "🟢" if r["change"] <= 0 else "🔴"
        change_str = f"%{abs(r['change']):.1f} {'düşüş' if r['change'] <= 0 else 'artış'}"
        lines.append(
            f"✈️ *{r['origin']} → {r['dest']}*\n"
            f"   📅 {r['date']}\n"
            f"   💰 Güncel: *{r['price']:,.0f} TRY*\n"
            f"   📊 7 Gün Ort: {r['avg']:,.0f} TRY\n"
            f"   {emoji} {change_str}\n"
        )
    
    if not results:
        lines.append("Fiyat verisi bulunamadı.")
    
    send_telegram("\n".join(lines))

# ─── Price Check Logic ───────────────────────────────────────────
def get_7day_avg(route_id: int) -> float:
    conn = get_db()
    rows = conn.execute("""
        SELECT AVG(price) FROM prices 
        WHERE route_id = ? 
        AND price IS NOT NULL
        AND checked_at >= datetime('now', '-7 days')
    """, (route_id,)).fetchone()
    conn.close()
    return rows[0] if rows[0] else 0

def check_all_routes():
    log.info("🔍 Fiyat kontrolü başlıyor...")
    conn = get_db()
    routes = conn.execute(
        "SELECT id, origin, destination, date FROM routes WHERE active = 1"
    ).fetchall()
    conn.close()
    
    results = []
    
    for route in routes:
        route_id, origin, dest, date = route
        log.info(f"  Kontrol: {origin} → {dest} ({date})")
        
        price_data = fetch_price_serper(origin, dest, date)
        
        if price_data and price_data.get("price"):
            price = float(price_data["price"])
            
            # Kaydet
            conn = get_db()
            conn.execute(
                "INSERT INTO prices (route_id, price, currency) VALUES (?, ?, ?)",
                (route_id, price, price_data.get("currency", "TRY"))
            )
            conn.commit()
            conn.close()
            
            # 7 günlük ortalama
            avg = get_7day_avg(route_id)
            if avg == 0:
                avg = price
            
            change_pct = ((price - avg) / avg) * 100 if avg else 0
            
            results.append({
                "origin": origin,
                "dest": dest,
                "date": date,
                "price": price,
                "avg": avg,
                "change": change_pct
            })
            
            # Fiyat düşüşü uyarısı
            if change_pct <= -PRICE_DROP_THRESHOLD:
                send_price_alert(route, price, avg, abs(change_pct))
            
            time.sleep(2)  # API rate limit
        else:
            log.warning(f"  ⚠️ Fiyat bulunamadı: {origin} → {dest}")
    
    # Günlük rapor
    send_daily_report(results)
    log.info(f"✅ {len(results)}/{len(routes)} rota kontrol edildi.")

# ─── Telegram Bot Commands ────────────────────────────────────────
def process_telegram_updates():
    """Telegram'dan gelen komutları işle."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    
    # Son işlenen update ID
    offset_file = Path("data/last_update_id.txt")
    offset = 0
    if offset_file.exists():
        offset = int(offset_file.read_text().strip()) + 1
    
    try:
        resp = requests.get(url, params={"offset": offset, "timeout": 5}, timeout=10)
        data = resp.json()
    except Exception as e:
        log.error(f"Telegram update hatası: {e}")
        return
    
    for update in data.get("result", []):
        update_id = update["update_id"]
        offset_file.write_text(str(update_id))
        
        message = update.get("message", {})
        text = message.get("text", "").strip()
        chat_id = str(message.get("chat", {}).get("id", ""))
        
        if chat_id != TELEGRAM_CHAT_ID:
            continue
        
        if text.startswith("/add"):
            handle_add_command(text)
        elif text.startswith("/remove"):
            handle_remove_command(text)
        elif text.startswith("/list"):
            send_telegram(list_routes())
        elif text.startswith("/check"):
            send_telegram("🔍 Manuel kontrol başlatılıyor...")
            check_all_routes()
        elif text.startswith("/help") or text.startswith("/start"):
            send_help()

def handle_add_command(text: str):
    """
    /add IST DPS 2026-03-30
    """
    parts = text.split()
    if len(parts) != 4:
        send_telegram(
            "❌ Format: `/add KALKIŞ VARIŞ YYYY-MM-DD`\n"
            "Örnek: `/add IST DPS 2026-03-30`"
        )
        return
    
    _, origin, dest, date = parts
    result = add_route(origin, dest, date)
    send_telegram(result)

def handle_remove_command(text: str):
    """
    /remove 3
    """
    parts = text.split()
    if len(parts) != 2:
        send_telegram("❌ Format: `/remove ROTA_ID`\nÖrnek: `/remove 3`")
        return
    
    try:
        route_id = int(parts[1])
        result = remove_route(route_id)
        send_telegram(result)
    except ValueError:
        send_telegram("❌ Geçersiz rota ID.")

def send_help():
    msg = (
        "✈️ *Uçuş Fiyat Takip Botu*\n\n"
        "📌 *Komutlar:*\n"
        "`/add IST DPS 2026-03-30` → Rota ekle\n"
        "`/remove 3` → Rota sil\n"
        "`/list` → Aktif rotaları göster\n"
        "`/check` → Manuel fiyat kontrolü\n"
        "`/help` → Bu mesaj\n\n"
        "🔔 Fiyat normalin altına düşünce otomatik uyarı alırsın!\n"
        f"⏰ Her {CHECK_INTERVAL_HOURS} saatte bir kontrol ediliyor."
    )
    send_telegram(msg)

# ─── Main ────────────────────────────────────────────────────────
def main():
    log.info("🚀 Uçuş Fiyat Takip Botu başlatılıyor...")
    init_db()
    
    # Config kontrolü
    if not all([SERPER_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        log.error("❌ Eksik environment variable! SERPER_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID gerekli.")
        return
    
    send_telegram("🟢 Bot aktif! /help yazarak komutları görebilirsin.")
    
    scheduler = BlockingScheduler()
    
    # Fiyat kontrolü
    scheduler.add_job(
        check_all_routes,
        'interval',
        hours=CHECK_INTERVAL_HOURS,
        next_run_time=datetime.now() + timedelta(seconds=10)
    )
    
    # Telegram komut kontrolü (her 30 saniyede)
    scheduler.add_job(
        process_telegram_updates,
        'interval',
        seconds=30
    )
    
    log.info(f"⏰ Her {CHECK_INTERVAL_HOURS} saatte bir fiyat kontrolü yapılacak.")
    
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot durduruluyor...")

if __name__ == "__main__":
    main()
