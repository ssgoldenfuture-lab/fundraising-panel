"""
home_aggregates.py — Queries khusus untuk halaman Home/Ringkasan
Menggabungkan data dari CRM (SQLite) dan Analisis Web (MySQL berdonasi)
"""
import aiosqlite
from datetime import datetime, timedelta, date

# DB_PATH diisi dari main.py
DB_PATH: str = ""


async def _fetch_sqlite(sql: str, params=()) -> list[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


def _this_month() -> tuple[str, str]:
    today = date.today()
    return today.replace(day=1).isoformat(), today.isoformat()


def _last_month() -> tuple[str, str]:
    today = date.today()
    first_this = today.replace(day=1)
    last_prev  = first_this - timedelta(days=1)
    first_prev = last_prev.replace(day=1)
    return first_prev.isoformat(), last_prev.isoformat()


# ── CRM ringkasan ─────────────────────────────────────────────────────────────

async def crm_bulan_ini() -> dict:
    d0, d1 = _this_month()
    rows = await _fetch_sqlite("""
        SELECT COALESCE(SUM(nominal),0) AS total, COUNT(*) AS jumlah,
               COALESCE(AVG(nominal),0) AS rata
        FROM donations WHERE tanggal BETWEEN ? AND ? AND is_institusional=0
    """, (d0, d1))
    r = rows[0] if rows else {}
    return {"total": float(r.get("total",0)), "jumlah": int(r.get("jumlah",0)),
            "rata": float(r.get("rata",0)), "d0": d0, "d1": d1}


async def crm_bulan_lalu() -> dict:
    d0, d1 = _last_month()
    rows = await _fetch_sqlite("""
        SELECT COALESCE(SUM(nominal),0) AS total, COUNT(*) AS jumlah
        FROM donations WHERE tanggal BETWEEN ? AND ? AND is_institusional=0
    """, (d0, d1))
    r = rows[0] if rows else {}
    return {"total": float(r.get("total",0)), "jumlah": int(r.get("jumlah",0)),
            "d0": d0, "d1": d1}


async def crm_apple_to_apple() -> dict:
    """
    Bandingkan periode yang sama secara apple-to-apple:
    Bulan ini hari 1 s/d hari-N vs bulan lalu hari 1 s/d hari-N yang sama.
    Kalau hari ini tgl 3 Sept → bandingkan 1-3 Sept vs 1-3 Agustus.
    """
    today = date.today()
    day_of_month = today.day  # misal: 3

    # Bulan ini: 1 s/d hari ini
    d0_now = today.replace(day=1).isoformat()
    d1_now = today.isoformat()

    # Bulan lalu: 1 s/d hari yang sama
    first_this = today.replace(day=1)
    last_prev  = first_this - timedelta(days=1)  # hari terakhir bulan lalu
    # Hari yang sama di bulan lalu (atau hari terakhir bulan lalu kalau bulan lalu lebih pendek)
    same_day_prev = min(day_of_month, last_prev.day)
    first_prev = last_prev.replace(day=1)
    d0_prev = first_prev.isoformat()
    d1_prev = last_prev.replace(day=same_day_prev).isoformat()

    rows_now = await _fetch_sqlite("""
        SELECT COALESCE(SUM(nominal),0) AS total, COUNT(*) AS jumlah
        FROM donations WHERE tanggal BETWEEN ? AND ? AND is_institusional=0
    """, (d0_now, d1_now))
    rows_prev = await _fetch_sqlite("""
        SELECT COALESCE(SUM(nominal),0) AS total, COUNT(*) AS jumlah
        FROM donations WHERE tanggal BETWEEN ? AND ? AND is_institusional=0
    """, (d0_prev, d1_prev))

    r_now  = rows_now[0]  if rows_now  else {}
    r_prev = rows_prev[0] if rows_prev else {}
    total_now  = float(r_now.get("total",  0))
    total_prev = float(r_prev.get("total", 0))
    pct = round((total_now - total_prev) / total_prev * 100, 1) if total_prev > 0 else None

    return {
        "total_now":    total_now,
        "total_prev":   total_prev,
        "jumlah_now":   int(r_now.get("jumlah",  0)),
        "jumlah_prev":  int(r_prev.get("jumlah", 0)),
        "pct":          pct,
        "d0_now":  d0_now,  "d1_now":  d1_now,
        "d0_prev": d0_prev, "d1_prev": d1_prev,
        "day_n":   day_of_month,
    }


async def crm_top_programs(limit: int = 5) -> list[dict]:
    d0, d1 = _this_month()
    return await _fetch_sqlite("""
        SELECT kode_program AS program, COUNT(*) AS jumlah, SUM(nominal) AS total
        FROM donations
        WHERE tanggal BETWEEN ? AND ? AND is_institusional=0 AND kode_program != ''
        GROUP BY kode_program ORDER BY total DESC LIMIT ?
    """, (d0, d1, limit))


async def crm_cs_alert() -> list[dict]:
    """CS dengan total bulan ini < 50% rata-rata 3 bulan sebelumnya → tren turun."""
    today = date.today()
    d0_now, d1_now = _this_month()
    d0_prev = (today.replace(day=1) - timedelta(days=90)).isoformat()
    d1_prev = (today.replace(day=1) - timedelta(days=1)).isoformat()

    # Bulan ini per CS
    now_rows = await _fetch_sqlite("""
        SELECT cs, SUM(nominal) AS total, COUNT(*) AS jumlah
        FROM donations
        WHERE tanggal BETWEEN ? AND ? AND is_institusional=0 AND cs != ''
        GROUP BY cs
    """, (d0_now, d1_now))

    # 3 bulan sebelumnya per CS
    prev_rows = await _fetch_sqlite("""
        SELECT cs, SUM(nominal)/3.0 AS avg_per_month
        FROM donations
        WHERE tanggal BETWEEN ? AND ? AND is_institusional=0 AND cs != ''
        GROUP BY cs
    """, (d0_prev, d1_prev))

    prev_map = {r["cs"]: float(r["avg_per_month"] or 0) for r in prev_rows}
    alerts = []
    for r in now_rows:
        avg = prev_map.get(r["cs"], 0)
        if avg > 0 and float(r["total"] or 0) < avg * 0.5:
            pct = int(float(r["total"] or 0) / avg * 100)
            alerts.append({
                "cs": r["cs"],
                "total_now": float(r["total"] or 0),
                "avg_prev": avg,
                "pct": pct,
            })
    return sorted(alerts, key=lambda x: x["pct"])[:5]


async def crm_donatur_baru_besar(days: int = 7, min_nominal: int = 500_000) -> list[dict]:
    """Donatur baru (belum pernah donasi sebelumnya) dengan nominal besar dalam N hari."""
    since = (date.today() - timedelta(days=days)).isoformat()
    return await _fetch_sqlite("""
        SELECT d.donor_name, d.donor_phone, d.nominal, d.tanggal, d.kode_program, d.cs
        FROM donations d
        WHERE d.tanggal >= ?
          AND d.nominal >= ?
          AND d.is_institusional = 0
          AND d.donor_phone NOT IN (
              SELECT DISTINCT donor_phone FROM donations
              WHERE tanggal < ? AND donor_phone != ''
          )
        ORDER BY d.nominal DESC
        LIMIT 10
    """, (since, min_nominal, since))


# ── MySQL berdonasi (transaksi online) ────────────────────────────────────────
# Dipanggil dengan berdonasi_db module

async def online_bulan_ini(bdb) -> dict:
    """KPI transaksi online bulan ini."""
    try:
        d0, d1 = _this_month()
        return await bdb.kpi_transaksi_range(d0, d1)
    except Exception:
        return {"total": 0, "paid": 0, "revenue": 0, "conv_rate": 0}


async def online_stuck_payments(bdb, max_days: int = 3) -> list[dict]:
    """Transaksi initiated yang sudah > max_days hari — stuck payments."""
    try:
        return await bdb.stuck_initiated(max_days)
    except Exception:
        return []


# ── Alert: hari dengan konversi buruk ────────────────────────────────────────

async def hari_konversi_buruk(bdb, days: int = 7, threshold: float = 50.0) -> list[dict]:
    """Hari dengan konversi online < threshold% dalam N hari terakhir."""
    try:
        tren = await bdb.tren_harian(days)
        result = []
        for r in tren:
            total = r.get("total", 0)
            paid  = r.get("paid", 0)
            if total > 0:
                rate = paid / total * 100
                if rate < threshold:
                    result.append({
                        "label": r["label"],
                        "total": total,
                        "paid": paid,
                        "rate": round(rate, 1),
                    })
        return result
    except Exception:
        return []


# ── Ringkasan narasi otomatis ──────────────────────────────────────────────────

def generate_narasi(crm_now: dict, crm_a2a: dict, online_now: dict) -> str:
    """Generate ringkasan eksekutif — pakai apple-to-apple agar tidak menyesatkan."""
    def rp(v):
        v = int(v)
        if v >= 1_000_000_000: return f"Rp {v/1_000_000_000:.1f} miliar"
        if v >= 1_000_000:     return f"Rp {v/1_000_000:.1f} juta"
        return f"Rp {v:,.0f}".replace(",", ".")

    d0 = crm_now.get("d0", "")
    try:
        bulan = datetime.strptime(d0, "%Y-%m-%d").strftime("%B %Y")
    except Exception:
        bulan = "bulan ini"

    crm_total  = crm_now.get("total", 0)
    crm_jumlah = crm_now.get("jumlah", 0)
    online_rev  = online_now.get("revenue", 0)
    online_conv = online_now.get("conv_rate", 0)
    gabungan    = crm_total + online_rev

    # Kalimat 1: CRM + apple-to-apple comparison
    kalimat1 = f"Pada {bulan}, donasi yang dikonfirmasi CS mencapai {rp(crm_total)} dari {crm_jumlah:,} transaksi.".replace(",", ".")

    pct = crm_a2a.get("pct")
    day_n = crm_a2a.get("day_n", 1)
    d1_prev = crm_a2a.get("d1_prev", "")
    try:
        prev_bulan = datetime.strptime(crm_a2a.get("d0_prev", ""), "%Y-%m-%d").strftime("%B")
    except Exception:
        prev_bulan = "bulan lalu"

    if pct is not None and crm_total > 0:
        arah = "naik" if pct >= 0 else "turun"
        kalimat1 += (
            f" Dibanding {day_n} hari pertama {prev_bulan} "
            f"({rp(crm_a2a.get('total_prev', 0))}), angka ini "
            f"{arah} {abs(pct):.0f}% — perbandingan periode yang sama (1–{day_n} hari)."
        )
    elif crm_total == 0:
        kalimat1 = f"Belum ada donasi yang dikonfirmasi CS pada {bulan}."

    # Kalimat 2: Online
    if online_rev > 0:
        kalimat2 = (
            f"Transaksi online di berdonasi.goldenfutureindonesia.org menghasilkan "
            f"{rp(online_rev)} dengan tingkat konversi {online_conv:.0f}%."
        )
    else:
        kalimat2 = ""

    # Kalimat 3: Gabungan
    kalimat3 = f"Total penerimaan gabungan (CRM + online) bulan ini: {rp(gabungan)}." if gabungan > 0 else ""

    return " ".join(filter(None, [kalimat1, kalimat2, kalimat3]))
