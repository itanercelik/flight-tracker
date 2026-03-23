import os
import json
import time
import re
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import requests
from apscheduler.schedulers.blocking import BlockingScheduler

# --- Config ---
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
CHECK_INTERVAL_HOURS = int(os.environ.get("CHECK_INTERVAL_HOURS", "12"))
PRICE_DROP_THRESHOLD = float(os.environ.get("PRICE_DROP_THRESHOLD", "5"))

RAPIDAPI_HOST = "flights-sky.p.rapidapi.com"
DB_PATH = Path("data/flights.db")

logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# --- Database ---
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
                airline TEXT DEFAULT '',
                stops INTEGER DEFAULT -1,
                duration TEXT DEFAULT '',
                checked_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (route_id) REFERENCES routes(id)
            )
        """)
        conn.commit()
        conn.close()

def get_db():
        return sqlite3.connect(DB_PATH)

# --- Route Management ---
def add_route(origin, destination, date):
        conn = get_db()
        try:
                    conn.execute(
                                    "INSERT OR IGNORE INTO routes (origin, destination, date) VALUES (?, ?, ?)",
                                    (origin.upper(), destination.upper(), date)
                    )
                    conn.commit()
                    return f"Rota eklendi: {origin.upper()} -> {destination.upper()} ({date})"
except Exception as e:
        return f"Hata: {e}"
finally:
        conn.close()

def remove_route(route_id):
        conn = get_db()
        conn.execute("UPDATE routes SET active = 0 WHERE id = ?", (route_id,))
        conn.commit()
        conn.close()
        return f"Rota #{route_id} silindi."

def list_routes():
        conn = get_db()
        rows = conn.execute(
            "SELECT id, origin, destination, date FROM routes WHERE active = 1"
        ).fetchall()
        conn.close()
        if not rows:
                    return "Aktif rota yok. /add ile ekle."
                lines = ["Aktif Rotalar:\n"]
    for r in rows:
                lines.append(f"  #{r[0]} {r[1]} -> {r[2]} ({r[3]})")
            return "\n".join(lines)

# --- Flights Scraper Sky API (RapidAPI) ---
def get_sky_id(query):
        """Sehir/havaalani adini veya IATA kodunu skyId'ye cevir."""
    url = "https://flights-sky.p.rapidapi.com/flights/auto-complete"
    headers = {
                "x-rapidapi-host": RAPIDAPI_HOST,
                "x-rapidapi-key": RAPIDAPI_KEY
    }
    params = {"query": query}
    try:
                resp = requests.get(url, headers=headers, params=params, timeout=15)
                resp.raise_for_status()
                data = resp.json()
                results = data.get("data", [])
                if results:
                                presentation = results[0].get("presentation", {})
                                sky_id = presentation.get("id") or results[0].get("id")
                                entity_id = results[0].get("navigation", {}).get("relevantFlightParams", {}).get("skyId")
                                return sky_id or entity_id or query.upper()
                            return query.upper()
except Exception as e:
        log.error(f"Sky ID alma hatasi ({query}): {e}")
        return query.upper()

def fetch_flights(origin, destination, date):
        """Tek yon ucus fiyatlarini cek."""
    url = "https://flights-sky.p.rapidapi.com/flights/search-one-way"
    headers = {
                "x-rapidapi-host": RAPIDAPI_HOST,
                "x-rapidapi-key": RAPIDAPI_KEY
    }
    from_id = get_sky_id(origin)
    to_id = get_sky_id(destination)
                params = {
        "fromEntityId": from_id,
        "toEntityId": to_id,
                            "departDate": date,
                            "market": "TR",
                            "locale": "tr-TR",
                            "currency": "TRY"
                }
    try:
                resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        context = data.get("data", {}).get("context", {})
        status = context.get("status", "")
        itineraries = data.get("data", {}).get("itineraries", [])
        if not itineraries:
                        log.warning(f"Ucus bulunamadi: {origin} -> {destination} ({date})")
                        return None
                    flights = []
        for itin in itineraries:
                        price_raw = itin.get("price", {}).get("raw", 0)
                        price_fmt = itin.get("price", {}).get("formatted", "")
                        legs = itin.get("legs", [])
                        if legs:
                                            leg = legs[0]
                                            carrier_name = ""
                                            carriers = leg.get("carriers", {}).get("marketing", [])
                                            if carriers:
                                                                    carrier_name = carriers[0].get("name", "")
                                                                stop_count = leg.get("stopCount", 0)
                                            duration_min = leg.get("durationInMinutes", 0)
                                            dep_time = leg.get("departure", "")
                                            arr_time = leg.get("arrival", "")
                                            dep_short = dep_time[11:16] if len(dep_time) > 16 else dep_time
                                            arr_short = arr_time[11:16] if len(arr_time) > 16 else arr_time
                                            flights.append({
                                                "price": price_raw,
                                                "price_formatted": price_fmt,
                                                "airline": carrier_name,
                                                "stops": stop_count,
                                                "duration_min": duration_min,
                                                "departure": dep_short,
                                                "arrival": arr_short,
                                                "currency": "TRY"
                            })
                                    if flights:
                        flights.sort(key=lambda x: x["price"])
        return flights
except Exception as e:
        log.error(f"Flight API hatasi: {e}")
        return None

# --- Telegram Messaging ---
def send_telegram(text, parse_mode="Markdown"):
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
        log.error(f"Telegram gonderim hatasi: {e}")

def format_duration(minutes):
        h = minutes // 60
    m = minutes % 60
    if h > 0 and m > 0:
                return f"{h}sa {m}dk"
elif h > 0:
        return f"{h}sa"
else:
        return f"{m}dk"

def send_price_alert(route, current_price, avg_price, drop_pct):
    origin, dest, date = route[1], route[2], route[3]
    msg = (
                          f"FIYAT DUSTU!\n\n"
                f"Ucus: {origin} -> {dest}\n"
                f"Tarih: {date}\n"
                f"Su anki: {current_price:,.0f} TRY\n"
        f"7 Gun Ort: {avg_price:,.0f} TRY\n"
                f"Dusus: %{drop_pct:.1f}"
    )
    send_telegram(msg, parse_mode=None)

def send_daily_report(results):
        now = datetime.now().strftime("%d/%m/%Y %H:%M")
    lines = [f"Gunluk Fiyat Raporu\nTarih: {now}\n"]
    for r in results:
                change_str = f"%{abs(r['change']):.1f} {'dusus' if r['change'] <= 0 else 'artis'}"
        airline_str = f" ({r['airline']})" if r.get('airline') else ""
        lines.append(
                        f"Ucus: {r['origin']} -> {r['dest']}\n"
                f"  Tarih: {r['date']}\n"
            f"  Fiyat: {r['price']:,.0f} TRY{airline_str}\n"
            f"  7 Gun Ort: {r['avg']:,.0f} TRY\n"
                        f"  Degisim: {change_str}\n"
        )
    if not results:
        lines.append("Fiyat verisi bulunamadi.")
    send_telegram("\n".join(lines), parse_mode=None)

# --- /prices Command ---
def handle_prices_command(text):
            """/prices IST ECN 2026-05-08"""
    parts = text.split()
    if len(parts) != 4:
        send_telegram(
                        "Format: /prices KALKIS VARIS YYYY-MM-DD\n"
                        "Ornek: /prices IST ECN 2026-05-08",
                        parse_mode=None
        )
        return
    _, origin, dest, date = parts
    send_telegram(f"Fiyatlar arastiriliyor: {origin.upper()} -> {dest.upper()} ({date})...", parse_mode=None)
    flights = fetch_flights(origin, dest, date)
    if not flights:
                send_telegram(f"Ucus bulunamadi: {origin.upper()} -> {dest.upper()} ({date})", parse_mode=None)
        return
    lines = [f"Ucus Fiyatlari: {origin.upper()} -> {dest.upper()}\nTarih: {date}\n"]
    seen = set()
    count = 0
    for f in flights:
        if count >= 10:
                        break
        key = f"{f['airline']}_{f['stops']}_{f['price']}"
        if key in seen:
                        continue
        seen.add(key)
        count += 1
        stop_str = "Direkt" if f["stops"] == 0 else f"{f['stops']} Aktarma"
        dur_str = format_duration(f["duration_min"]) if f["duration_min"] else ""
        lines.append(
                        f"{count}. {f['airline'] or 'Bilinmiyor'}\n"
                        f"   Fiyat: {f['price']:,.0f} TRY\n"
                        f"   {stop_str} | {dur_str}\n"
                        f"   Kalkis: {f['departure']} -> Varis: {f['arrival']}\n"
        )
    lines.append(f"\nToplam {len(flights)} ucus bulundu, en ucuz {count} tanesi gosteriliyor.")
    send_telegram("\n".join(lines), parse_mode=None)

# --- Price Check Logic ---
def get_7day_avg(route_id):
        conn = get_db()
    rows = conn.execute("""
            SELECT AVG(price) FROM prices
                    WHERE route_id = ? AND price IS NOT NULL
                            AND checked_at >= datetime('now', '-7 days')
                                """, (route_id,)).fetchone()
    conn.close()
    return rows[0] if rows[0] else 0

def check_all_routes():
        log.info("Fiyat kontrolu basliyor...")
    conn = get_db()
    routes = conn.execute(
                "SELECT id, origin, destination, date FROM routes WHERE active = 1"
    ).fetchall()
    conn.close()
    results = []
    for route in routes:
                route_id, origin, dest, date = route
        log.info(f"  Kontrol: {origin} -> {dest} ({date})")
        flights = fetch_flights(origin, dest, date)
        if flights and len(flights) > 0:
                        best = flights[0]
            price = float(best["price"])
            airline = best.get("airline", "")
            conn = get_db()
            conn.execute(
                                "INSERT INTO prices (route_id, price, currency, airline, stops, duration) VALUES (?, ?, ?, ?, ?, ?)",
                                (route_id, price, "TRY", airline, best.get("stops", -1), str(best.get("duration_min", "")))
            )
            conn.commit()
            conn.close()
            avg = get_7day_avg(route_id)
            if avg == 0:
                avg = price
            change_pct = ((price - avg) / avg) * 100 if avg else 0
            results.append({
                                "origin": origin,
                                "dest": dest,
                                "date": date,
                                "price": price,
                                "airline": airline,
                                "avg": avg,
                                "change": change_pct
            })
            if change_pct <= -PRICE_DROP_THRESHOLD:
                                send_price_alert(route, price, avg, abs(change_pct))
                            time.sleep(2)
else:
            log.warning(f"  Fiyat bulunamadi: {origin} -> {dest}")
    send_daily_report(results)
    log.info(f"{len(results)}/{len(routes)} rota kontrol edildi.")

# --- Telegram Bot Commands ---
def process_telegram_updates():
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    offset_file = Path("data/last_update_id.txt")
    offset_file.parent.mkdir(parents=True, exist_ok=True)
    offset = 0
    if offset_file.exists():
                try:
                                offset = int(offset_file.read_text().strip()) + 1
except ValueError:
            offset = 0
    try:
                resp = requests.get(url, params={"offset": offset, "timeout": 5}, timeout=10)
        data = resp.json()
except Exception as e:
                log.error(f"Telegram update hatasi: {e}")
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
            send_telegram(list_routes(), parse_mode=None)
elif text.startswith("/check"):
            send_telegram("Manuel kontrol baslatiliyor...", parse_mode=None)
            check_all_routes()
elif text.startswith("/prices"):
            handle_prices_command(text)
elif text.startswith("/help") or text.startswith("/start"):
            send_help()

def handle_add_command(text):
        parts = text.split()
    if len(parts) != 4:
                send_telegram(
                                "Format: /add KALKIS VARIS YYYY-MM-DD\n"
                                "Ornek: /add IST DPS 2026-03-30",
            parse_mode=None
                )
        return
    _, origin, dest, date = parts
    result = add_route(origin, dest, date)
    send_telegram(result, parse_mode=None)

def handle_remove_command(text):
    parts = text.split()
    if len(parts) != 2:
                send_telegram("Format: /remove ROTA_ID\nOrnek: /remove 3", parse_mode=None)
        return
    try:
                route_id = int(parts[1])
        result = remove_route(route_id)
        send_telegram(result, parse_mode=None)
except ValueError:
        send_telegram("Gecersiz rota ID.", parse_mode=None)

def send_help():
        msg = (
        "Ucus Fiyat Takip Botu\n\n"
        "Komutlar:\n"
                    "/add IST DPS 2026-03-30 -> Rota ekle\n"
                    "/remove 3 -> Rota sil\n"
        "/list -> Aktif rotalari goster\n"
                    "/check -> Manuel fiyat kontrolu\n"
                    "/prices IST ECN 2026-05-08 -> Anlik fiyat listesi\n"
                    "/help -> Bu mesaj\n\n"
                    "Fiyat normalin altina dusunce otomatik uyari alirsin!\n"
        f"Her {CHECK_INTERVAL_HOURS} saatte bir kontrol ediliyor."
        )
    send_telegram(msg, parse_mode=None)

# --- Main ---
def main():
        log.info("Ucus Fiyat Takip Botu baslatiliyor...")
    init_db()
    if not all([RAPIDAPI_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
                    log.error("Eksik environment variable! RAPIDAPI_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID gerekli.")
        return
    send_telegram("Bot aktif! /help yazarak komutlari gorebilirsin.", parse_mode=None)
    scheduler = BlockingScheduler()
    scheduler.add_job(
                check_all_routes,
                'interval',
                hours=CHECK_INTERVAL_HOURS,
                next_run_time=datetime.now() + timedelta(seconds=30)
    )
    scheduler.add_job(
                process_telegram_updates,
                'interval',
                seconds=30
    )
    log.info(f"Her {CHECK_INTERVAL_HOURS} saatte bir fiyat kontrolu yapilacak.")
    try:
                scheduler.start()
except (KeyboardInterrupt, SystemExit):
        log.info("Bot durduruluyor...")

if __name__ == "__main__":
        main()
