"""
sheets.py — Sync dari SEMUA sumber aktif di tabel sheet_sources
"""
import os, re, hashlib, logging, aiosqlite, httpx
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("sheets")
DB_PATH = Path(__file__).parent / "fundraising.db"
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")

# ── Normalisasi nomor HP ──────────────────────────────────────────────────────
_CLEAN_RE = re.compile(r"[\s\-\.\(\)]")

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

# ── Parse tanggal ─────────────────────────────────────────────────────────────
_DATE_FMTS = ["%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y"]

def parse_date(raw: str) -> str:
    if not raw:
        return ""
    raw = str(raw).strip()
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""

# ── Row hash ──────────────────────────────────────────────────────────────────
def row_hash(tanggal: str, nominal: int, phone: str, cs: str, name: str) -> str:
    key = f"{tanggal}|{nominal}|{phone}|{cs}|{name}"
    return hashlib.sha1(key.encode()).hexdigest()

# ── Cek apakah institusional (dari DB) ───────────────────────────────────────
async def _load_exclusions(db) -> list[dict]:
    async with db.execute("SELECT donor_name, tanggal, nominal FROM institutional_exclusion") as cur:
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

def _is_institutional(name: str, tanggal: str, nominal: int, exclusions: list[dict]) -> bool:
    name_lower = (name or "").lower()
    for ex in exclusions:
        if (ex["nominal"] == nominal
                and ex["tanggal"] == tanggal
                and ex["donor_name"].lower() in name_lower):
            return True
    return False

# ── Sync satu sumber ──────────────────────────────────────────────────────────
async def _sync_one(source: dict, api_key: str) -> dict:
    sid        = source["spreadsheet_id"]
    sheet_name = source["sheet_name"]
    src_id     = source["id"]

    import urllib.parse
    sheet_enc = urllib.parse.quote(sheet_name)
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{sid}"
        f"/values/{sheet_enc}!A2:N?key={api_key}"
    )

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

    rows = data.get("values", [])
    log.info(f"[Sync] Source #{src_id} '{source['label']}': {len(rows)} rows")

    upserted = 0
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        exclusions = await _load_exclusions(db)

        for r in rows:
            def col(i, default=""):
                return r[i].strip() if i < len(r) and r[i] else default

            tanggal = parse_date(col(1))
            if not tanggal:
                continue

            try:
                nominal = int(float(str(col(5, "0")).replace(".", "").replace(",", "")))
            except (ValueError, TypeError):
                nominal = 0

            name  = col(2)
            phone = normalize_phone(col(3))
            cs    = col(12)

            rhash   = row_hash(tanggal, nominal, phone, cs, name)
            is_inst = 1 if _is_institutional(name, tanggal, nominal, exclusions) else 0

            await db.execute("""
                INSERT INTO donations
                    (row_hash, tanggal, donor_name, donor_phone, ig_username,
                     nominal, kode_program, asal_donasi, cs, platform,
                     bulan, keterangan, is_institusional)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(row_hash) DO UPDATE SET
                    is_institusional = excluded.is_institusional
            """, (
                rhash, tanggal, name, phone, col(4),
                nominal, col(6), col(7), cs, col(13),
                col(11), col(10), is_inst
            ))
            upserted += 1

        await db.commit()

        # Update last_synced_at dan row count
        await db.execute("""
            UPDATE sheet_sources
            SET last_synced_at = datetime('now'), last_row_count = ?
            WHERE id = ?
        """, (upserted, src_id))
        await db.commit()

    return {"rows_fetched": len(rows), "rows_upserted": upserted, "source": source["label"]}


# Tab-tab yang BUKAN CS (harus dilewati saat scan per-tab)
_SKIP_TAB_PREFIXES = (
    "form responses", "rekap ", "nama program", "koreksi",
    "telat konfirm", "donasi riba", "kemitraan", "donasi lain",
    "ip", "devi", "cs ap", "cantika", "osa", "cs 1", "cs 2",
    "cs 3", "cs 4", "cs 5", "cs 6", "cs 7", "cs 8", "cs 9",
)

async def _list_tabs(sid: str, api_key: str) -> list[str]:
    """Ambil semua tab dari spreadsheet, kembalikan nama tab yang merupakan CS."""
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sid}?fields=sheets.properties.title&key={api_key}"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

    all_tabs = [s["properties"]["title"] for s in data.get("sheets", [])]
    cs_tabs = []
    for t in all_tabs:
        tl = t.lower()
        if not any(tl.startswith(skip) for skip in _SKIP_TAB_PREFIXES):
            cs_tabs.append(t)
    return cs_tabs


async def _sync_multi_tab(source: dict, api_key: str) -> dict:
    """
    Untuk historical sheet dengan format per-tab CS.
    Scan semua tab, baca data per-tab, inject nama tab sebagai CS.
    """
    sid    = source["spreadsheet_id"]
    src_id = source["id"]
    year   = source.get("source_year")  # None kalau kolom tidak ada

    cs_tabs = await _list_tabs(sid, api_key)
    log.info(f"[MultiSync] '{source['label']}': {len(cs_tabs)} CS tabs: {cs_tabs}")

    total_fetched = total_upserted = 0

    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        exclusions = await _load_exclusions(db)

        for tab in cs_tabs:
            import urllib.parse
            tab_enc = urllib.parse.quote(tab)
            url = (
                f"https://sheets.googleapis.com/v4/spreadsheets/{sid}"
                f"/values/{tab_enc}!A2:N?key={api_key}"
            )
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    data = resp.json()
            except Exception as e:
                log.warning(f"[MultiSync] Tab '{tab}' error: {e}")
                continue

            rows = data.get("values", [])
            total_fetched += len(rows)
            cs_from_tab = tab.upper()  # gunakan nama tab sebagai CS

            for r in rows:
                def col(i, default=""):
                    return r[i].strip() if i < len(r) and r[i] else default

                tanggal = parse_date(col(1))
                if not tanggal:
                    continue

                try:
                    nominal = int(float(str(col(5, "0")).replace(".", "").replace(",", "")))
                except (ValueError, TypeError):
                    nominal = 0

                if nominal <= 0:
                    continue

                name  = col(2)
                phone = normalize_phone(col(3))
                # Gunakan kolom CS kalau ada, fallback ke nama tab
                cs    = col(12) or cs_from_tab

                rhash   = row_hash(tanggal, nominal, phone, cs, name)
                is_inst = 1 if _is_institutional(name, tanggal, nominal, exclusions) else 0

                await db.execute("""
                    INSERT INTO donations
                        (row_hash, tanggal, donor_name, donor_phone, ig_username,
                         nominal, kode_program, asal_donasi, cs, platform,
                         bulan, keterangan, is_institusional, source_year)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(row_hash) DO UPDATE SET
                        is_institusional = excluded.is_institusional
                """, (
                    rhash, tanggal, name, phone, col(4),
                    nominal, col(6), col(7), cs, col(13),
                    col(11), col(10), is_inst, year
                ))
                total_upserted += 1

        await db.commit()
        await db.execute("""
            UPDATE sheet_sources
            SET last_synced_at = datetime('now'), last_row_count = ?
            WHERE id = ?
        """, (total_upserted, src_id))
        await db.commit()

    return {"rows_fetched": total_fetched, "rows_upserted": total_upserted, "source": source["label"]}


# ── Main: sync semua sumber aktif ────────────────────────────────────────
async def sync_from_sheets():
    api_key = GOOGLE_API_KEY or os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        log.warning("[Sync] GOOGLE_API_KEY tidak dikonfigurasi — skip")
        return

    # Ambil semua sumber aktif dari DB
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sheet_sources WHERE is_active = 1 ORDER BY id"
        ) as cur:
            sources = [dict(r) for r in await cur.fetchall()]

    if not sources:
        log.warning("[Sync] Tidak ada sumber aktif di sheet_sources")
        return

    total_fetched = total_upserted = 0
    errors = []

    for src in sources:
        try:
            # Pilih mode sync: multi-tab (per CS) atau single-tab biasa
            if src.get("sheet_name", "").lower().strip() == "per-tab cs":
                result = await _sync_multi_tab(src, api_key)
            else:
                result = await _sync_one(src, api_key)
            total_fetched  += result["rows_fetched"]
            total_upserted += result["rows_upserted"]
            log.info(f"[Sync] '{src['label']}' done: {result['rows_upserted']} upserted")
        except Exception as e:
            log.error(f"[Sync] '{src['label']}' error: {e}", exc_info=True)
            errors.append(f"{src['label']}: {e}")

    status = "error" if errors else "ok"
    msg    = "; ".join(errors) if errors else None

    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute(
            "INSERT INTO sync_log (rows_fetched, rows_upserted, status, message) VALUES (?,?,?,?)",
            (total_fetched, total_upserted, status, msg)
        )
        await db.commit()

    log.info(f"[Sync] Selesai: {total_upserted}/{total_fetched} rows, {len(sources)} sumber")
    return {"rows_fetched": total_fetched, "rows_upserted": total_upserted, "sources": len(sources)}
