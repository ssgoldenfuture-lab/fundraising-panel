"""
program_analytics.py — Analisis performa kode_program dari DB donasi (tanpa kalender)
Data coverage: semua donasi yang sudah masuk (2023 - sekarang)
"""
import logging
import aiosqlite
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger("program_analytics")
DB_PATH = Path(__file__).parent / "fundraising.db"


async def _fetch(sql: str, params=()) -> list[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_program_performance_all_time() -> dict:
    """
    Analisis total performa setiap kode_program sepanjang masa dari DB.
    Tidak butuh kalender — pakai data donasi langsung.
    """
    # ── Total per program (all time) ─────────────────────────────────
    all_time = await _fetch("""
        SELECT
            kode_program,
            SUM(nominal)   AS total_rp,
            COUNT(*)       AS jumlah_trx,
            AVG(nominal)   AS avg_per_trx,
            MIN(tanggal)   AS pertama,
            MAX(tanggal)   AS terakhir,
            COUNT(DISTINCT strftime('%Y-%m', tanggal)) AS aktif_bulan
        FROM donations
        WHERE kode_program != '' AND is_institusional = 0
              AND tanggal IS NOT NULL AND tanggal != ''
        GROUP BY kode_program
        ORDER BY total_rp DESC
    """)

    # ── Per program per bulan (trend) ─────────────────────────────────
    monthly = await _fetch("""
        SELECT
            kode_program,
            strftime('%Y-%m', tanggal) AS bulan,
            SUM(nominal)  AS total_rp,
            COUNT(*)      AS jumlah_trx
        FROM donations
        WHERE kode_program != '' AND is_institusional = 0
              AND tanggal IS NOT NULL AND tanggal != ''
        GROUP BY kode_program, bulan
        ORDER BY kode_program, bulan
    """)

    # ── Top program per tahun ─────────────────────────────────────────
    yearly = await _fetch("""
        SELECT
            strftime('%Y', tanggal) AS tahun,
            kode_program,
            SUM(nominal) AS total_rp,
            COUNT(*) AS jumlah_trx
        FROM donations
        WHERE kode_program != '' AND is_institusional = 0
              AND tanggal IS NOT NULL AND tanggal != ''
        GROUP BY tahun, kode_program
        ORDER BY tahun, total_rp DESC
    """)

    # ── Bulan terbaik per program ─────────────────────────────────────
    best_months = await _fetch("""
        SELECT
            kode_program,
            strftime('%Y-%m', tanggal) AS bulan,
            SUM(nominal) AS total_rp,
            COUNT(*) AS jumlah_trx
        FROM donations
        WHERE kode_program != '' AND is_institusional = 0
              AND tanggal IS NOT NULL AND tanggal != ''
        GROUP BY kode_program, bulan
        ORDER BY kode_program, total_rp DESC
    """)

    # ── Struktur data untuk AI ────────────────────────────────────────
    # Group monthly by program
    monthly_by_prog: dict[str, list] = {}
    for r in monthly:
        p = r["kode_program"]
        if p not in monthly_by_prog:
            monthly_by_prog[p] = []
        monthly_by_prog[p].append({"bulan": r["bulan"], "total_rp": r["total_rp"], "jumlah_trx": r["jumlah_trx"]})

    # Group yearly top per tahun (filter None tahun)
    yearly_top: dict[str, list] = {}
    for r in yearly:
        t = r["tahun"]
        if not t:  # skip NULL tahun
            continue
        if t not in yearly_top:
            yearly_top[t] = []
        if len(yearly_top[t]) < 5:
            yearly_top[t].append({"program": r["kode_program"], "total_rp": r["total_rp"], "jumlah_trx": r["jumlah_trx"]})

    # Best month per program (top 3)
    best_by_prog: dict[str, list] = {}
    for r in best_months:
        p = r["kode_program"]
        if p not in best_by_prog:
            best_by_prog[p] = []
        if len(best_by_prog[p]) < 3:
            best_by_prog[p].append({"bulan": r["bulan"], "total_rp": r["total_rp"]})

    # Avg per bulan aktif (efisiensi)
    prog_summary = []
    for r in all_time:
        p = r["kode_program"]
        aktif = r["aktif_bulan"] or 1
        prog_summary.append({
            "program":         p,
            "total_rp":        int(r["total_rp"] or 0),
            "jumlah_trx":      int(r["jumlah_trx"] or 0),
            "avg_per_trx":     int(r["avg_per_trx"] or 0),
            "avg_per_bulan":   int((r["total_rp"] or 0) / aktif),
            "aktif_bulan":     int(aktif),
            "pertama":         r["pertama"],
            "terakhir":        r["terakhir"],
            "bulan_terbaik":   best_by_prog.get(p, [])[:2],
            "tren_6bln":       monthly_by_prog.get(p, [])[-6:],  # 6 bulan terakhir
        })

    return {
        "generated_at":      date.today().isoformat(),
        "total_program":     len(prog_summary),
        "ranking_all_time":  prog_summary[:20],          # top 20 by total
        "ranking_by_avg_bulan": sorted(
            prog_summary, key=lambda x: x["avg_per_bulan"], reverse=True
        )[:10],                                           # top 10 by efisiensi bulanan
        "top_per_tahun":     yearly_top,
        "ringkasan_singkat": [
            f"{r['program']}: Rp {r['total_rp']:,.0f} ({r['jumlah_trx']} trx, {r['aktif_bulan']} bulan aktif)"
            for r in prog_summary[:15]
        ],
    }


async def get_program_monthly_trend(program: str, months: int = 12) -> list[dict]:
    """Trend bulanan untuk 1 program spesifik."""
    rows = await _fetch("""
        SELECT
            strftime('%Y-%m', tanggal) AS bulan,
            SUM(nominal) AS total_rp,
            COUNT(*) AS jumlah_trx
        FROM donations
        WHERE kode_program = ? AND is_institusional = 0
        GROUP BY bulan
        ORDER BY bulan DESC
        LIMIT ?
    """, [program.upper(), months])
    return list(reversed(rows))


async def get_cross_program_comparison(programs: list[str]) -> dict:
    """Bandingkan beberapa program head-to-head per bulan."""
    if not programs:
        return {}
    placeholders = ",".join("?" * len(programs))
    rows = await _fetch(f"""
        SELECT
            kode_program,
            strftime('%Y-%m', tanggal) AS bulan,
            SUM(nominal) AS total_rp,
            COUNT(*) AS jumlah_trx
        FROM donations
        WHERE kode_program IN ({placeholders}) AND is_institusional = 0
        GROUP BY kode_program, bulan
        ORDER BY bulan DESC
    """, [p.upper() for p in programs])

    result: dict[str, list] = {}
    for r in rows:
        p = r["kode_program"]
        if p not in result:
            result[p] = []
        result[p].append({"bulan": r["bulan"], "total_rp": r["total_rp"], "jumlah_trx": r["jumlah_trx"]})

    return result
