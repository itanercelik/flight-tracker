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

# ---- DB helpers ----
def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            origin TEXT,
            destination TEXT,
            date TEXT,
            origin_sky_id TEXT,
            dest_sky_id TEXT,
            origin_entity_id TEXT,
            dest_entity_id TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route_id INTEGER,
            price REAL,
            airline TEXT,
            checked_at TEXT,
            FOREIGN KEY(route_id) REFERENCES routes(id)
        )
    """)
    con.commit()
    con.close()

# ---- API helpers ----
def api_headers():
    return {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": RAPIDAPI_HOST,
    }

def search_airport(query):
    url = "https://flights-sky.p.rapidapi.com/flights/auto-complete"
    params = {"query": query, "placeTypes": "AIRPORT"}
    try:
        r = requests.get(url, headers=api_headers(), params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        results = data.get("data", [])
        if results:
            first = results[0]
            pres = first.get("presentation", {})
            nav = first.get("navigation", {})
            rel = nav.get("relevantFlightParams", {})
            return {
                "skyId": rel.get("skyId", ""),
                "entityId": rel.get("entityId", ""),
                "title": pres.get("title", query),
            }
    except Exception as e:
        log.error(f"Airport search error: {e}")
    return None

def search_one_way(from_entity, to_entity, depart_date):
    url = "https://flights-sky.p.rapidapi.com/flights/search-one-way"
    params = {
        "fromEntityId": from_entity,
        "toEntityId": to_entity,
        "departDate": depart_date,
        "market": "TR",
        "locale": "tr-TR",
        "currency": "TRY",
    }
    try:
        r = requests.get(url, headers=api_headers(), params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"Flight search error: {e}")
    return None

def parse_itineraries(api_response):
    flights = []
    if not api_response:
        return flights
    data = api_response.get("data", {})
    itineraries = data.get("itineraries", [])
    for it in itineraries[:10]:
        price_raw = it.get("price", {}).get("raw", 0)
        price_fmt = it.get("price", {}).get("formatted", "N/A")
        legs = it.get("legs", [])
        if not legs:
            continue
        leg = legs[0]
        departure = leg.get("departure", "")
        arrival = leg.get("arrival", "")
        duration = leg.get("durationInMinutes", 0)
        stop_count = leg.get("stopCount", 0)
        carriers = leg.get("carriers", {}).get("marketing", [])
        airline = carriers[0].get("name", "Bilinmiyor") if carriers else "Bilinmiyor"
        flights.append({
            "airline": airline,
            "price": price_raw,
            "price_formatted": price_fmt,
            "departure": departure,
            "arrival": arrival,
            "duration_min": duration,
            "stops": stop_count,
        })
    return flights

# ---- Telegram helpers ----
def send_telegram(text, parse_mode="Markdown"):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        log.error(f"Telegram send error: {e}")

def get_updates(offset=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 5}
    if offset:
        params["offset"] = offset
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception as e:
        log.error(f"Telegram getUpdates error: {e}")
    return []

# ---- Command handlers ----
def cmd_add(args):
    if len(args) < 3:
        return "Kullanim: /add ORIGIN DEST YYYY-MM-DD\nOrnek: /add IST ADB 2026-05-08"
    origin_q, dest_q, date_str = args[0], args[1], args[2]
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return "Tarih formati hatali. YYYY-MM-DD kullanin."
    origin = search_airport(origin_q)
    if not origin:
        return f"Kalkis havaalani bulunamadi: {origin_q}"
    dest = search_airport(dest_q)
    if not dest:
        return f"Varis havaalani bulunamadi: {dest_q}"
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO routes (origin, destination, date, origin_sky_id, dest_sky_id, origin_entity_id, dest_entity_id) VALUES (?,?,?,?,?,?,?)",
        (origin_q.upper(), dest_q.upper(), date_str, origin["skyId"], dest["skyId"], origin["entityId"], dest["entityId"]),
    )
    con.commit()
    route_id = cur.lastrowid
    con.close()
    return f"Rota eklendi (#{route_id}): {origin['title']} -> {dest['title']} | {date_str}"

def cmd_remove(args):
    if not args:
        return "Kullanim: /remove ROTA_ID"
    try:
        rid = int(args[0])
    except ValueError:
        return "Gecersiz ID."
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DELETE FROM routes WHERE id=?", (rid,))
    deleted = cur.rowcount
    cur.execute("DELETE FROM price_history WHERE route_id=?", (rid,))
    con.commit()
    con.close()
    if deleted:
        return f"Rota #{rid} silindi."
    return f"Rota #{rid} bulunamadi."

def cmd_list():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT id, origin, destination, date FROM routes")
    rows = cur.fetchall()
    con.close()
    if not rows:
        return "Takip edilen rota yok. /add ile ekleyin."
    lines = ["*Takip Edilen Rotalar:*"]
    for r in rows:
        lines.append(f"#{r[0]}  {r[1]} -> {r[2]}  Tarih: {r[3]}")
    return "\n".join(lines)

def cmd_check():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT id, origin, destination, date, origin_entity_id, dest_entity_id FROM routes")
    rows = cur.fetchall()
    con.close()
    if not rows:
        return "Takip edilen rota yok."
    messages = []
    for r in rows:
        rid, origin, dest, date_str, o_eid, d_eid = r
        api_resp = search_one_way(o_eid, d_eid, date_str)
        flights = parse_itineraries(api_resp)
        if not flights:
            messages.append(f"#{rid} {origin}->{dest} {date_str}: Ucus bulunamadi.")
            continue
        best = min(flights, key=lambda f: f["price"])
        line = f"#{rid} {origin}->{dest} {date_str}\nEn ucuz: {best['airline']} {best['price_formatted']} ({best['stops']} aktarma, {best['duration_min']}dk)"
        con2 = sqlite3.connect(DB_PATH)
        cur2 = con2.cursor()
        cur2.execute("SELECT price FROM price_history WHERE route_id=? ORDER BY id DESC LIMIT 1", (rid,))
        prev = cur2.fetchone()
        if prev and prev[0] > 0:
            diff_pct = ((best["price"] - prev[0]) / prev[0]) * 100
            if diff_pct < -PRICE_DROP_THRESHOLD:
                line += f"\n\u26a0 Fiyat dustu! Onceki: {prev[0]:.0f} TL -> Simdi: {best['price']:.0f} TL ({diff_pct:.1f}%)"
        cur2.execute(
            "INSERT INTO price_history (route_id, price, airline, checked_at) VALUES (?,?,?,?)",
            (rid, best["price"], best["airline"], datetime.now().isoformat()),
        )
        con2.commit()
        con2.close()
        messages.append(line)
    return "\n\n".join(messages) if messages else "Sonuc yok."

def cmd_prices(args):
    if len(args) < 3:
        return "Kullanim: /prices ORIGIN DEST YYYY-MM-DD\nOrnek: /prices ADB ECN 2026-05-08"
    origin_q, dest_q, date_str = args[0], args[1], args[2]
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return "Tarih formati hatali. YYYY-MM-DD kullanin."
    origin = search_airport(origin_q)
    if not origin:
        return f"Kalkis havaalani bulunamadi: {origin_q}"
    dest = search_airport(dest_q)
    if not dest:
        return f"Varis havaalani bulunamadi: {dest_q}"
    api_resp = search_one_way(origin["entityId"], dest["entityId"], date_str)
    flights = parse_itineraries(api_resp)
    if not flights:
        return f"{origin['title']} -> {dest['title']} ({date_str}) icin ucus bulunamadi."
    lines = [f"*{origin['title']} -> {dest['title']}* | {date_str}\n"]
    for i, f in enumerate(flights, 1):
        stop_txt = "Direkt" if f["stops"] == 0 else f"{f['stops']} aktarma"
        dep_time = f["departure"][11:16] if len(f["departure"]) > 16 else f["departure"]
        arr_time = f["arrival"][11:16] if len(f["arrival"]) > 16 else f["arrival"]
        h, m = divmod(f["duration_min"], 60)
        dur_txt = f"{h}s {m}dk" if h else f"{m}dk"
        lines.append(f"{i}. {f['airline']} - {f['price_formatted']}  |  {stop_txt}  |  {dur_txt}  |  {dep_time}-{arr_time}")
    return "\n".join(lines)

def cmd_help():
    return (
        "*Ucus Takip Botu Komutlari:*\n"
        "/add ORIGIN DEST YYYY-MM-DD - Rota ekle\n"
        "/remove ID - Rota sil\n"
        "/list - Takip edilen rotalari goster\n"
        "/check - Tum rotalarin guncel fiyatlarini kontrol et\n"
        "/prices ORIGIN DEST YYYY-MM-DD - Anlik fiyat listesi\n"
        "/help - Bu mesaji goster"
    )

# ---- Scheduled job ----
def check_all_routes():
    log.info("Zamanlanmis fiyat kontrolu basliyor...")
    result = cmd_check()
    if result and result != "Takip edilen rota yok.":
        send_telegram(result)
    log.info("Fiyat kontrolu tamamlandi.")

# ---- Telegram polling ----
LAST_UPDATE_ID = 0

def process_telegram_updates():
    global LAST_UPDATE_ID
    updates = get_updates(offset=LAST_UPDATE_ID + 1 if LAST_UPDATE_ID else None)
    for upd in updates:
        LAST_UPDATE_ID = upd["update_id"]
        msg = upd.get("message", {})
        text = msg.get("text", "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if not text.startswith("/"):
            continue
        parts = text.split()
        command = parts[0].lower().split("@")[0]
        args = parts[1:]
        if command == "/start":
            reply = "Merhaba! Ucus takip botu aktif. /help yazarak komutlari gorebilirsin."
        elif command == "/add":
            reply = cmd_add(args)
        elif command == "/remove":
            reply = cmd_remove(args)
        elif command == "/list":
            reply = cmd_list()
        elif command == "/check":
            reply = cmd_check()
        elif command == "/prices":
            reply = cmd_prices(args)
        elif command == "/help":
            reply = cmd_help()
        else:
            reply = "Bilinmeyen komut. /help yazin."
        send_to = chat_id or TELEGRAM_CHAT_ID
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": send_to, "text": reply, "parse_mode": "Markdown"}
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            log.error(f"Reply error: {e}")

# ---- Main ----
def main():
    log.info("Bot baslatiliyor...")
    init_db()
    if not RAPIDAPI_KEY:
        log.error("RAPIDAPI_KEY ayarlanmamis!")
        return
    if not TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN ayarlanmamis!")
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
