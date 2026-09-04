"""
ingest_historical.py — Import data 2023, 2024, 2025 ke tabel donations

Cara pakai (di VPS, sekali jalan):
  cd /var/www/fundraising
  ./venv/bin/python ingest/ingest_historical.py

Fitur:
- Header detection otomatis (cari posisi kolom "Bulan", "Nominal", dst dari baris pertama)
- CS name dari nama tab (bukan kolom)
- Deduplicated via row_hash — aman dijalankan ulang
- Skip tab non-CS (Rekap Bulan, Form Responses, Kemitraan, dll)
- source_year tersimpan per baris untuk filter multi-tahun
"""
import asyncio, hashlib, re, os, sys, urllib.request, urllib.parse, json, logging
from datetime import datetime
from pathlib import Path

# Supaya bisa import models dari parent directory
sys.path.insert(0, str(Path(__file__).parent.parent))

import aiosqlite
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
log = logging.getLogger("ingest")

DB_PATH   = Path(__file__).parent.parent / "fundraising.db"
API_KEY   = os.getenv("GOOGLE_API_KEY", "")

# ── Spreadsheet per tahun ─────────────────────────────────────────────────────
SOURCES = {
    2025: "1LyljKwyyegMdLQ3b33pEwDiJygf5jE2MRlEvSy5G3YU",
    2024: "1-BdA2obr1zLyo0z8c1xR6F79u1MWwQhSKZ4IjHQnbB8",
    2023: "1UnVD-IYW_aSdlHn6K1iNpmzexgUZylZg6mikSthpgXI",
}

# ── Tab yang harus di-skip ────────────────────────────────────────────────────
SKIP_PATTERNS = [
    r'^form responses',
    r'^rekap\s+(januar|februar|maret|april|mei|juni|juli|agust|septem|oktob|novem|desem)',
    r'^telat\s+konfirm',
    r'^donasi\s+(lain|riba)',
    r'^kemitraan$',
    r'^nama\s+program$',
    r'^koreksi',
    r'^tidak\s+terpakai',
    r'\btidak terpakai\b',
]

def should_skip(tab_name: str) -> bool:
    t = tab_name.strip().lower()
    return any(re.search(p, t) for p in SKIP_PATTERNS)

# ── Helpers ───────────────────────────────────────────────────────────────────
_CLEAN_RE = re.compile(r"[\s\-\.\(\)]")
_DATE_FMTS = ["%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y",
               "%m/%d/%Y %H:%M:%S", "%-m/%-d/%Y"]

def normalize_phone(raw: str) -> str:
    if not raw:
        return ""
    s = _CLEAN_RE.sub("", str(raw).strip())
    if s.startswith("+62"):
        s = "0" + s[3:]
    elif s.startswith("62") and len(s) > 10:
        s = "0" + s[2:]
    elif s.startswith("8") and not s.startswith("08"):
        s = "0" + s
    return s

def parse_date(raw: str) -> str:
    if not raw:
        return ""
    raw = str(raw).strip()
    # Kalau ada spasi (timestamp), ambil bagian tanggal saja
    raw = raw.split(" ")[0] if " " in raw else raw
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""

def row_hash(tanggal: str, nominal: int, phone: str, cs: str, name: str) -> str:
    key = f"{tanggal}|{nominal}|{phone}|{cs}|{name}"
    return hashlib.sha1(key.encode()).hexdigest()

# ── Deteksi kolom dari header ─────────────────────────────────────────────────
def detect_cols(header: list[str]) -> dict:
    """
    Temukan indeks kolom kunci dari baris header.
    Fallback ke posisi default jika tidak ketemu.
    """
    h = [c.lower().strip() for c in header]

    def find(*keywords) -> int:
        for kw in keywords:
            for i, col in enumerate(h):
                if kw in col:
                    return i
        return -1

    return {
        "tanggal":  find("tanggal transfer", "tanggal") or 1,
        "nama":     find("nama donatur", "nama") or 2,
        "hp":       find("nomor hp", "no hp", "phone") or 3,
        "ig":       find("username ig", "instagram", "ig") or 4,
        "nominal":  find("nominal transfer", "nominal") or 5,
        "program":  find("kode program", "program") or 6,
        "asal":     find("asal donasi", "asal") or 7,
        "keterangan": find("keterangan") or 10,
        "bulan":    find("bulan") if find("bulan") != -1 else 11,
    }

# ── Fetch via Sheets API ──────────────────────────────────────────────────────
def sheets_get(url: str) -> dict:
    try:
        r = urllib.request.urlopen(url, timeout=20)
        return json.loads(r.read())
    except Exception as e:
        return {"__error__": str(e)}

def get_all_tabs(sid: str) -> list[dict]:
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sid}?fields=sheets.properties&key={API_KEY}"
    d = sheets_get(url)
    if "__error__" in d:
        log.error(f"Gagal ambil tabs dari {sid}: {d['__error__']}")
        return []
    return [s["properties"] for s in d.get("sheets", [])]

def get_tab_data(sid: str, tab_name: str) -> list[list[str]]:
    enc = urllib.parse.quote(tab_name)
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{sid}"
           f"/values/{enc}!A1:P?key={API_KEY}")
    d = sheets_get(url)
    if "__error__" in d:
        log.warning(f"  Tab '{tab_name}' error: {d['__error__']}")
        return []
    return d.get("values", [])

# ── Load exclusions dari DB ───────────────────────────────────────────────────
async def load_exclusions() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT donor_name, tanggal, nominal FROM institutional_exclusion") as cur:
            return [dict(r) for r in await cur.fetchall()]

def is_institutional(name: str, tanggal: str, nominal: int, exclusions: list[dict]) -> bool:
    name_lower = (name or "").lower()
    for ex in exclusions:
        if (ex["nominal"] == nominal
                and ex["tanggal"] == tanggal
                and ex["donor_name"].lower() in name_lower):
            return True
    return False

# ── Ensure source_year column ─────────────────────────────────────────────────
async def ensure_schema():
    async with aiosqlite.connect(DB_PATH) as db:
        # Tambah source_year kalau belum ada
        try:
            await db.execute("ALTER TABLE donations ADD COLUMN source_year INTEGER")
            await db.commit()
            log.info("Kolom source_year ditambahkan ke tabel donations")
        except Exception:
            pass  # sudah ada

# ── Ingest satu tab ───────────────────────────────────────────────────────────
async def ingest_tab(sid: str, tab_name: str, year: int, exclusions: list[dict]) -> tuple[int, int]:
    """Return (rows_inserted, rows_skipped)."""
    rows = get_tab_data(sid, tab_name)
    if not rows:
        return 0, 0

    # Header di baris pertama
    header = rows[0]
    cols   = detect_cols(header)
    data_rows = rows[1:]

    cs_name = tab_name.strip()  # nama CS dari nama tab

    inserted = skipped = 0

    async with aiosqlite.connect(DB_PATH) as db:
        for r in data_rows:
            def col(i, default=""):
                return r[i].strip() if i < len(r) and str(r[i]).strip() else default

            tanggal = parse_date(col(cols["tanggal"]))
            if not tanggal:
                skipped += 1
                continue

            # Validasi tahun — tidak boleh masuk data yang salah tahun ke source_year
            try:
                row_year = int(tanggal[:4])
                if row_year < 2020 or row_year > 2030:
                    skipped += 1
                    continue
            except Exception:
                skipped += 1
                continue

            try:
                nominal = int(float(str(col(cols["nominal"], "0"))
                               .replace(".", "").replace(",", "").replace(" ", "")))
            except (ValueError, TypeError):
                nominal = 0

            if nominal <= 0:
                skipped += 1
                continue

            name  = col(cols["nama"])
            phone = normalize_phone(col(cols["hp"]))
            rhash = row_hash(tanggal, nominal, phone, cs_name, name)
            is_inst = 1 if is_institutional(name, tanggal, nominal, exclusions) else 0

            await db.execute("""
                INSERT INTO donations
                    (row_hash, tanggal, donor_name, donor_phone, ig_username,
                     nominal, kode_program, asal_donasi, cs, bulan,
                     keterangan, is_institusional, source_year)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(row_hash) DO UPDATE SET
                    is_institusional = excluded.is_institusional,
                    source_year      = excluded.source_year
            """, (
                rhash, tanggal, name, phone,
                col(cols["ig"]),
                nominal,
                col(cols["program"]),
                col(cols["asal"]),
                cs_name,
                col(cols["bulan"]),
                col(cols["keterangan"]),
                is_inst,
                year,
            ))
            inserted += 1

        await db.commit()

    return inserted, skipped

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    if not API_KEY:
        print("ERROR: GOOGLE_API_KEY tidak ada di .env")
        return

    await ensure_schema()
    exclusions = await load_exclusions()
    log.info(f"Loaded {len(exclusions)} exclusion rules")

    grand_total = 0

    for year in [2025, 2024, 2023]:
        sid = SOURCES[year]
        log.info(f"\n{'='*60}")
        log.info(f"TAHUN {year} — {sid}")
        log.info('='*60)

        tabs = get_all_tabs(sid)
        if not tabs:
            log.warning(f"  Tidak bisa ambil tab list untuk {year}")
            continue

        log.info(f"  Total tab: {len(tabs)}")

        year_total = year_skipped = 0
        for tab in tabs:
            tab_name = tab["title"]

            if should_skip(tab_name):
                log.info(f"  SKIP: {tab_name}")
                continue

            ins, skp = await ingest_tab(sid, tab_name, year, exclusions)
            if ins > 0 or skp > 0:
                log.info(f"  [{tab_name}]: {ins} inserted, {skp} skipped")
            else:
                log.info(f"  [{tab_name}]: kosong")
            year_total   += ins
            year_skipped += skp

        log.info(f"\n  Tahun {year}: {year_total} baris masuk, {year_skipped} dilewati")
        grand_total += year_total

    log.info(f"\n{'='*60}")
    log.info(f"SELESAI. Total {grand_total} baris imported.")
    log.info('='*60)

    # Summary per tahun dari DB
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT source_year, COUNT(*) AS cnt, SUM(nominal) AS total
            FROM donations
            GROUP BY source_year
            ORDER BY source_year
        """) as cur:
            rows = await cur.fetchall()
            print("\n--- Ringkasan per tahun di DB ---")
            for r in rows:
                yr  = r["source_year"] or "2026(live)"
                cnt = r["cnt"]
                tot = r["total"] or 0
                print(f"  {yr}: {cnt} donasi, total Rp {tot:,.0f}".replace(",", "."))

if __name__ == "__main__":
    asyncio.run(main())
