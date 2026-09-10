"""
aggregates.py — Logic agregasi CRM & analisis donatur
Semua query ke SQLite, tidak ada live call ke Sheets saat halaman dibuka.
"""
import aiosqlite
from pathlib import Path

DB_PATH = Path(__file__).parent / "fundraising.db"


async def _fetch(sql: str, params=()) -> list[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def available_years() -> list[int]:
    """Tahun valid (2020-2030) yang ada di DB."""
    rows = await _fetch("""
        SELECT DISTINCT
            COALESCE(source_year, CAST(substr(tanggal,1,4) AS INTEGER)) AS yr
        FROM donations
        WHERE tanggal != '' AND length(tanggal) >= 4
    """)
    return sorted(
        [r["yr"] for r in rows if r["yr"] and 2020 <= r["yr"] <= 2030],
        reverse=True
    )


def _date_clause(date_from: str, date_to: str,
                 years: list[int] | None) -> tuple[str, list]:
    """
    Kalau years dipilih: filter by year only (ignore date range).
    Pakai COALESCE(source_year, substr) supaya konsisten dengan available_years.
    Kalau tidak: filter by date range biasa.
    """
    if years:
        placeholders = ",".join("?" * len(years))
        clause = f"COALESCE(source_year, CAST(substr(tanggal,1,4) AS INTEGER)) IN ({placeholders})"
        return clause, list(years)
    else:
        return "tanggal BETWEEN ? AND ?", [date_from, date_to]


# ── KPI CRM ───────────────────────────────────────────────────────────────────

async def kpi_crm(date_from: str, date_to: str, years: list[int] | None = None) -> dict:
    date_clause, dc_params = _date_clause(date_from, date_to, years)
    rows = await _fetch(f"""
        SELECT
            COALESCE(SUM(nominal), 0)   AS total,
            COUNT(*)                    AS jumlah,
            COALESCE(AVG(nominal), 0)   AS rata
        FROM donations
        WHERE {date_clause}
          AND is_institusional = 0
    """, dc_params)
    r = rows[0] if rows else {}

    cs_rows = await _fetch(f"""
        SELECT cs, SUM(nominal) AS total
        FROM donations
        WHERE {date_clause}
          AND is_institusional = 0
          AND cs != ''
        GROUP BY cs ORDER BY total DESC LIMIT 1
    """, dc_params)

    best_cs       = cs_rows[0]["cs"]    if cs_rows else "-"
    best_cs_total = cs_rows[0]["total"] if cs_rows else 0

    return {
        "total":         int(r.get("total", 0)),
        "jumlah":        int(r.get("jumlah", 0)),
        "rata":          int(r.get("rata", 0)),
        "best_cs":       best_cs,
        "best_cs_total": int(best_cs_total),
    }


# ── Tren donasi (per hari / per bulan) ────────────────────────────────────────

async def tren_donasi(date_from: str, date_to: str, granularity: str = "auto",
                      years: list[int] | None = None) -> list[dict]:
    from datetime import date
    date_clause, dc_params = _date_clause(date_from, date_to, years)

    # Tentukan granularity
    if granularity == "auto":
        if years and len(years) > 1:
            granularity = "month"
        elif years:
            granularity = "month"  # satu tahun penuh → per bulan
        else:
            try:
                d0 = date.fromisoformat(date_from)
                d1 = date.fromisoformat(date_to)
                granularity = "day" if (d1 - d0).days <= 62 else "month"
            except Exception:
                granularity = "month"

    group_expr = "tanggal" if granularity == "day" else "substr(tanggal, 1, 7)"

    rows = await _fetch(f"""
        SELECT {group_expr} AS label,
               SUM(nominal) AS total,
               COUNT(*)     AS jumlah
        FROM donations
        WHERE {date_clause}
          AND is_institusional = 0
        GROUP BY {group_expr}
        ORDER BY {group_expr}
    """, dc_params)
    return rows


# ── Ranking CS ────────────────────────────────────────────────────────────────

async def ranking_cs(date_from: str, date_to: str,
                     years: list[int] | None = None) -> list[dict]:
    date_clause, dc_params = _date_clause(date_from, date_to, years)
    rows = await _fetch(f"""
        SELECT cs, SUM(nominal) AS total, COUNT(*) AS jumlah
        FROM donations
        WHERE {date_clause}
          AND is_institusional = 0
          AND cs != ''
        GROUP BY cs
        ORDER BY total DESC
    """, dc_params)

    fav = await _fetch(f"""
        SELECT cs, kode_program, COUNT(*) AS cnt
        FROM donations
        WHERE {date_clause}
          AND is_institusional = 0
          AND cs != ''
          AND kode_program != ''
        GROUP BY cs, kode_program
        ORDER BY cs, cnt DESC
    """, dc_params)

    fav_map: dict[str, str] = {}
    seen_cs: set[str] = set()
    for f in fav:
        cs = f["cs"]
        if cs not in seen_cs:
            fav_map[cs] = f["kode_program"]
            seen_cs.add(cs)

    for r in rows:
        r["program_favorit"] = fav_map.get(r["cs"], "-")

    return rows


# ── Detail CS (untuk modal) ───────────────────────────────────────────────────

async def detail_cs(cs: str, date_from: str, date_to: str) -> dict:
    summary = await _fetch("""
        SELECT SUM(nominal) AS total, COUNT(*) AS jumlah,
               MIN(tanggal) AS tgl_awal, MAX(tanggal) AS tgl_akhir
        FROM donations
        WHERE cs = ? AND tanggal BETWEEN ? AND ? AND is_institusional = 0
    """, (cs, date_from, date_to))

    programs = await _fetch("""
        SELECT kode_program AS program, SUM(nominal) AS total, COUNT(*) AS jumlah
        FROM donations
        WHERE cs = ? AND tanggal BETWEEN ? AND ? AND is_institusional = 0
          AND kode_program != ''
        GROUP BY kode_program ORDER BY total DESC
    """, (cs, date_from, date_to))

    tren = await _fetch("""
        SELECT tanggal AS label, SUM(nominal) AS total, COUNT(*) AS jumlah
        FROM donations
        WHERE cs = ? AND tanggal BETWEEN ? AND ? AND is_institusional = 0
        GROUP BY tanggal ORDER BY tanggal
    """, (cs, date_from, date_to))

    return {
        "cs":       cs,
        "summary":  summary[0] if summary else {},
        "programs": programs,
        "tren":     tren,
    }


# ── Program ranking ───────────────────────────────────────────────────────────

async def ranking_program(date_from: str, date_to: str,
                          years: list[int] | None = None) -> list[dict]:
    date_clause, dc_params = _date_clause(date_from, date_to, years)
    return await _fetch(f"""
        SELECT kode_program AS program, SUM(nominal) AS total, COUNT(*) AS jumlah
        FROM donations
        WHERE {date_clause}
          AND is_institusional = 0
          AND kode_program != ''
        GROUP BY kode_program ORDER BY total DESC LIMIT 20
    """, dc_params)


# ── Sumber traffic (asal_donasi / platform) ───────────────────────────────────

async def sumber_traffic(date_from: str, date_to: str,
                         years: list[int] | None = None) -> list[dict]:
    date_clause, dc_params = _date_clause(date_from, date_to, years)
    return await _fetch(f"""
        SELECT COALESCE(NULLIF(platform,''), asal_donasi, 'Lainnya') AS channel,
               SUM(nominal) AS total, COUNT(*) AS jumlah
        FROM donations
        WHERE {date_clause} AND is_institusional = 0
        GROUP BY channel ORDER BY total DESC LIMIT 10
    """, dc_params)


# ── Donasi institusional ──────────────────────────────────────────────────────

async def donasi_institusional(date_from: str, date_to: str) -> list[dict]:
    return await _fetch("""
        SELECT donor_name, tanggal, kode_program, nominal, cs
        FROM donations
        WHERE tanggal BETWEEN ? AND ? AND is_institusional = 1
        ORDER BY nominal DESC
    """, (date_from, date_to))


# ── KPI Donatur ───────────────────────────────────────────────────────────────

async def kpi_donatur() -> dict:
    rows = await _fetch("""
        SELECT
            COUNT(DISTINCT donor_phone)                              AS total_unik,
            COUNT(DISTINCT CASE WHEN cnt > 1 THEN donor_phone END)  AS repeat_donor,
            COUNT(DISTINCT CASE WHEN cs_cnt > 1 THEN donor_phone END) AS multi_cs
        FROM (
            SELECT donor_phone,
                   COUNT(*)                AS cnt,
                   COUNT(DISTINCT cs)      AS cs_cnt
            FROM donations
            WHERE donor_phone != '' AND is_institusional = 0
            GROUP BY donor_phone
        )
    """)
    r = rows[0] if rows else {}
    return {
        "total_unik":   int(r.get("total_unik", 0)),
        "repeat_donor": int(r.get("repeat_donor", 0)),
        "multi_cs":     int(r.get("multi_cs", 0)),
    }


# ── Tabel donatur ─────────────────────────────────────────────────────────────

async def tabel_donatur(search: str = "", limit: int = 100, offset: int = 0) -> list[dict]:
    like = f"%{search}%" if search else "%"
    return await _fetch("""
        SELECT
            donor_name,
            donor_phone,
            SUM(nominal)      AS total,
            COUNT(*)          AS jumlah,
            GROUP_CONCAT(DISTINCT cs)    AS cs_list,
            COUNT(DISTINCT cs)           AS cs_count,
            MAX(kode_program)            AS program_raw,
            MIN(tanggal)                 AS tgl_awal,
            MAX(tanggal)                 AS tgl_akhir
        FROM donations
        WHERE (donor_name LIKE ? OR donor_phone LIKE ?)
          AND donor_phone != ''
          AND is_institusional = 0
        GROUP BY donor_phone
        ORDER BY total DESC
        LIMIT ? OFFSET ?
    """, (like, like, limit, offset))


# ── Detail donatur (untuk modal) ──────────────────────────────────────────────

async def detail_donatur(phone: str) -> dict:
    summary = await _fetch("""
        SELECT donor_name, donor_phone,
               SUM(nominal) AS total, COUNT(*) AS jumlah,
               GROUP_CONCAT(DISTINCT cs) AS cs_list,
               COUNT(DISTINCT cs) AS cs_count,
               MIN(tanggal) AS tgl_awal, MAX(tanggal) AS tgl_akhir
        FROM donations WHERE donor_phone = ? AND is_institusional = 0
    """, (phone,))

    programs = await _fetch("""
        SELECT kode_program AS program, SUM(nominal) AS total, COUNT(*) AS jumlah
        FROM donations WHERE donor_phone = ? AND is_institusional = 0 AND kode_program != ''
        GROUP BY kode_program ORDER BY total DESC
    """, (phone,))

    return {
        "summary":  summary[0] if summary else {},
        "programs": programs,
    }


# ── Last sync info ────────────────────────────────────────────────────────────

async def last_sync_info() -> dict:
    rows = await _fetch("""
        SELECT synced_at, rows_fetched, rows_upserted, status, message
        FROM sync_log ORDER BY id DESC LIMIT 1
    """)
    return rows[0] if rows else {}
