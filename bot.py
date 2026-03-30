import os
import json
import time
import re
import logging
import sqlite3
import calendar
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

# ---- Markdown escape helper ----
def escape_md(text):
    """Escape Telegram Markdown special characters."""
    if not text:
        return ""
    text = str(text)
    for ch in ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']:
        text = text.replace(ch, '\\' + ch)
    return text

# ---- Rate limiter ----
class RateLimiter:
    def __init__(self, max_calls, period_seconds):
        self.max_calls = max_calls
        self.period = period_seconds
        self.calls = []

    def allow(self):
        now = time.time()
        self.calls = [t for t in self.calls if now - t < self.period]
        if len(self.calls) < self.max_calls:
            self.calls.append(now)
            return True
        return False

api_limiter = RateLimiter(max_calls=50, period_seconds=60)

# ---- DB helpers ----
def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with get_db() as con:
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

# ---- API helpers ----
def api_headers():
    return {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": RAPIDAPI_HOST,
    }

def search_airport(query):
    if not api_limiter.allow():
        log.warning("Rate limit asildi, havaalani aramasi atlaniyor")
        return None
    url = "https://flights-sky.p.rapidapi.com/flights/auto-complete"
    params = {"query": query}
    try:
        r = requests.get(url, headers=api_headers(), params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        log.info(f"Havaalani aramasi '{query}': status={r.status_code}")
        results = data.get("data", [])
        if results:
            first = results[0]
            pres = first.get("presentation", {})
            nav = first.get("navigation", {})
            rel = nav.get("relevantFlightParams", {})
            sky_id = rel.get("skyId", "")
            entity_id = rel.get("entityId", "")
            log.info(f"Havaalani bulundu: {pres.get('title', query)} skyId={sky_id} entityId={entity_id}")
            return {
                "skyId": sky_id,
                "entityId": entity_id,
                "title": pres.get("title", query),
            }
        else:
            log.warning(f"'{query}' icin havaalani bulunamadi")
    except Exception as e:
        log.error(f"Havaalani arama hatasi: {e}")
    return None

def search_one_way(origin_sky_id, dest_sky_id, depart_date):
    if not api_limiter.allow():
        log.warning("Rate limit asildi, ucus aramasi atlaniyor")
        return None
    url = "https://flights-sky.p.rapidapi.com/flights/search-one-way"
    params = {
        "fromEntityId": origin_sky_id,
        "toEntityId": dest_sky_id,
        "departDate": depart_date,
        "market": "TR",
        "locale": "tr-TR",
        "currency": "TRY",
    }
    try:
        log.info(f"Ucus aranacak: {origin_sky_id} -> {dest_sky_id} tarih={depart_date}")
        r = requests.get(url, headers=api_headers(), params=params, timeout=30)
        r.raise_for_status()
        resp = r.json()
        status = resp.get("data", {}).get("context", {}).get("status", "unknown") if isinstance(resp.get("data"), dict) else "unknown"
        log.info(f"Ucus arama yanit durumu: {status}")
        return resp
    except Exception as e:
        log.error(f"Ucus arama hatasi: {e}")
    return None

def parse_itineraries(api_response):
    flights = []
    if not api_response:
        return flights
    data = api_response.get("data", {})
    if not data or not isinstance(data, dict):
        return flights
    itineraries = data.get("itineraries", [])
    if not itineraries:
        log.info(f"Yanit icinde sefer bulunamadi. Data anahtarlari: {list(data.keys())}")
        return flights
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
    log.info(f"{len(flights)} ucus parsed edildi")
    return flights

# ---- Telegram helpers ----
def send_telegram(text, chat_id=None, parse_mode="Markdown"):
    target = chat_id or TELEGRAM_CHAT_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": target, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        r = requests.post(url, json=payload, timeout=10)
        if not r.ok:
            log.error(f"Telegram gonderim hatasi: {r.status_code} {r.text}")
            if parse_mode:
                payload.pop("parse_mode", None)
                requests.post(url, json=payload, timeout=10)
    except Exception as e:
        log.error(f"Telegram gonderim hatasi: {e}")

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
        log.error(f"Telegram getUpdates hatasi: {e}")
        return []

# ---- Auth check ----
def is_authorized(chat_id):
    if not TELEGRAM_CHAT_ID:
        return True
    allowed = [cid.strip() for cid in TELEGRAM_CHAT_ID.split(",")]
    return str(chat_id) in allowed

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

    with get_db() as con:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO routes (origin, destination, date, origin_sky_id, dest_sky_id, origin_entity_id, dest_entity_id) VALUES (?,?,?,?,?,?,?)",
            (origin_q.upper(), dest_q.upper(), date_str, origin["skyId"], dest["skyId"], origin["entityId"], dest["entityId"]),
        )
        con.commit()
        route_id = cur.lastrowid
    return f"Rota eklendi (#{route_id}): {origin['title']} -> {dest['title']} | {date_str}"

def cmd_remove(args):
    if not args:
        return "Kullanim: /remove ROTA_ID"
    try:
        rid = int(args[0])
    except ValueError:
        return "Gecersiz ID."
    with get_db() as con:
        cur = con.cursor()
        cur.execute("DELETE FROM routes WHERE id=?", (rid,))
        deleted = cur.rowcount
        cur.execute("DELETE FROM price_history WHERE route_id=?", (rid,))
        con.commit()
    if deleted:
        return f"Rota #{rid} silindi."
    return f"Rota #{rid} bulunamadi."

def cmd_list():
    with get_db() as con:
        cur = con.cursor()
        cur.execute("SELECT id, origin, destination, date FROM routes")
        rows = cur.fetchall()
    if not rows:
        return "Takip edilen rota yok. /add ile ekleyin."
    lines = ["Takip Edilen Rotalar:"]
    for r in rows:
        lines.append(f"#{r[0]} {r[1]} -> {r[2]} Tarih: {r[3]}")
    return "\n".join(lines)

def cmd_check():
    with get_db() as con:
        cur = con.cursor()
        cur.execute("SELECT id, origin, destination, date, origin_sky_id, dest_sky_id FROM routes")
        rows = cur.fetchall()
    if not rows:
        return "Takip edilen rota yok."
    messages = []
    for r in rows:
        rid, origin, dest, date_str, o_sky, d_sky = r
        if not o_sky or not d_sky:
            messages.append(f"#{rid} {origin}->{dest} {date_str}: SkyID eksik, rota yeniden eklenmelidir.")
            continue
        api_resp = search_one_way(o_sky, d_sky, date_str)
        flights = parse_itineraries(api_resp)
        if not flights:
            messages.append(f"#{rid} {origin}->{dest} {date_str}: Ucus bulunamadi.")
            continue
        best = min(flights, key=lambda f: f["price"])
        line = f"#{rid} {origin}->{dest} {date_str}\nEn ucuz: {best['airline']} {best['price_formatted']} ({best['stops']} aktarma, {best['duration_min']}dk)"
        with get_db() as con2:
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
        messages.append(line)
    return "\n\n".join(messages) if messages else "Sonuc yok."

def cmd_prices(args):
    if len(args) < 3:
        return "Kullanim: /prices ORIGIN DEST YYYY-MM-DD\nOrnek: /prices ADB ECN 2026-05-08"
    origin_q, dest_q, date_str = args[0], args[1], args[2]

    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return "Tarih formati hatali. YYYY-MM-DD kullanin."

    TURKCE_AYLAR = [
        "", "Ocak", "Subat", "Mart", "Nisan", "Mayis", "Haziran",
        "Temmuz", "Agustos", "Eylul", "Ekim", "Kasim", "Aralik"
    ]

    origin = search_airport(origin_q)
    if not origin:
        return f"Kalkis havaalani bulunamadi: {origin_q}"
    dest = search_airport(dest_q)
    if not dest:
        return f"Varis havaalani bulunamadi: {dest_q}"

    log.info(f"/prices komutu: {origin_q} -> {dest_q} tarih={date_str}")

    api_resp = search_one_way(origin["skyId"], dest["skyId"], date_str)
    flights = parse_itineraries(api_resp)

    if not flights:
        return f"{origin_q.upper()} -> {dest_q.upper()} ({date_str}) icin ucus bulunamadi."

    # Havayoluna gore grupla
    airline_data = {}
    for f in flights:
        name = f["airline"]
        if name not in airline_data:
            airline_data[name] = []
        airline_data[name].append(f)

    rows = []
    for name, group in airline_data.items():
        cheapest = min(group, key=lambda x: x["price"])
        avg_price = sum(x["price"] for x in group) / len(group)

        # Aktarma bilgisi: gruptaki en ucuzun aktarma sayisi
        stop_txt = "Direkt" if cheapest["stops"] == 0 else str(cheapest["stops"])
        h, m = divmod(cheapest["duration_min"], 60)
        dur_txt = f"{h}s {m:02d}dk" if h else f"{m}dk"

        rows.append({
            "name": name,
            "cheapest": cheapest["price"],
            "avg": avg_price,
            "stop_txt": stop_txt,
            "dur_txt": dur_txt,
            "stops_raw": cheapest["stops"],
            "duration_min": cheapest["duration_min"],
        })

    # En ucuzdan pahaliya sirala
    rows.sort(key=lambda x: x["cheapest"])

    tarih_str = f"{dt.day} {TURKCE_AYLAR[dt.month]} {dt.year}"
    header = f"{origin_q.upper()} -> {dest_q.upper()} | {tarih_str}"
    col_header = f"{'Havayolu':<15}| {'En Ucuz':<9}| {'Ort.':<9}| {'Aktarma':<8}| Sure"
    sep = "-" * len(col_header)

    lines = [header, "", col_header, sep]
    for r in rows:
        cheapest_str = f"{r['cheapest']:,.0f} TL".replace(",", ".")
        avg_str = f"{r['avg']:,.0f} TL".replace(",", ".")
        lines.append(
            f"{r['name']:<15}| {cheapest_str:<9}| {avg_str:<9}| {r['stop_txt']:<8}| {r['dur_txt']}"
        )

    # En ucuz ozet
    best = rows[0]
    best_stop = "Direkt" if best["stops_raw"] == 0 else f"{best['stops_raw']} aktarma"
    best_price_str = f"{best['cheapest']:,.0f} TL".replace(",", ".")
    lines.append("")
    lines.append(f"\U0001f4b0 En ucuz: {best['name']} {best_price_str} ({best_stop}, {best['dur_txt']})")

    return "\n".join(lines)

def cmd_best(args):
    if len(args) < 3:
        return "Kullanim: /best ORIGIN DEST YYYY-MM\nOrnek: /best IST ECN 2026-06"
    origin_q, dest_q, month_str = args[0], args[1], args[2]

    TURKCE_AYLAR = [
        "", "Ocak", "Subat", "Mart", "Nisan", "Mayis", "Haziran",
        "Temmuz", "Agustos", "Eylul", "Ekim", "Kasim", "Aralik"
    ]

    try:
        parts = month_str.split("-")
        year = int(parts[0])
        month = int(parts[1])
        if month < 1 or month > 12:
            raise ValueError
    except (ValueError, IndexError):
        return "Ay formati hatali. YYYY-MM kullanin. Ornek: 2026-06"

    origin = search_airport(origin_q)
    if not origin:
        return f"Kalkis havaalani bulunamadi: {origin_q}"
    dest = search_airport(dest_q)
    if not dest:
        return f"Varis havaalani bulunamadi: {dest_q}"

    # Aydaki tum Cuma-Pazar ciftlerini bul
    _, days_in_month = calendar.monthrange(year, month)
    weekends = []
    for day in range(1, days_in_month + 1):
        dt = datetime(year, month, day)
        if dt.weekday() == 4:  # Cuma
            friday = dt
            sunday = friday + timedelta(days=2)
            # Pazar ay disina cikiyorsa atla
            if sunday.month != month:
                continue
            weekends.append((friday, sunday))

    if not weekends:
        return f"{TURKCE_AYLAR[month]} {year} icinde uygun hafta sonu bulunamadi."

    log.info(f"/best komutu: {origin_q} -> {dest_q} ay={month_str}, {len(weekends)} hafta sonu taranacak")

    # Kullaniciya bilgi mesaji
    scan_msg = f"\U0001f50d Taraniyor... {len(weekends)} hafta sonu kontrol edilecek"

    results = []
    for friday, sunday in weekends:
        fri_str = friday.strftime("%Y-%m-%d")
        sun_str = sunday.strftime("%Y-%m-%d")

        log.info(f"Gidis aranacak: {fri_str}")
        outbound_resp = search_one_way(origin["skyId"], dest["skyId"], fri_str)
        outbound_flights = parse_itineraries(outbound_resp)
        time.sleep(0.5)

        log.info(f"Donus aranacak: {sun_str}")
        return_resp = search_one_way(dest["skyId"], origin["skyId"], sun_str)
        return_flights = parse_itineraries(return_resp)
        time.sleep(0.5)

        if not outbound_flights or not return_flights:
            log.info(f"{fri_str}-{sun_str} icin ucus bulunamadi, atlanacak.")
            continue

        best_out = min(outbound_flights, key=lambda x: x["price"])
        best_ret = min(return_flights, key=lambda x: x["price"])
        total = best_out["price"] + best_ret["price"]

        results.append({
            "friday": friday,
            "sunday": sunday,
            "total": total,
            "outbound": best_out,
            "return": best_ret,
        })

    if not results:
        return f"{origin_q.upper()} -> {dest_q.upper()} | {TURKCE_AYLAR[month]} {year} icin ucus bulunamadi."

    # En ucuzdan pahaliya sirala
    results.sort(key=lambda x: x["total"])

    medals = ["\U0001f947", "\U0001f948", "\U0001f949"]  # gold, silver, bronze
    ay_adi = TURKCE_AYLAR[month]

    lines = [scan_msg, ""]
    lines.append(f"\u2708\ufe0f {origin_q.upper()} -> {dest_q.upper()} | {ay_adi} {year} Hafta Sonu Fiyatlari")
    lines.append("")

    for i, r in enumerate(results):
        medal = medals[i] if i < 3 else f" {i+1}."

        fri_day = r["friday"].day
        sun_day = r["sunday"].day

        total_str = f"{r['total']:,.0f} TL".replace(",", ".")

        out = r["outbound"]
        ret = r["return"]

        out_price_str = f"{out['price']:,.0f} TL".replace(",", ".")
        ret_price_str = f"{ret['price']:,.0f} TL".replace(",", ".")

        out_stop = "Direkt" if out["stops"] == 0 else f"{out['stops']} aktarma"
        ret_stop = "Direkt" if ret["stops"] == 0 else f"{ret['stops']} aktarma"

        h_out, m_out = divmod(out["duration_min"], 60)
        dur_out = f"{h_out}s {m_out:02d}dk" if h_out else f"{m_out}dk"

        h_ret, m_ret = divmod(ret["duration_min"], 60)
        dur_ret = f"{h_ret}s {m_ret:02d}dk" if h_ret else f"{m_ret}dk"

        lines.append(f"{medal} {fri_day}-{sun_day} {ay_adi}: {total_str}")
        lines.append(f"   Gidis: {out['airline']} {out_price_str} ({out_stop}, {dur_out})")
        lines.append(f"   Donus: {ret['airline']} {ret_price_str} ({ret_stop}, {dur_ret})")
        lines.append("")

    return "\n".join(lines).rstrip()

def cmd_help():
    return (
        "Ucus Takip Botu Komutlari:\n"
        "/add ORIGIN DEST YYYY-MM-DD - Rota ekle\n"
        "/remove ID - Rota sil\n"
        "/list - Takip edilen rotalari goster\n"
        "/check - Tum rotalarin guncel fiyatlarini kontrol et\n"
        "/prices ORIGIN DEST YYYY-MM-DD - Havayolu bazli fiyat tablosu\n"
        "/best ORIGIN DEST YYYY-MM - Aydaki en ucuz hafta sonu\n"
        "/help - Bu mesaji goster"
    )

# ---- Scheduled job ----
def check_all_routes():
    log.info("Zamanlanmis fiyat kontrolu basliyor...")
    try:
        result = cmd_check()
        if result and result != "Takip edilen rota yok.":
            send_telegram(result, parse_mode=None)
    except Exception as e:
        log.error(f"check_all_routes hatasi: {e}")
    log.info("Fiyat kontrolu tamamlandi.")

# ---- Telegram polling ----
LAST_UPDATE_ID = 0

def process_telegram_updates():
    global LAST_UPDATE_ID
    updates = get_updates(offset=LAST_UPDATE_ID + 1 if LAST_UPDATE_ID else None)
    for upd in updates:
        LAST_UPDATE_ID = upd["update_id"]
        msg = upd.get("message")
        if not msg:
            continue
        text = (msg.get("text") or "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))

        if not text.startswith("/"):
            continue

        if not is_authorized(chat_id):
            log.warning(f"Yetkisiz erisim denemesi, chat_id: {chat_id}")
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": chat_id, "text": "Bu botu kullanma yetkiniz yok."}
            try:
                requests.post(url, json=payload, timeout=10)
            except Exception:
                pass
            continue

        parts = text.split()
        command = parts[0].lower().split("@")[0]
        args = parts[1:]

        try:
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
                send_telegram(reply, chat_id=chat_id, parse_mode=None)
                continue
            elif command == "/best":
                reply = cmd_best(args)
                send_telegram(reply, chat_id=chat_id, parse_mode=None)
                continue
            elif command == "/help":
                reply = cmd_help()
            else:
                reply = "Bilinmeyen komut. /help yazin."
        except Exception as e:
            log.error(f"Komut hatasi ({command}): {e}")
            reply = f"Komut islenirken hata olustu: {e}"

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": chat_id, "text": reply}
        try:
            r = requests.post(url, json=payload, timeout=10)
            if not r.ok:
                log.error(f"Yanit gonderme hatasi: {r.status_code} {r.text}")
        except Exception as e:
            log.error(f"Yanit hatasi: {e}")

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
