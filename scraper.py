"""
scraper.py – Master skrypt zbierający dane o kortach padlowych w Warszawie
==========================================================================
Źródła:
  1. Playtomic.com  – sloty dostępności + ceny (bez logowania)
  2. Kluby.org      – pełny grafik dnia z logowaniem (zajęte + wolne)

Uruchomienie:
    python3 scraper.py

Skrypt pyta o login/hasło kluby.org raz na początku.
Hasło NIE jest nigdzie zapisywane.
"""

import asyncio, sqlite3, csv, logging, re, json, getpass, os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

WARSAW_TZ = ZoneInfo("Europe/Warsaw")

DB_PATH  = Path("padel_warszawa.db")
CSV_DIR  = Path("csv_exports")
LOG_FILE = "scraper.log"

# ─── Kompletna lista klubów ───────────────────────────────────────────────────

# Playtomic: publiczne API api.playtomic.io — dokładne ceny per slot,
# prawdziwa liczba kortów, indoor/outdoor (od bieżącej godziny wzwyż)
PLAYTOMIC = [
    {"slug": "warsaw-padel-club",   "name": "Warsaw Padel Club",          "dzielnica": "Białołęka",     "tenant_id": "e7284c78-e269-44ad-8f3d-a4d63089c80c"},
    {"slug": "interpadel-warszawa", "name": "Interpadel Warszawa",        "dzielnica": "Mokotów",       "tenant_id": "057c5f40-f54b-4e4d-977c-1f9547a25076"},
    {"slug": "rakiety-pge-narodowy","name": "Rakiety PGE Narodowy",       "dzielnica": "Praga Południe","tenant_id": "153bbff6-abf6-4ffe-ad93-ba1045e9d43b"},
    {"slug": "rakiety-aero-outdoor","name": "Rakiety Aero Padel Outdoor", "dzielnica": "Wawer",         "tenant_id": "f3f86625-3c23-41fd-be77-526395fabe74"},
    {"slug": "loba-padel",          "name": "Loba Padel",                 "dzielnica": "Białołęka",     "tenant_id": "3ae6a706-eba4-42be-9cb3-074c7ade27bb"},
    {"slug": "san-padel",           "name": "San Padel",                  "dzielnica": "Ursynów",       "tenant_id": "f690a458-011d-4ad2-88c5-e8d175ccc31c"},
    {"slug": "rqt-spot",            "name": "RQT Spot",                   "dzielnica": "Bielany",       "tenant_id": "44340c7a-0951-47bd-8a7e-ccbe0703cdc3"},
]

# Kluby.org: pełny grafik dnia (cały dzień z historią) – wymaga logowania
KLUBYORG = [
    {"slug": "padlovnia",       "name": "Padlovnia",           "dzielnica": "Ursynów",
     "extra_courts": [{"name": f"Outdoor {i+1}", "surface_type": "outdoor",
                        "court_format": "double"} for i in range(4)]},
    {"slug": "mana-padel",      "name": "Mana Padel",          "dzielnica": "Wilanów"},
    {"slug": "toro-padel",      "name": "Toro Padel",          "dzielnica": "Bemowo"},
    {"slug": "mera",            "name": "WKT Mera",            "dzielnica": "Ochota"},
    {"slug": "tenes",           "name": "TENES Klub Sportowy", "dzielnica": "Ursus"},
    {"slug": "sporteum",        "name": "Sporteum",            "dzielnica": "Białołęka"},
    {"slug": "teniswil",        "name": "TenisWil",            "dzielnica": "Wilanów"},
    {"slug": "bulwary-wislane", "name": "Padel4All Bulwary",   "dzielnica": "Śródmieście"},
    {"slug": "miedzeszyn",      "name": "Klub Miedzeszyn",     "dzielnica": "Wawer"},
    {"slug": "propadel",        "name": "ProPadel Jutrzenki",  "dzielnica": "Włochy"},
]

def pora_dnia(hour: int) -> str:
    if  6 <= hour < 10: return "rano (6-10)"
    if 10 <= hour < 14: return "poludnie (10-14)"
    if 14 <= hour < 18: return "popoludnie (14-18)"
    if 18 <= hour < 22: return "wieczor (18-22)"
    return "noc"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ─── Baza danych ──────────────────────────────────────────────────────────────

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS clubs (
            id                 TEXT PRIMARY KEY,
            name               TEXT NOT NULL,
            source             TEXT,           -- 'playtomic'/'kluby_org'/'oba'
            dzielnica          TEXT,
            address            TEXT,
            lat                REAL,
            lng                REAL,
            padel_courts_total INTEGER,
            hours_weekday      TEXT,
            hours_weekend      TEXT,
            amenities          TEXT,           -- JSON
            website            TEXT,
            phone              TEXT,
            scraped_at         TEXT
        );

        CREATE TABLE IF NOT EXISTS courts (
            id           TEXT PRIMARY KEY,
            club_id      TEXT NOT NULL,
            court_name   TEXT,
            surface_type TEXT,                 -- 'indoor'/'outdoor'/'unknown'
            court_format TEXT DEFAULT 'double',-- 'single'/'double'
            court_style  TEXT DEFAULT 'standard',
            scraped_at   TEXT,
            FOREIGN KEY (club_id) REFERENCES clubs(id)
        );

        -- Sloty: każdy 30-minutowy lub godzinowy przedział per kort
        CREATE TABLE IF NOT EXISTS slots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            club_id       TEXT NOT NULL,
            court_id      TEXT,
            court_name    TEXT,
            source        TEXT,               -- 'playtomic'/'kluby_org'
            slot_date     TEXT NOT NULL,
            slot_hour     INTEGER NOT NULL,
            slot_minute   INTEGER DEFAULT 0,
            slot_time     TEXT,               -- "09:30"
            pora_dnia     TEXT,
            day_of_week   TEXT,
            is_weekend    INTEGER,
            is_free       INTEGER NOT NULL,   -- 1=wolny, 0=zajęty
            booking_start TEXT,              -- "09:00" jeśli zajęty blok
            booking_end   TEXT,              -- "11:00" jeśli zajęty blok
            scraped_at    TEXT,
            FOREIGN KEY (club_id) REFERENCES clubs(id)
        );

        CREATE TABLE IF NOT EXISTS prices (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            club_id      TEXT NOT NULL,
            price_type   TEXT DEFAULT 'court',-- 'court'/'academy'/'tournament'/'karnet'
            day_type     TEXT DEFAULT 'all',  -- 'weekday'/'weekend'/'all'
            time_slot    TEXT DEFAULT 'all',  -- 'rano'/'popoludnie'/'wieczor'/'all'
            hour_from    INTEGER,
            hour_to      INTEGER,
            price_pln    REAL NOT NULL,
            duration_min INTEGER DEFAULT 90,
            description  TEXT,
            scraped_at   TEXT,
            FOREIGN KEY (club_id) REFERENCES clubs(id)
        );

        CREATE INDEX IF NOT EXISTS idx_slots_club_date ON slots(club_id, slot_date);
        CREATE INDEX IF NOT EXISTS idx_slots_hour      ON slots(slot_hour);
        CREATE INDEX IF NOT EXISTS idx_slots_source    ON slots(source);
        CREATE INDEX IF NOT EXISTS idx_courts_club     ON courts(club_id);
        CREATE INDEX IF NOT EXISTS idx_prices_club     ON prices(club_id);
    """)
    conn.commit()
    return conn


def upsert_club(conn, d: dict):
    conn.execute("""
        INSERT INTO clubs (id,name,source,dzielnica,address,lat,lng,
            padel_courts_total,hours_weekday,hours_weekend,amenities,
            website,phone,scraped_at)
        VALUES (:id,:name,:source,:dzielnica,:address,:lat,:lng,
            :padel_courts_total,:hours_weekday,:hours_weekend,:amenities,
            :website,:phone,:scraped_at)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,
            source=CASE WHEN excluded.source!=clubs.source AND clubs.source!='oba'
                        THEN 'oba' ELSE excluded.source END,
            dzielnica=COALESCE(excluded.dzielnica,clubs.dzielnica),
            address=COALESCE(NULLIF(excluded.address,''),clubs.address),
            lat=COALESCE(excluded.lat,clubs.lat),
            lng=COALESCE(excluded.lng,clubs.lng),
            padel_courts_total=COALESCE(excluded.padel_courts_total,clubs.padel_courts_total),
            hours_weekday=COALESCE(NULLIF(excluded.hours_weekday,''),clubs.hours_weekday),
            hours_weekend=COALESCE(NULLIF(excluded.hours_weekend,''),clubs.hours_weekend),
            amenities=COALESCE(excluded.amenities,clubs.amenities),
            website=COALESCE(excluded.website,clubs.website),
            phone=COALESCE(excluded.phone,clubs.phone),
            scraped_at=excluded.scraped_at
    """, d)
    conn.commit()


def save_courts(conn, club_id, courts):
    now = datetime.now().isoformat()
    for i, c in enumerate(courts):
        cid = f"{club_id}__c{i+1}"
        conn.execute("""
            INSERT INTO courts (id,club_id,court_name,surface_type,court_format,court_style,scraped_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                court_name=excluded.court_name, surface_type=excluded.surface_type,
                court_format=excluded.court_format, scraped_at=excluded.scraped_at
        """, (cid, club_id, c["name"], c.get("surface_type","unknown"),
              c.get("court_format","double"), c.get("court_style","standard"), now))
    conn.commit()


def save_slots(conn, club_id, date_str, slots, courts, source):
    now_dt = datetime.now(WARSAW_TZ)
    now    = now_dt.isoformat()
    dt     = datetime.strptime(date_str, "%Y-%m-%d")
    dow    = dt.strftime("%A")
    is_weekend = 1 if dt.weekday() >= 5 else 0
    # Usuń tylko sloty od bieżącej godziny wzwyż — zachowaj dane z wcześniejszych godzin
    # (przy wielokrotnym uruchomieniu w ciągu dnia historia porannych slotów nie ginie)
    conn.execute("""
        DELETE FROM slots
        WHERE club_id=? AND slot_date=? AND source=?
          AND (slot_hour > ? OR (slot_hour = ? AND slot_minute >= ?))
    """, (club_id, date_str, source,
          now_dt.hour, now_dt.hour, now_dt.minute))
    n = max(len(courts), 1)
    for idx, s in enumerate(slots):
        c_idx    = s.get("court_idx", idx % n)
        c_idx    = min(c_idx, n-1)
        court_id = f"{club_id}__c{c_idx+1}" if courts else None
        c_name   = courts[c_idx]["name"] if courts and c_idx < len(courts) else s.get("court_name")
        hour     = s["hour"]
        minute   = s.get("minute", 0)
        conn.execute("""
            INSERT INTO slots (club_id,court_id,court_name,source,slot_date,
                slot_hour,slot_minute,slot_time,pora_dnia,day_of_week,
                is_weekend,is_free,booking_start,booking_end,scraped_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (club_id, court_id, c_name, source, date_str,
              hour, minute, f"{hour:02d}:{minute:02d}",
              pora_dnia(hour), dow, is_weekend, s["is_free"],
              s.get("booking_start"), s.get("booking_end"), now))
    conn.commit()


def save_prices(conn, club_id, prices):
    now = datetime.now().isoformat()
    conn.execute("DELETE FROM prices WHERE club_id=?", (club_id,))
    for p in prices:
        conn.execute("""
            INSERT INTO prices (club_id,price_type,day_type,time_slot,
                hour_from,hour_to,price_pln,duration_min,description,scraped_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (club_id, p.get("price_type","court"), p.get("day_type","all"),
              p.get("time_slot","all"), p.get("hour_from"), p.get("hour_to"),
              p["price_pln"], p.get("duration_min",90), p.get("description",""), now))
    conn.commit()


# ─── PLAYTOMIC (publiczne API) ────────────────────────────────────────────────

PLAYTOMIC_API = "https://api.playtomic.io/v1"
PT_HEADERS    = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
WEEKDAY_API   = ["MONDAY","TUESDAY","WEDNESDAY","THURSDAY","FRIDAY","SATURDAY","SUNDAY"]


def _pt_get(path, params=None):
    r = requests.get(f"{PLAYTOMIC_API}/{path}", params=params, headers=PT_HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def _parse_price(s):
    m = re.search(r"(\d+(?:[.,]\d+)?)", s or "")
    return float(m.group(1).replace(",", ".")) if m else None


def build_playtomic_data(klub, date_str):
    """Pobiera metadane klubu i dostępność z publicznego API Playtomic.

    Obłożenie wyprowadzamy z siatki potencjalnych slotów (godziny otwarcia × korty,
    co 30 min, od bieżącej godziny wzwyż): slot jest wolny, gdy API zwróciło dla
    danego kortu możliwy start o tej godzinie — w przeciwnym razie jest zajęty.
    """
    out = {"name":"", "courts":[], "slots":[], "prices":[], "address":"",
           "lat":None, "lng":None, "hours_weekday":"", "hours_weekend":"",
           "amenities":[], "ok":False}
    tid = klub.get("tenant_id")
    if not tid:
        log.warning("    brak tenant_id – pomijam %s", klub["slug"]); return out
    try:
        tenant = _pt_get(f"tenants/{tid}")
        out["name"] = tenant.get("tenant_name") or klub["name"]

        addr  = tenant.get("address") or {}
        out["address"] = ", ".join(x for x in (addr.get("street"), addr.get("postal_code")) if x)[:200]
        coord = addr.get("coordinate") or {}
        out["lat"], out["lng"] = coord.get("lat"), coord.get("lon")

        oh = tenant.get("opening_hours") or {}
        def fmt(day):
            d = oh.get(day) or {}
            return f"{d['opening_time']}-{d['closing_time']}" if d.get("opening_time") else ""
        out["hours_weekday"] = fmt("MONDAY")
        out["hours_weekend"] = fmt("SATURDAY")

        # Korty padlowe (kolejność = court_idx używany przez save_slots/save_courts)
        resources = [r for r in (tenant.get("resources") or [])
                     if r.get("sport_id") == "PADEL" and r.get("is_active", True)]
        res_idx = {}
        for i, r in enumerate(resources):
            props = r.get("properties") or {}
            out["courts"].append({
                "name": (r.get("name") or f"Kort {i+1}")[:100],
                "surface_type": props.get("resource_type") or "unknown",
                "court_format": props.get("resource_size") or "double",
                "court_style":  props.get("resource_feature") or "standard",
            })
            res_idx[r.get("resource_id")] = i

        # Dostępność: wolne starty per kort + ceny per slot
        avail = _pt_get("availability", {
            "user_id": "me", "tenant_id": tid, "sport_id": "PADEL",
            "local_start_min": f"{date_str}T00:00:00",
            "local_start_max": f"{date_str}T23:59:59",
        })
        free       = {}    # court_idx -> set((hour, minute))
        seen_price = set()
        for entry in avail:
            cidx = res_idx.get(entry.get("resource_id"))
            if cidx is None: continue
            for sl in entry.get("slots") or []:
                tm = re.match(r"(\d{1,2}):(\d{2})", sl.get("start_time") or "")
                if not tm: continue
                h, mnt = int(tm.group(1)), int(tm.group(2))
                free.setdefault(cidx, set()).add((h, mnt))
                price = _parse_price(sl.get("price"))
                dur   = int(sl.get("duration") or 90)
                if price and 30 <= price <= 600 and (dur, price) not in seen_price:
                    seen_price.add((dur, price))
                    out["prices"].append({"price_type":"court", "price_pln":price,
                                          "duration_min":dur, "description":"cena z Playtomic API"})

        # Siatka potencjalnych slotów na dziś (co 30 min), tylko od bieżącej godziny.
        # Budujemy ją tylko gdy API w ogóle zwróciło dostępność (avail) — inaczej
        # klub bez rezerwacji online dostałby fałszywe 100% obłożenia.
        dow   = WEEKDAY_API[datetime.strptime(date_str, "%Y-%m-%d").weekday()]
        today = oh.get(dow) or {}
        if today.get("opening_time") and today.get("closing_time") and resources and avail:
            oh_h, oh_m = map(int, today["opening_time"].split(":"))
            ch_h, ch_m = map(int, today["closing_time"].split(":"))
            start_min, end_min = oh_h*60 + oh_m, ch_h*60 + ch_m
            # Zamknięcie po północy (np. 01:00) → grafik tylko do końca dnia (23:30)
            if end_min <= start_min:
                end_min = 24*60
            end_min = min(end_min, 24*60)
            now = datetime.now(WARSAW_TZ)
            if date_str == now.strftime("%Y-%m-%d"):
                start_min = max(start_min, now.hour*60 + (0 if now.minute < 30 else 30))
            for cidx in range(len(resources)):
                t = start_min
                while t < end_min:
                    h, mnt = divmod(t, 60)
                    out["slots"].append({"court_idx":cidx, "hour":h, "minute":mnt,
                                         "is_free": 1 if (h, mnt) in free.get(cidx, set()) else 0})
                    t += 30

        out["ok"] = True
        n_free = sum(1 for s in out["slots"] if s["is_free"])
        log.info("    ✓ %d kortów | %d slotów (%d wolnych) | ceny: %s PLN",
                 len(out["courts"]), len(out["slots"]), n_free,
                 sorted({p["price_pln"] for p in out["prices"]}) or "brak")
    except Exception as e:
        log.error("    Błąd API Playtomic (%s): %s", klub["slug"], e)
    return out


# ─── KLUBY.ORG logowanie ──────────────────────────────────────────────────────

async def login_klubyorg(page, email, password) -> bool:
    try:
        await page.goto("https://kluby.org/logowanie", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)

        # Zapisz screenshot do debugowania
        await page.screenshot(path="debug_login.png")

        # Próbuj wszystkich możliwych selektorów pola email/login
        email_filled = False
        for sel in [
            "input[type='email']", "input[name='email']", "input[name='login']",
            "input[name='username']", "input[name='user_login']",
            "input[placeholder*='mail']", "input[placeholder*='ogin']",
            "input[type='text']",
        ]:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.fill(email)
                email_filled = True
                log.info("Wypełniono email (selektor: %s)", sel)
                break

        if not email_filled:
            log.error("Nie znaleziono pola email – sprawdź debug_login.png w folderze padel")
            return False

        # Hasło
        pass_el = await page.query_selector("input[type='password']")
        if pass_el:
            await pass_el.fill(password)
        else:
            log.error("Nie znaleziono pola hasła")
            return False

        # Kliknij zaloguj
        for btn_sel in [
            "button[type='submit']", "input[type='submit']",
            "button:has-text('Zaloguj')", "button:has-text('Loguj')",
            ".btn-login", "[class*='submit']",
        ]:
            btn = await page.query_selector(btn_sel)
            if btn and await btn.is_visible():
                await btn.click()
                break
        else:
            await page.keyboard.press("Enter")

        await page.wait_for_timeout(3000)
        await page.screenshot(path="debug_after_login.png")

        body = await page.inner_text("body")
        url  = page.url

        if any(w in body.lower() for w in ["wyloguj","moje konto","profil","dashboard","grafik","panel","kornel","zaloguj jako"]):
            log.info("✓ Zalogowano na kluby.org"); return True
        if "logowanie" not in url.lower():
            log.info("✓ Zalogowano (redirect: %s)", url); return True

        log.error("✗ Logowanie nieudane – sprawdź debug_after_login.png")
        return False
    except Exception as e:
        log.error("Błąd logowania: %s", e); return False


# ─── KLUBY.ORG grafik ─────────────────────────────────────────────────────────

async def scrape_klubyorg(page, klub, date_str):
    url = f"https://kluby.org/{klub['slug']}/rezerwacje"
    out = {"name":"", "courts":[], "slots":[], "prices":[], "address":"",
           "hours_weekday":"", "hours_weekend":"", "phone":"",
           "amenities":[], "ok":False}
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2500)
        body = await page.inner_text("body")

        if "zaloguj" in body.lower() and "grafik" not in body.lower():
            log.warning("    Sesja wygasła"); return out

        # Klub bez rezerwacji online — nie ma danych o obłożeniu
        if "wyłączone rezerwacje online" in body.lower():
            log.warning("    Klub ma wyłączone rezerwacje ONLINE — pomijam sloty")
            out["ok"] = True  # zbierzemy tylko ceny i metadane
            # przejdź od razu do zbierania cen ze strony głównej
            try:
                await page.goto(f"https://kluby.org/{klub['slug']}",
                                wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(1500)
                await page.evaluate(
                    "Array.from(document.querySelectorAll('button,a,li,[role=tab],span'))"
                    ".find(el => el.textContent.trim().toUpperCase() === 'PADEL')?.click()"
                )
                await page.wait_for_timeout(1000)
                _PRICE_JS_EARLY = """() => {
                    const RE = /(\\d{2,3}(?:[,.]\\d{1,2})?)\\s*(?:zł|PLN)/gi;
                    const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','HEAD']);
                    const seen = new Set();
                    const NON_PADEL = ['sauna','bilard','billiard','dart','pickleball',
                                       'shuffleboard','tenis','squash','fitness','fit ',
                                       'mączka','basen','bowling','spinning','siatkówka',
                                       'golf','kręgiel','kort hard','kort zewn','hala hard',
                                       'sala fit','zajęcia fitness','karnet'];
                    const allTables = Array.from(document.querySelectorAll('table'));
                    // slice(2) pomija pierwsze 2 tabele nawigacyjne (zawierają nazwę klubu z "padel")
                    const priceTables = allTables.slice(2).filter(tbl => {
                        const firstRow = (tbl.querySelector('tr')?.innerText || '').toLowerCase();
                        if (/\\(202\\d-\\d{2}-\\d{2}\\)/.test(firstRow)) return false;
                        return true;
                    });
                    const explicitPadel = priceTables.filter(tbl =>
                        (tbl.querySelector('tr')?.innerText || '').toLowerCase().includes('padel'));
                    const withoutNonPadel = priceTables.filter(tbl =>
                        !NON_PADEL.some(kw => (tbl.querySelector('tr')?.innerText || '').toLowerCase().includes(kw)));
                    const targetTables = explicitPadel.length > 0 ? explicitPadel : withoutNonPadel;
                    targetTables.forEach(tbl => {
                        const walker = document.createTreeWalker(tbl, NodeFilter.SHOW_TEXT, null);
                        let node;
                        while ((node = walker.nextNode()) !== null) {
                            const el = node.parentElement;
                            if (!el || SKIP.has(el.tagName)) continue;
                            let m;
                            while ((m = RE.exec(node.textContent)) !== null) {
                                    const p = parseFloat(m[1].replace(',', '.'));
                                    if (p >= 60 && p <= 500 && Number.isInteger(p)) seen.add(p);
                                }
                                RE.lastIndex = 0;
                            }
                        });
                    return Array.from(seen).sort((a, b) => a - b);
                }"""
                pv = await page.evaluate(_PRICE_JS_EARLY)
                out["prices"] = [{"price_pln": p, "duration_min": 60,
                                   "description": "cena z kluby.org"}
                                  for p in pv]
                log.info("    ✓ (brak grafiku online) | ceny: %s PLN", pv or "brak")
            except Exception as e:
                log.warning("    Błąd cen dla wyłączonego klubu: %s", e)
            return out

        # Przełącz na PADEL przez JavaScript (omija problem z niewidocznym przyciskiem)
        await page.evaluate(
            "Array.from(document.querySelectorAll('button,a,li'))"
            ".find(el => el.textContent.trim().toUpperCase() === 'PADEL')?.click()"
        )
        await page.wait_for_timeout(1000)
        body = await page.inner_text("body")

        # Adres i kontakt
        m = re.search(r"ul\.[^\n]{5,60}|al\.[^\n]{5,60}", body)
        if m: out["address"] = m.group(0).strip()[:200]
        m = re.search(r"(\d{3}[\s-]?\d{3}[\s-]?\d{3})", body)
        if m: out["phone"] = m.group(1)

        # Godziny
        hm = re.findall(r"(Pn|Pt|So|Nd)[^0-9]{0,10}(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})", body)
        for day, hf, ht in hm:
            s = f"{hf}-{ht}"
            if day in ("Pn","Pt"): out["hours_weekday"] = s
            elif day in ("So","Nd"): out["hours_weekend"] = s

        # Udogodnienia
        am = {"gotówk":"Płatność gotówką","kart":"Płatność kartą","multisport":"Multisport",
              "parking bezpłat":"Free Parking","wi-fi":"WiFi","szatni":"Changing Room",
              "prysznic":"Showers","wynajem sprzętu":"Equipment Rental","kawiarni":"Cafeteria"}
        out["amenities"] = [v for k,v in am.items() if k in body.lower()]

        # Korty z nagłówków tabeli
        courts = []
        for sel in ["th:not(:first-child)","[class*='grafik'] th","[class*='schedule'] th"]:
            headers = await page.query_selector_all(sel)
            for h in headers:
                txt = (await h.inner_text()).strip()
                if not txt or txt in ("Godzina","Czas"): continue
                if any(w in txt.lower() for w in ["hala","padel","kort"]):
                    courts.append({
                        "name": txt[:80],
                        "surface_type": "outdoor" if any(w in txt.lower()
                            for w in ["outdoor","zewn","open"]) else "indoor",
                        "court_format": "single" if "singl" in txt.lower() else "double",
                    })
            if courts: break

        # Fallback z liczby kortów w tekście
        if not courts:
            m = re.search(r"(\d+)x\s*Padel", body)
            n = int(m.group(1)) if m else 1
            surface = "outdoor" if any(w in klub["name"].lower() for w in ["outdoor","bulwar"]) else "indoor"
            courts = [{"name":f"Kort {i+1}","surface_type":surface,"court_format":"double"}
                      for i in range(n)]
        # Override nawierzchni z konfiguracji klubu (np. "outdoor" dla aerosquash)
        if "surface_type" in klub:
            for c in courts:
                c["surface_type"] = klub["surface_type"]
        out["courts"] = courts

        # Sloty z grafiku
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        dow = dt.strftime("%A")
        is_weekend = 1 if dt.weekday() >= 5 else 0
        slots = []

        # Strategia: wiersze tabeli
        rows = await page.query_selector_all("tr")
        for row in rows:
            row_txt = (await row.inner_text()).strip()
            time_m  = re.search(r"^(\d{1,2}):(\d{2})", row_txt)
            if not time_m: continue
            hour, minute = int(time_m.group(1)), int(time_m.group(2))
            cells = await row.query_selector_all("td")
            for col_idx, cell in enumerate(cells[1:], 0):
                ct  = (await cell.inner_text()).strip()
                cls = await cell.get_attribute("class") or ""
                # Pomiń szare komórki — poza godzinami pracy lub "Klub zamknięty"
                if "bg-gray" in cls:
                    continue
                # Zajęty: tekst "Zarezerwowane" LUB klasa rezerwacja_*/kolor (zajęcia grupowe)
                is_free = 0 if (
                    "zarezerwow" in ct.lower()
                    or "booked" in cls.lower()
                    or "rezerwacja" in cls.lower()
                    or "kolor" in cls.lower()   # zajęcia grupowe (kolor-11 itp.)
                ) else 1
                bk = re.search(r"(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})", ct)
                slots.append({
                    "court_idx": col_idx, "hour": hour, "minute": minute,
                    "is_free": is_free,
                    "booking_start": bk.group(1) if bk else None,
                    "booking_end":   bk.group(2) if bk else None,
                })

        out["slots"] = slots

        # Ceny: ze strony głównej klubu, TYLKO z tabel sekcji padlowej.
        # Strategia: (1) szukaj tabel gdzie nagłówek zawiera "padel" → używaj tylko te.
        # (2) jeśli brak jawnego "padel" → wyklucz tabele z nagłówkiem innego sportu/usługi.
        # To eliminuje: saunę z Mana, pickleball/shuffleboard z Toro, tenis z Sporteum, itp.
        _PRICE_JS = """() => {
            const RE = /(\\d{2,3}(?:[,.]\\d{1,2})?)\\s*(?:zł|PLN)/gi;
            const seen = new Set();
            const NON_PADEL = ['sauna','bilard','billiard','dart','pickleball',
                               'shuffleboard','tenis','squash','fitness','fit ',
                               'mączka','basen','bowling','spinning','siatkówka',
                               'golf','kręgiel','kort hard','kort zewn','hala hard',
                               'sala fit','zajęcia fitness','karnet'];

            const allTables = Array.from(document.querySelectorAll('table'));
            // Pomijamy pierwsze 2 tabele (nawigacja/info)
            const priceTables = allTables.slice(2).filter(tbl => {
                const firstRow = (tbl.querySelector('tr')?.innerText || '').toLowerCase();
                // Jeśli wiersz wygląda jak ogłoszenie/event (np. zawiera rok w nawiasie) — pomiń
                if (/\\(202\\d-\\d{2}-\\d{2}\\)/.test(firstRow)) return false;
                return true;
            });

            // Strategia 1: tabel z jawnym "padel" w nagłówku
            const explicitPadel = priceTables.filter(tbl => {
                const r1 = (tbl.querySelector('tr')?.innerText || '').toLowerCase();
                return r1.includes('padel');
            });

            // Strategia 2: wszystkie tabele bez jawnie nie-padlowych nagłówków
            const withoutNonPadel = priceTables.filter(tbl => {
                const r1 = (tbl.querySelector('tr')?.innerText || '').toLowerCase();
                return !NON_PADEL.some(kw => r1.includes(kw));
            });

            const targetTables = explicitPadel.length > 0 ? explicitPadel : withoutNonPadel;

            const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','HEAD']);
            targetTables.forEach(tbl => {
                const walker = document.createTreeWalker(tbl, NodeFilter.SHOW_TEXT, null);
                let node;
                while ((node = walker.nextNode()) !== null) {
                    const el = node.parentElement;
                    if (!el || SKIP.has(el.tagName)) continue;
                    let m;
                    while ((m = RE.exec(node.textContent)) !== null) {
                        const p = parseFloat(m[1].replace(',', '.'));
                        if (p >= 60 && p <= 500 && Number.isInteger(p)) seen.add(p);
                    }
                    RE.lastIndex = 0;
                }
            });
            return Array.from(seen).sort((a, b) => a - b);
        }"""
        price_vals = []
        try:
            await page.goto(f"https://kluby.org/{klub['slug']}",
                            wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(1500)
            price_vals = await page.evaluate(_PRICE_JS)
        except Exception as e:
            log.warning("    Błąd zbierania cen: %s", e)

        out["prices"] = [{"price_pln": p, "duration_min": 60,
                           "description": "cena z kluby.org"}
                          for p in price_vals]
        out["ok"] = True

        n_free   = sum(1 for s in slots if s["is_free"])
        n_booked = len(slots) - n_free
        log.info("    ✓ %d kortów | %d slotów (%d zajętych, %d wolnych) | ceny: %s PLN",
                 len(courts), len(slots), n_booked, n_free,
                 price_vals if price_vals else "brak")
    except Exception as e:
        log.error("    Błąd: %s", e)
    return out


# ─── Eksport CSV ──────────────────────────────────────────────────────────────

def export_csv(conn, date_str):
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    CSV_DIR.mkdir(exist_ok=True)
    queries = {
        "1_kluby": """
            SELECT id,name,source,dzielnica,address,lat,lng,
                   padel_courts_total,hours_weekday,hours_weekend,
                   amenities,website,phone FROM clubs ORDER BY dzielnica,name""",
        "2_korty_padlowe": """
            SELECT c.name AS klub, c.dzielnica,
                   r.court_name, r.surface_type, r.court_format, r.court_style
            FROM courts r JOIN clubs c ON c.id=r.club_id
            ORDER BY c.dzielnica, c.name, r.court_name""",
        # Unikalne ceny per godzinę — duplikaty z 60/90/120 min są złączone w jedną wartość/h.
        # Brak kolumny cena_oryginalna (surowej) — tylko znormalizowana per godzinę.
        "3_ceny_wg_pory_dnia": """
            SELECT c.name AS klub, c.dzielnica, c.source,
                   ROUND(p.price_pln * 60.0 / COALESCE(p.duration_min, 60), 0)
                       AS cena_za_godzine,
                   p.description
            FROM prices p JOIN clubs c ON c.id=p.club_id
            WHERE p.price_type IN ('court','academy')
            GROUP BY c.id, ROUND(p.price_pln * 60.0 / COALESCE(p.duration_min, 60), 0)
            ORDER BY c.name, cena_za_godzine""",
        "4_oblozenie_per_kort_godzina": f"""
            SELECT c.name AS klub, c.dzielnica, s.source,
                   s.court_name AS kort, r.surface_type AS nawierzchnia,
                   s.slot_date, s.day_of_week,
                   CASE s.is_weekend WHEN 1 THEN 'weekend' ELSE 'tydzien' END AS typ_dnia,
                   s.slot_hour AS godzina, s.slot_minute AS minuta,
                   s.pora_dnia, s.is_free AS wolny,
                   s.booking_start, s.booking_end
            FROM slots s
            JOIN clubs c ON c.id=s.club_id
            LEFT JOIN courts r ON r.id=s.court_id
            WHERE s.slot_date='{date_str}'
            ORDER BY c.name, s.court_name, s.slot_hour, s.slot_minute""",
        "5_oblozenie_per_pora_dnia": f"""
            SELECT c.name AS klub, c.dzielnica, s.source,
                   s.court_name AS kort, r.surface_type AS nawierzchnia,
                   s.pora_dnia,
                   CASE s.is_weekend WHEN 1 THEN 'weekend' ELSE 'tydzien' END AS typ_dnia,
                   COUNT(*) AS slotow,
                   SUM(CASE WHEN s.is_free=0 THEN 1 ELSE 0 END) AS zajete,
                   SUM(s.is_free) AS wolne,
                   ROUND(100.0*SUM(CASE WHEN s.is_free=0 THEN 1 ELSE 0 END)/COUNT(*),1) AS oblozenie_pct
            FROM slots s
            JOIN clubs c ON c.id=s.club_id
            LEFT JOIN courts r ON r.id=s.court_id
            WHERE s.slot_date='{date_str}'
            GROUP BY c.name, s.court_name, s.pora_dnia, r.surface_type, s.is_weekend
            ORDER BY c.name, s.court_name,
                CASE s.pora_dnia WHEN 'rano (6-10)' THEN 1
                    WHEN 'poludnie (10-14)' THEN 2 WHEN 'popoludnie (14-18)' THEN 3
                    WHEN 'wieczor (18-22)' THEN 4 ELSE 5 END""",
        # Jeden wiersz na (klub, nawierzchnia) — kluby z indoor+outdoor = 2 wiersze.
        # Obłożenie liczone tylko dla kortów danej nawierzchni (JOIN courts→slots).
        # Ceny na poziomie klubu (te same dla obu nawierzchni gdy brak rozróżnienia).
        "6_porownanie_klubow": f"""
            SELECT c.name AS klub, c.dzielnica, c.source,
                   r.surface_type AS nawierzchnia,
                   COUNT(DISTINCT r.id) AS korty_padlowe,
                   pr.cena_min, pr.cena_max, pr.cena_srednia,
                   c.hours_weekday AS godz_tydzien, c.hours_weekend AS godz_weekend,
                   COALESCE(sl.slotow_dzisiaj,0) AS slotow_dzisiaj,
                   ROUND(100.0*sl.zajete/NULLIF(sl.slotow_dzisiaj,0),1) AS oblozenie_pct,
                   c.address,
                   COALESCE(mx.slotow_max,0) AS slotow_max
            FROM clubs c
            JOIN courts r ON r.club_id=c.id
            LEFT JOIN (SELECT club_id,
                              MIN(ROUND(price_pln*60.0/COALESCE(duration_min,60),0)) AS cena_min,
                              MAX(ROUND(price_pln*60.0/COALESCE(duration_min,60),0)) AS cena_max,
                              ROUND(AVG(price_pln*60.0/COALESCE(duration_min,60)),0) AS cena_srednia
                       FROM prices WHERE price_type='court'
                       GROUP BY club_id) pr ON pr.club_id=c.id
            LEFT JOIN (SELECT s.club_id, r2.surface_type,
                              COUNT(*) AS slotow_dzisiaj,
                              SUM(CASE WHEN s.is_free=0 THEN 1 ELSE 0 END) AS zajete
                       FROM slots s
                       JOIN courts r2 ON r2.id=s.court_id
                       WHERE s.slot_date='{date_str}'
                       GROUP BY s.club_id, r2.surface_type) sl
                    ON sl.club_id=c.id AND sl.surface_type=r.surface_type
            LEFT JOIN (SELECT club_id, MAX(total) AS slotow_max
                       FROM (SELECT club_id, slot_date, COUNT(*) AS total
                             FROM slots GROUP BY club_id, slot_date)
                       GROUP BY club_id) mx
                    ON mx.club_id=c.id
            GROUP BY c.id, r.surface_type
            HAVING pr.cena_min IS NOT NULL OR sl.slotow_dzisiaj > 0
            ORDER BY c.dzielnica, c.name, r.surface_type""",
        # Liczba kortów z tabeli courts; ceny per nawierzchnia z agregatu cen
        # klubów (jeden wiersz na klub) — bez fan-out z JOIN prices.
        "7_indoor_vs_outdoor": """
            SELECT r.surface_type AS nawierzchnia,
                   COUNT(DISTINCT r.club_id) AS klubow,
                   COUNT(r.id) AS kortow,
                   ROUND(AVG(pr.cena_srednia),0) AS srednia_cena_pln,
                   MIN(pr.cena_min) AS min_cena, MAX(pr.cena_max) AS max_cena
            FROM courts r
            LEFT JOIN (SELECT club_id,
                              MIN(ROUND(price_pln*60.0/COALESCE(duration_min,60),0)) AS cena_min,
                              MAX(ROUND(price_pln*60.0/COALESCE(duration_min,60),0)) AS cena_max,
                              AVG(price_pln*60.0/COALESCE(duration_min,60)) AS cena_srednia
                       FROM prices WHERE price_type='court'
                       GROUP BY club_id) pr ON pr.club_id=r.club_id
            GROUP BY r.surface_type""",
    }
    exported = []
    for name, query in queries.items():
        path = CSV_DIR / f"{name}_{ts}.csv"
        try:
            rows = conn.execute(query).fetchall()
            if not rows: continue
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow([d[0] for d in conn.execute(query).description])
                writer.writerows(rows)
            exported.append((path.name, len(rows)))
        except Exception as e:
            log.warning("Błąd eksportu %s: %s", name, e)

    # Pliki "latest_*" — nadpisywane przy każdym uruchomieniu (dla dashboardu HTML)
    for name, query in queries.items():
        # np. "1_kluby" → "latest_kluby.csv", "6_porownanie_klubow" → "latest_porownanie_klubow.csv"
        suffix = name.split("_", 1)[1]
        path   = CSV_DIR / f"latest_{suffix}.csv"
        try:
            rows = conn.execute(query).fetchall()
            if not rows: continue
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow([d[0] for d in conn.execute(query).description])
                writer.writerows(rows)
        except Exception as e:
            log.warning("Błąd eksportu latest_%s: %s", suffix, e)

    # Historia godzinowa — per klub × godzina × dzień (dla dashboardu per-klub).
    # Regenerowana w całości przy każdym uruchomieniu z pełnej historii bazy.
    history_detail_path = CSV_DIR / "history_detail.csv"
    try:
        rows_hd = conn.execute("""
            SELECT s.slot_date AS data,
                   c.name     AS klub,
                   c.source,
                   s.slot_hour AS godzina,
                   COUNT(DISTINCT s.court_id) AS n_kortow,
                   COUNT(*)    AS n_slotow,
                   SUM(CASE WHEN s.is_free=0 THEN 1 ELSE 0 END) AS n_zajetych,
                   ROUND(100.0*SUM(CASE WHEN s.is_free=0 THEN 1 ELSE 0 END)
                         /NULLIF(COUNT(*),0),0) AS oblozenie_pct
            FROM slots s
            JOIN clubs c ON c.id=s.club_id
            GROUP BY s.slot_date, c.name, s.slot_hour
            ORDER BY s.slot_date, c.name, s.slot_hour
        """).fetchall()
        with open(history_detail_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["data","klub","source","godzina",
                             "n_kortow","n_slotow","n_zajetych","oblozenie_pct"])
            writer.writerows(rows_hd)
    except Exception as e:
        log.warning("Błąd eksportu history_detail.csv: %s", e)

    # Historia dzienna — jedna linia dopisywana każdego dnia (wykres trendów)
    history_path   = CSV_DIR / "history.csv"
    history_fields = ["date", "n_clubs", "n_slots", "n_booked", "occupancy_pct",
                      "price_min", "price_max", "price_avg"]
    try:
        n_slots  = conn.execute("SELECT COUNT(*) FROM slots WHERE slot_date=?", (date_str,)).fetchone()[0]
        n_booked = conn.execute("SELECT COUNT(*) FROM slots WHERE slot_date=? AND is_free=0", (date_str,)).fetchone()[0]
        prices   = conn.execute("""SELECT MIN(ROUND(price_pln*60.0/COALESCE(duration_min,60),0)),
                                          MAX(ROUND(price_pln*60.0/COALESCE(duration_min,60),0)),
                                          AVG(price_pln*60.0/COALESCE(duration_min,60))
                                   FROM prices WHERE price_type='court'""").fetchone()
        n_clubs  = conn.execute("SELECT COUNT(*) FROM clubs").fetchone()[0]
        obl      = round(n_booked / n_slots * 100, 1) if n_slots else 0
        history_row = {
            "date": date_str, "n_clubs": n_clubs, "n_slots": n_slots,
            "n_booked": n_booked, "occupancy_pct": obl,
            "price_min": prices[0], "price_max": prices[1],
            "price_avg": round(prices[2], 0) if prices[2] else None,
        }
        history_exists = history_path.exists() and history_path.stat().st_size > 0
        with open(history_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=history_fields)
            if not history_exists:
                writer.writeheader()
            writer.writerow(history_row)
    except Exception as e:
        log.warning("Błąd eksportu history.csv: %s", e)

    return exported



# ─── Dane logowania z Keychain ────────────────────────────────────────────────

KEYCHAIN_SERVICE = "kluby_org"
KEYCHAIN_ACCOUNT = "kornel.kim@gmail.com"

def get_credentials():
    """
    Pobiera email i hasło z macOS Keychain.
    Jeśli nie ma zapisanych danych, pyta interaktywnie i oferuje zapis.
    """
    import subprocess

    # GitHub Actions / CI — zmienne środowiskowe mają priorytet
    env_email = os.getenv("KLUBYORG_EMAIL")
    env_pass  = os.getenv("KLUBYORG_PASSWORD")
    if env_email and env_pass:
        log.info("✓ Poświadczenia z zmiennych środowiskowych (tryb CI)")
        return env_email, env_pass

    # Próba pobrania z Keychain
    try:
        result = subprocess.run(
            ["security", "find-generic-password",
             "-a", KEYCHAIN_ACCOUNT,
             "-s", KEYCHAIN_SERVICE,
             "-w"],  # -w = tylko hasło
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            password = result.stdout.strip()
            if password:
                log.info("✓ Hasło pobrane z macOS Keychain")
                return KEYCHAIN_ACCOUNT, password
    except Exception as e:
        log.debug("Keychain niedostępny: %s", e)

    # Fallback: zapytaj ręcznie
    print("\n⚠️  Nie znaleziono hasła w Keychain.")
    print("   Aby zapisać hasło na stałe, uruchom:")
    print(f"   security add-generic-password -a \"{KEYCHAIN_ACCOUNT}\" -s \"{KEYCHAIN_SERVICE}\" -w\n")
    email    = input(f"Login [{KEYCHAIN_ACCOUNT}]: ").strip() or KEYCHAIN_ACCOUNT
    password = getpass.getpass("Hasło kluby.org: ")

    # Zaproponuj zapis do Keychain
    save = input("Zapisać hasło w Keychain? (t/n): ").strip().lower()
    if save == "t":
        try:
            subprocess.run(
                ["security", "add-generic-password",
                 "-a", email, "-s", KEYCHAIN_SERVICE,
                 "-w", password, "-U"],  # -U = update jeśli istnieje
                check=True, timeout=5
            )
            print("✓ Hasło zapisane w Keychain – następnym razem bez pytania!")
        except Exception as e:
            print(f"⚠️  Nie udało się zapisać: {e}")

    return email, password


# ─── Główna pętla ─────────────────────────────────────────────────────────────

async def run():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("\n❌ Uruchom najpierw:  python3 setup.py\n"); return

    print("\n" + "═"*60)
    print("  Skraper kortów padlowych Warszawa – Master")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("═"*60)
    print(f"\n  Kluby Playtomic:  {len(PLAYTOMIC)}")
    print(f"  Kluby Kluby.org:  {len(KLUBYORG)}")
    print(f"  Łącznie:          {len(PLAYTOMIC)+len(KLUBYORG)} klubów\n")
    # ── Pobierz dane logowania z Keychain lub ręcznie ────────────────────────
    email, password = get_credentials()

    conn  = init_db()
    _now_warsaw = datetime.now(WARSAW_TZ)
    today = _now_warsaw.strftime("%Y-%m-%d")
    now   = _now_warsaw.isoformat()

    # Wyczyść stare kluby usunięte z list (squash, nieistniejące, zmienione slugi)
    for stale_id in ("pt_warsaw-padel-club-squash", "pt_rakiety-squash-padel-outdoor",
                     "pt_we-are-padel-warsaw",
                     "ko_sinus", "ko_happy-padel", "ko_decathlon", "ko_aerosquash"):
        conn.execute("DELETE FROM clubs  WHERE id=?",       (stale_id,))
        conn.execute("DELETE FROM courts WHERE club_id=?",  (stale_id,))
        conn.execute("DELETE FROM slots  WHERE club_id=?",  (stale_id,))
        conn.execute("DELETE FROM prices WHERE club_id=?",  (stale_id,))
    conn.commit()

    # ── 1. PLAYTOMIC (publiczne API, bez przeglądarki) ────────────────────────
    print("\n── Playtomic (" + str(len(PLAYTOMIC)) + " klubów) " + "─"*35)
    for klub in PLAYTOMIC:
        log.info("  [Playtomic API] → %s", klub["name"])
        data = build_playtomic_data(klub, today)
        if not data["ok"]: continue
        club_id = f"pt_{klub['slug']}"
        upsert_club(conn, {
            "id":club_id, "name":data["name"] or klub["name"],
            "source":"playtomic", "dzielnica":klub["dzielnica"],
            "address":data["address"], "lat":data["lat"], "lng":data["lng"],
            "padel_courts_total":len(data["courts"]) or None,
            "hours_weekday":data["hours_weekday"], "hours_weekend":data["hours_weekend"],
            "amenities":json.dumps(data["amenities"], ensure_ascii=False),
            "website":f"https://playtomic.com/clubs/{klub['slug']}",
            "phone":None, "scraped_at":now,
        })
        save_courts(conn, club_id, data["courts"])
        save_slots(conn, club_id, today, data["slots"], data["courts"], "playtomic")
        save_prices(conn, club_id, data["prices"])

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
        ctx = await browser.new_context(
            locale="pl-PL",
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        page = await ctx.new_page()

        # ── 2. KLUBY.ORG – logowanie ──────────────────────────────────────────
        print("\n── Kluby.org (" + str(len(KLUBYORG)) + " klubów) " + "─"*35)
        logged_in = await login_klubyorg(page, email, password)
        if not logged_in:
            print("\n❌ Logowanie nieudane – pomijam kluby.org\n")
        else:
            for klub in KLUBYORG:
                log.info("  [Kluby.org] → %s", klub["name"])
                data = await scrape_klubyorg(page, klub, today)
                club_id = f"ko_{klub['slug']}"
                all_courts = data["courts"] + klub.get("extra_courts", [])
                upsert_club(conn, {
                    "id":club_id, "name":klub["name"], "source":"kluby_org",
                    "dzielnica":klub["dzielnica"], "address":data["address"],
                    "lat":None, "lng":None,
                    "padel_courts_total":len(all_courts) or None,
                    "hours_weekday":data["hours_weekday"],
                    "hours_weekend":data["hours_weekend"],
                    "amenities":json.dumps(data["amenities"], ensure_ascii=False),
                    "website":f"https://kluby.org/{klub['slug']}",
                    "phone":data["phone"], "scraped_at":now,
                })
                save_courts(conn, club_id, all_courts)
                save_slots(conn, club_id, today, data["slots"], all_courts, "kluby_org")
                save_prices(conn, club_id, data["prices"])
                await page.wait_for_timeout(1500)

        await browser.close()

    # ── Eksport i podsumowanie ────────────────────────────────────────────────
    exported = export_csv(conn, today)

    n_clubs   = conn.execute("SELECT COUNT(*) FROM clubs").fetchone()[0]
    n_courts  = conn.execute("SELECT COUNT(*) FROM courts").fetchone()[0]
    n_indoor  = conn.execute("SELECT COUNT(*) FROM courts WHERE surface_type='indoor'").fetchone()[0]
    n_outdoor = conn.execute("SELECT COUNT(*) FROM courts WHERE surface_type='outdoor'").fetchone()[0]
    n_slots   = conn.execute("SELECT COUNT(*) FROM slots WHERE slot_date=?", (today,)).fetchone()[0]
    n_booked  = conn.execute("SELECT COUNT(*) FROM slots WHERE slot_date=? AND is_free=0", (today,)).fetchone()[0]
    prices    = conn.execute("SELECT MIN(price_pln),MAX(price_pln),AVG(price_pln) FROM prices WHERE price_type='court'").fetchone()
    obl       = round(n_booked/n_slots*100,1) if n_slots else 0

    print("\n" + "═"*60)
    print("  WYNIKI")
    print("═"*60)
    print(f"  📍 Klubów w bazie:     {n_clubs}  ({len(PLAYTOMIC)} Playtomic + {len(KLUBYORG)} Kluby.org)")
    print(f"  🎾 Kortów padlowych:   {n_courts}  (indoor: {n_indoor} | outdoor: {n_outdoor})")
    print(f"  📊 Slotów dziś:        {n_slots}  (zajętych: {n_booked} = {obl}%)")
    if prices[0]:
        print(f"  💰 Ceny kortów:        {prices[0]:.0f}–{prices[1]:.0f} zł  (śr. {prices[2]:.0f} zł)")
    print(f"\n  📁 {len(exported)} plików CSV → {CSV_DIR.resolve()}")
    for fname, rows in exported:
        print(f"     • {fname}  ({rows} wierszy)")
    print("═"*60 + "\n")
    conn.close()

if __name__ == "__main__":
    asyncio.run(run())
