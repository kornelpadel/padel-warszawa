"""
scraper_browser.py v3
=====================
Szczegółowy scraper kortów padlowych Warszawa – playtomic.com
Zbiera dane PER KORT, PER SLOT, z cenami wg pory dnia.

Uruchomienie:  python3 scraper_browser.py
"""

import asyncio, sqlite3, csv, logging, re, json
from datetime import datetime
from pathlib import Path

# ─── Konfiguracja ─────────────────────────────────────────────────────────────

DB_PATH  = Path("padel_warszawa.db")
CSV_DIR  = Path("csv_exports")
LOG_FILE = "scraper_browser.log"

KLUBY_WARSZAWA = [
    {"slug": "warsaw-padel-club",                 "name": "Warsaw Padel Club"},
    {"slug": "warsaw-padel-club-squash",          "name": "Warsaw Padel Club – Squash"},
    {"slug": "interpadel-warszawa",               "name": "Interpadel Warszawa"},
    {"slug": "rakiety-pge-narodowy",              "name": "Rakiety PGE Narodowy"},
    {"slug": "rakiety-squash-padel-outdoor",      "name": "Rakiety Aero – Padel Outdoor"},
    {"slug": "loba-padel",                        "name": "Loba Padel"},
    {"slug": "san-padel",                         "name": "San Padel"},
    {"slug": "rqt-spot",                          "name": "RQT Spot"},
]

# Pory dnia (do statystyk)
def pora_dnia(hour: int) -> str:
    if  6 <= hour < 10: return "rano (6-10)"
    if 10 <= hour < 14: return "poludnie (10-14)"
    if 14 <= hour < 18: return "popoludnie (14-18)"
    if 18 <= hour < 22: return "wieczor (18-22)"
    return "noc"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ─── Baza danych ──────────────────────────────────────────────────────────────

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        -- Kluby
        CREATE TABLE IF NOT EXISTS clubs (
            id              TEXT PRIMARY KEY,   -- np. "warsaw-padel-club"
            name            TEXT NOT NULL,
            address         TEXT,
            lat             REAL,
            lng             REAL,
            hours_weekday   TEXT,               -- "06:00-01:00"
            hours_weekend   TEXT,
            amenities       TEXT,               -- JSON lista
            website         TEXT,
            scraped_at      TEXT
        );

        -- Korty (per klub)
        CREATE TABLE IF NOT EXISTS courts (
            id              TEXT PRIMARY KEY,   -- "warsaw-padel-club__kort1"
            club_id         TEXT NOT NULL,
            court_name      TEXT,               -- "1 - Full Panoramic"
            surface_type    TEXT,               -- "indoor" / "outdoor"
            court_format    TEXT,               -- "single" / "double"
            court_style     TEXT,               -- "panoramic" / "standard"
            scraped_at      TEXT,
            FOREIGN KEY (club_id) REFERENCES clubs(id)
        );

        -- Sloty dostępności (per kort, per godzina, per dzień)
        CREATE TABLE IF NOT EXISTS slots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            club_id         TEXT NOT NULL,
            court_id        TEXT NOT NULL,
            slot_date       TEXT NOT NULL,      -- "2026-05-24"
            slot_hour       INTEGER NOT NULL,   -- 6..23
            pora_dnia       TEXT,               -- "rano" / "poludnie" itd.
            day_of_week     TEXT,               -- "Monday" / "Sunday"
            is_weekend      INTEGER,            -- 0/1
            is_free         INTEGER,            -- 0/1
            scraped_at      TEXT,
            FOREIGN KEY (club_id)  REFERENCES clubs(id),
            FOREIGN KEY (court_id) REFERENCES courts(id)
        );

        -- Ceny z akademii / turniejów (te są najbardziej wiarygodne)
        CREATE TABLE IF NOT EXISTS prices (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            club_id         TEXT NOT NULL,
            price_type      TEXT,               -- "court" / "academy" / "tournament" / "karnet"
            price_pln       REAL NOT NULL,
            duration_min    INTEGER,
            description     TEXT,               -- np. "Trening B2/B3 1.5h"
            scraped_at      TEXT,
            FOREIGN KEY (club_id) REFERENCES clubs(id)
        );

        CREATE INDEX IF NOT EXISTS idx_slots_club    ON slots(club_id, slot_date);
        CREATE INDEX IF NOT EXISTS idx_slots_hour    ON slots(slot_hour);
        CREATE INDEX IF NOT EXISTS idx_slots_court   ON slots(court_id);
        CREATE INDEX IF NOT EXISTS idx_prices_club   ON prices(club_id);
    """)
    conn.commit()
    return conn

# ─── Scraper ──────────────────────────────────────────────────────────────────

async def scrape_club(page, klub: dict) -> dict:
    """Zbiera WSZYSTKIE szczegółowe dane o klubie."""
    url = f"https://playtomic.com/clubs/{klub['slug']}"
    result = {
        "name": klub["name"], "address": "", "lat": None, "lng": None,
        "hours_weekday": "", "hours_weekend": "", "amenities": [],
        "courts": [], "prices": [], "ok": False,
    }

    try:
        log.info("  → %s", klub["name"])
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if resp and resp.status == 404:
            log.warning("    404 – pomijam")
            return result
        await page.wait_for_timeout(3500)

        # ── ADRES + GPS ──────────────────────────────────────────────────────
        # GPS jest w URL mapy Google: maps/api/staticmap?...markers=...lat,lng
        html = await page.content()
        gps_match = re.search(r"markers=color[^|]+\|([0-9.]+),([0-9.]+)", html)
        if gps_match:
            result["lat"] = float(gps_match.group(1))
            result["lng"] = float(gps_match.group(2))

        # Adres z tekstu pod mapą
        addr_els = await page.query_selector_all("address, [class*='address'], [class*='location']")
        for el in addr_els:
            t = (await el.inner_text()).strip()
            if t and len(t) > 5:
                result["address"] = t[:200]
                break
        if not result["address"]:
            # fallback: szukamy kodu pocztowego w treści
            m = re.search(r"(\d{2}-\d{3}[^\n]{0,60})", await page.inner_text("body"))
            if m:
                result["address"] = m.group(1).strip()[:200]

        # ── GODZINY OTWARCIA ─────────────────────────────────────────────────
        body_text = await page.inner_text("body")
        # Szukamy wzorców "Monday 06:00 - 01:00" itd.
        hours_matches = re.findall(
            r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+(\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2})",
            body_text
        )
        hours_dict = {day: hours for day, hours in hours_matches}
        if hours_dict:
            weekdays = ["Monday","Tuesday","Wednesday","Thursday","Friday"]
            weekend  = ["Saturday","Sunday"]
            wd = next((hours_dict[d] for d in weekdays if d in hours_dict), "")
            we = next((hours_dict[d] for d in weekend  if d in hours_dict), "")
            result["hours_weekday"] = wd
            result["hours_weekend"] = we

        # ── UDOGODNIENIA ─────────────────────────────────────────────────────
        amenity_keywords = [
            "Disabled Access","Equipment Rental","Free Parking","Private Parking",
            "Store","Restaurant","Cafeteria","Snack Bar","Vending Machine",
            "Changing Room","Lockers","WiFi","Showers",
        ]
        result["amenities"] = [a for a in amenity_keywords if a.lower() in body_text.lower()]

        # ── KORTY – siatka dostępności ───────────────────────────────────────
        # Szukamy wierszy siatki: każdy wiersz = jeden kort
        # Format HTML: <div> NazwaKortu + "indoor, double, panoramic" + bloki godzin
        courts_found = []

        # Metoda 1: sekcja "Available courts" – szukamy opisu kortów
        court_desc_pattern = re.findall(
            r"(\d+\s*[-–]\s*[^\n]+?)\s*\n\s*((?:indoor|outdoor)[^\n]*)",
            body_text,
            re.IGNORECASE
        )
        for name_raw, desc_raw in court_desc_pattern:
            name = name_raw.strip()[:100]
            desc = desc_raw.lower()
            surface = "indoor" if "indoor" in desc else ("outdoor" if "outdoor" in desc else "unknown")
            fmt     = "single" if "single" in desc else "double"
            style   = "panoramic" if "panoramic" in desc else "standard"
            courts_found.append({
                "name": name,
                "surface_type": surface,
                "court_format": fmt,
                "court_style": style,
            })

        # Metoda 2: szukamy bezpośrednio w DOM elementów kortów
        if not courts_found:
            court_els = await page.query_selector_all(
                "[class*='court'], [class*='resource'], [class*='lane']"
            )
            for el in court_els:
                t = (await el.inner_text()).strip()
                if t and len(t) > 2:
                    desc = t.lower()
                    surface = "indoor" if "indoor" in desc else ("outdoor" if "outdoor" in desc else "unknown")
                    fmt     = "single" if "single" in desc else "double"
                    style   = "panoramic" if "panoramic" in desc else "standard"
                    courts_found.append({
                        "name": t[:100],
                        "surface_type": surface,
                        "court_format": fmt,
                        "court_style": style,
                    })

        result["courts"] = courts_found
        log.info("    Korty: %d znaleziono", len(courts_found))

        # ── SLOTY DOSTĘPNOŚCI ────────────────────────────────────────────────
        # Szukamy elementów siatki godzinowej
        slot_els = await page.query_selector_all(
            "button[class*='slot'], div[class*='slot'], "
            "[class*='time-cell'], [class*='timeslot'], "
            "[aria-label*='AM'], [aria-label*='PM'], "
            "[class*='available'], [class*='booked']"
        )

        slots_raw = []
        for el in slot_els:
            aria  = await el.get_attribute("aria-label") or ""
            cls   = await el.get_attribute("class") or ""
            txt   = (await el.inner_text()).strip()
            combined = (aria + " " + txt).strip()

            time_m = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", combined, re.IGNORECASE)
            if not time_m:
                continue
            h, m_str, ampm = int(time_m.group(1)), time_m.group(2), (time_m.group(3) or "")
            if ampm.upper() == "PM" and h != 12:
                h += 12
            if ampm.upper() == "AM" and h == 12:
                h = 0

            is_free = not any(w in cls.lower() for w in
                              ["disabled","unavailable","booked","full","occupied","taken"])
            slots_raw.append({"hour": h, "free": is_free})

        result["slots_raw"] = slots_raw
        log.info("    Sloty: %d znaleziono (%d wolnych)",
                 len(slots_raw), sum(1 for s in slots_raw if s["free"]))

        # ── CENY ─────────────────────────────────────────────────────────────
        prices_found = []

        # Ceny z akademii (najdokładniejsze – są przy konkretnych treningach)
        academy_pattern = re.findall(
            r"(Trening[^\n]{0,60}|Academy[^\n]{0,40}|Lekcja[^\n]{0,40})"
            r"[^\n]*?(\d+)\s*(?:min)[^\n]*?(\d+)\s*PLN",
            body_text, re.IGNORECASE
        )
        for desc, dur, price in academy_pattern:
            prices_found.append({
                "price_type": "academy",
                "price_pln": float(price),
                "duration_min": int(dur),
                "description": desc.strip()[:150],
            })

        # Ceny z turniejów
        tournament_pattern = re.findall(
            r"(Americano|Mexicano|Tournament|Turniej)[^\n]{0,60}\n[^\n]*?(\d{2,3})\s*PLN",
            body_text, re.IGNORECASE
        )
        for desc, price in tournament_pattern:
            prices_found.append({
                "price_type": "tournament",
                "price_pln": float(price),
                "duration_min": None,
                "description": desc.strip()[:150],
            })

        # Ceny ogólne (fallback) – PLN od 50 do 450 (odfiltruj karnety 500+)
        general_prices = re.findall(r"(\d{2,3})\s*PLN", body_text)
        seen = {p["price_pln"] for p in prices_found}
        for p_str in general_prices:
            p = float(p_str)
            if 50 <= p <= 450 and p not in seen:
                seen.add(p)
                prices_found.append({
                    "price_type": "court",
                    "price_pln": p,
                    "duration_min": 90,
                    "description": "cena z tekstu strony",
                })

        # Karnety (500+ PLN)
        karnet_pattern = re.findall(r"(\d{3,4})\s*PLN[^\n]{0,30}(?:karnet|doładow)", body_text, re.IGNORECASE)
        for p_str in karnet_pattern:
            prices_found.append({
                "price_type": "karnet",
                "price_pln": float(p_str),
                "duration_min": None,
                "description": "karnet / doładowanie",
            })

        result["prices"] = prices_found
        log.info("    Ceny: %s PLN", sorted({p["price_pln"] for p in prices_found}))
        result["ok"] = True

    except Exception as e:
        log.error("    Błąd: %s", e)

    return result


# ─── Zapis do bazy ────────────────────────────────────────────────────────────

def save_all(conn, klub_slug: str, data: dict, today: str):
    now = datetime.now().isoformat()
    dt  = datetime.strptime(today, "%Y-%m-%d")
    dow = dt.strftime("%A")
    is_weekend = 1 if dt.weekday() >= 5 else 0

    # Klub
    conn.execute("""
        INSERT INTO clubs (id, name, address, lat, lng, hours_weekday, hours_weekend,
                           amenities, website, scraped_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, address=excluded.address,
            lat=excluded.lat, lng=excluded.lng,
            hours_weekday=excluded.hours_weekday,
            hours_weekend=excluded.hours_weekend,
            amenities=excluded.amenities, scraped_at=excluded.scraped_at
    """, (
        klub_slug, data["name"], data["address"], data["lat"], data["lng"],
        data["hours_weekday"], data["hours_weekend"],
        json.dumps(data["amenities"], ensure_ascii=False),
        f"https://playtomic.com/clubs/{klub_slug}", now,
    ))

    # Korty
    for i, c in enumerate(data["courts"]):
        court_id = f"{klub_slug}__court{i+1}"
        conn.execute("""
            INSERT INTO courts (id, club_id, court_name, surface_type, court_format, court_style, scraped_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                court_name=excluded.court_name,
                surface_type=excluded.surface_type,
                court_format=excluded.court_format,
                court_style=excluded.court_style,
                scraped_at=excluded.scraped_at
        """, (court_id, klub_slug, c["name"], c["surface_type"],
              c["court_format"], c["court_style"], now))

    # Sloty – przypisuj do kortów równomiernie jeśli mamy korty, albo do "court0"
    slots_raw = data.get("slots_raw", [])
    courts_list = data["courts"]
    n_courts = len(courts_list) if courts_list else 1

    for idx, s in enumerate(slots_raw):
        # Przypisz slot do kortu (round-robin jeśli nie ma lepszej info)
        c_idx = idx % n_courts
        court_id = f"{klub_slug}__court{c_idx+1}" if courts_list else f"{klub_slug}__court0"
        conn.execute("""
            INSERT INTO slots (club_id, court_id, slot_date, slot_hour, pora_dnia,
                               day_of_week, is_weekend, is_free, scraped_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (klub_slug, court_id, today, s["hour"], pora_dnia(s["hour"]),
              dow, is_weekend, 1 if s["free"] else 0, now))

    # Ceny
    conn.execute("DELETE FROM prices WHERE club_id=?", (klub_slug,))
    for p in data["prices"]:
        conn.execute("""
            INSERT INTO prices (club_id, price_type, price_pln, duration_min, description, scraped_at)
            VALUES (?,?,?,?,?,?)
        """, (klub_slug, p["price_type"], p["price_pln"],
              p.get("duration_min"), p.get("description",""), now))

    conn.commit()


# ─── Eksport CSV ──────────────────────────────────────────────────────────────

def export_csv(conn):
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    CSV_DIR.mkdir(exist_ok=True)

    queries = {
        "1_kluby": """
            SELECT id, name, address, lat, lng, hours_weekday, hours_weekend,
                   amenities, website, scraped_at
            FROM clubs ORDER BY name
        """,
        "2_korty": """
            SELECT c.name AS klub, r.court_name, r.surface_type,
                   r.court_format, r.court_style
            FROM courts r JOIN clubs c ON c.id=r.club_id
            ORDER BY c.name, r.court_name
        """,
        "3_ceny": """
            SELECT c.name AS klub, p.price_type, p.price_pln,
                   p.duration_min, p.description
            FROM prices p JOIN clubs c ON c.id=p.club_id
            WHERE p.price_type != 'karnet'
            ORDER BY c.name, p.price_pln
        """,
        "4_oblozenie_per_godzina": """
            SELECT
                c.name                              AS klub,
                s.slot_date                         AS data,
                s.day_of_week                       AS dzien_tygodnia,
                CASE s.is_weekend WHEN 1 THEN 'weekend' ELSE 'tydzien' END AS typ_dnia,
                s.slot_hour                         AS godzina,
                s.pora_dnia,
                r.surface_type                      AS nawierzchnia,
                r.court_format                      AS format_kortu,
                COUNT(*)                            AS slotow_lacznie,
                SUM(CASE WHEN s.is_free=0 THEN 1 ELSE 0 END) AS slotow_zajętych,
                SUM(s.is_free)                      AS slotow_wolnych,
                ROUND(100.0 * SUM(CASE WHEN s.is_free=0 THEN 1 ELSE 0 END) / COUNT(*), 1)
                                                    AS oblozenie_pct
            FROM slots s
            JOIN clubs  c ON c.id = s.club_id
            JOIN courts r ON r.id = s.court_id
            GROUP BY c.name, s.slot_date, s.slot_hour, r.surface_type, r.court_format
            ORDER BY c.name, s.slot_date, s.slot_hour
        """,
        "5_oblozenie_per_pora_dnia": """
            SELECT
                c.name                              AS klub,
                s.pora_dnia,
                r.surface_type                      AS nawierzchnia,
                COUNT(*)                            AS slotow_lacznie,
                SUM(CASE WHEN s.is_free=0 THEN 1 ELSE 0 END) AS zajete,
                ROUND(100.0 * SUM(CASE WHEN s.is_free=0 THEN 1 ELSE 0 END) / COUNT(*), 1)
                                                    AS oblozenie_pct
            FROM slots s
            JOIN clubs  c ON c.id = s.club_id
            JOIN courts r ON r.id = s.court_id
            GROUP BY c.name, s.pora_dnia, r.surface_type
            ORDER BY c.name,
                CASE s.pora_dnia
                    WHEN 'rano (6-10)'        THEN 1
                    WHEN 'poludnie (10-14)'   THEN 2
                    WHEN 'popoludnie (14-18)' THEN 3
                    WHEN 'wieczor (18-22)'    THEN 4
                    ELSE 5 END
        """,
        "6_porownanie_klubow": """
            SELECT
                c.name                              AS klub,
                r.surface_type                      AS nawierzchnia,
                COUNT(DISTINCT r.id)                AS liczba_kortow,
                MIN(p.price_pln)                    AS cena_min,
                MAX(p.price_pln)                    AS cena_max,
                ROUND(AVG(p.price_pln), 0)          AS cena_srednia,
                c.hours_weekday                     AS godziny_tydzien,
                c.hours_weekend                     AS godziny_weekend
            FROM clubs c
            LEFT JOIN courts  r ON r.club_id = c.id
            LEFT JOIN prices  p ON p.club_id = c.id AND p.price_type = 'court'
            GROUP BY c.name, r.surface_type
            ORDER BY c.name
        """,
    }

    exported = []
    for name, query in queries.items():
        path = CSV_DIR / f"{name}_{ts}.csv"
        try:
            rows = conn.execute(query).fetchall()
            if not rows:
                continue
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow([d[0] for d in conn.execute(query).description])
                writer.writerows(rows)
            exported.append((path.name, len(rows)))
            log.info("Eksport: %s (%d wierszy)", path.name, len(rows))
        except Exception as e:
            log.warning("Błąd eksportu %s: %s", name, e)

    return exported


# ─── Główna pętla ─────────────────────────────────────────────────────────────

async def run():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("\n❌ Brakuje Playwright. Uruchom najpierw:\n    python3 setup.py\n")
        return

    conn = init_db()
    today = datetime.today().strftime("%Y-%m-%d")

    print("\n" + "═"*58)
    print("  Skraper kortów padlowych – Warszawa  v3")
    print("  " + datetime.now().strftime("%Y-%m-%d %H:%M"))
    print("═"*58 + "\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context(
            locale="pl-PL",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await ctx.new_page()

        for klub in KLUBY_WARSZAWA:
            data = await scrape_club(page, klub)
            if data["ok"]:
                save_all(conn, klub["slug"], data, today)
            await page.wait_for_timeout(2000)

        await browser.close()

    exported = export_csv(conn)

    # ── Podsumowanie ──────────────────────────────────────────────────────────
    n_clubs  = conn.execute("SELECT COUNT(*) FROM clubs").fetchone()[0]
    n_courts = conn.execute("SELECT COUNT(*) FROM courts").fetchone()[0]
    n_slots  = conn.execute("SELECT COUNT(*) FROM slots WHERE slot_date=?", (today,)).fetchone()[0]
    n_free   = conn.execute("SELECT COUNT(*) FROM slots WHERE slot_date=? AND is_free=1", (today,)).fetchone()[0]
    prices   = conn.execute("SELECT MIN(price_pln),MAX(price_pln),AVG(price_pln) FROM prices WHERE price_type='court'").fetchone()

    print("\n" + "═"*58)
    print("  WYNIKI")
    print("═"*58)
    print(f"  📍 Klubów:           {n_clubs}")
    print(f"  🎾 Kortów:           {n_courts}")
    print(f"  📊 Slotów dziś:      {n_slots}  ({n_free} wolnych, {n_slots-n_free} zajętych)")
    if prices[0]:
        print(f"  💰 Ceny kortów:      {prices[0]:.0f}–{prices[1]:.0f} zł  (śr. {prices[2]:.0f} zł)")
    print(f"\n  📁 Wyeksportowano {len(exported)} plików CSV:")
    for fname, rows in exported:
        print(f"     • {fname}  ({rows} wierszy)")
    print(f"\n  Folder: {CSV_DIR.resolve()}")
    print("═"*58 + "\n")
    conn.close()


if __name__ == "__main__":
    asyncio.run(run())
