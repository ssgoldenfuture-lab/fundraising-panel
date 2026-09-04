"""
berdonasi_db.py — Async MySQL queries ke database berdonasi.goldenfutureindonesia.org
Menampilkan data transaksi online di halaman Analisis Web
"""
import aiomysql
import os
from datetime import datetime, timedelta

# Credentials — diisi dari main.py saat startup
DB_CFG: dict = {}


async def _fetch(sql: str, args=()) -> list[dict]:
    """Jalankan SELECT dan return list of dict."""
    async with aiomysql.connect(**DB_CFG) as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(sql, args)
            return list(await cur.fetchall())


async def test_connection() -> bool:
    """Return True kalau berhasil connect."""
    try:
        rows = await _fetch("SELECT 1 AS ok")
        return bool(rows)
    except Exception:
        return False


# ── KPI Transaksi ──────────────────────────────────────────────────────────────

async def kpi_transaksi(days: int = 30) -> dict:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = await _fetch("""
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN status = 'paid'      THEN 1 ELSE 0 END) AS paid,
          SUM(CASE WHEN status = 'initiated' THEN 1 ELSE 0 END) AS pending,
          SUM(CASE WHEN status IN ('failed','expired') THEN 1 ELSE 0 END) AS gagal,
          SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END) AS revenue,
          SUM(amount) AS gross
        FROM donations
        WHERE DATE(created_at) >= %s
    """, (since,))
    r = rows[0] if rows else {}
    total = int(r.get("total") or 0)
    paid  = int(r.get("paid") or 0)
    return {
        "total":        total,
        "paid":         paid,
        "pending":      int(r.get("pending") or 0),
        "gagal":        int(r.get("gagal") or 0),
        "revenue":      float(r.get("revenue") or 0),
        "gross":        float(r.get("gross") or 0),
        "conv_rate":    round(paid / total * 100, 1) if total else 0,
        "days":         days,
    }


# ── Tren Harian ───────────────────────────────────────────────────────────────

async def tren_harian(days: int = 30) -> list[dict]:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = await _fetch("""
        SELECT
          DATE(created_at) AS label,
          COUNT(*) AS total,
          SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END) AS paid,
          SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END) AS revenue
        FROM donations
        WHERE DATE(created_at) >= %s
        GROUP BY DATE(created_at)
        ORDER BY DATE(created_at)
    """, (since,))
    return [
        {
            "label":   str(r["label"]),
            "total":   int(r["total"] or 0),
            "paid":    int(r["paid"] or 0),
            "revenue": float(r["revenue"] or 0),
        }
        for r in rows
    ]


# ── Top Campaign ──────────────────────────────────────────────────────────────

async def top_campaigns(days: int = 30, limit: int = 10) -> list[dict]:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = await _fetch("""
        SELECT
          c.title AS campaign,
          COUNT(d.id) AS total,
          SUM(CASE WHEN d.status = 'paid' THEN 1 ELSE 0 END) AS paid,
          SUM(CASE WHEN d.status = 'paid' THEN d.amount ELSE 0 END) AS revenue
        FROM donations d
        JOIN campaigns c ON c.id = d.campaign_id
        WHERE DATE(d.created_at) >= %s
        GROUP BY d.campaign_id, c.title
        ORDER BY revenue DESC
        LIMIT %s
    """, (since, limit))
    return [
        {
            "campaign": r["campaign"],
            "total":    int(r["total"] or 0),
            "paid":     int(r["paid"] or 0),
            "revenue":  float(r["revenue"] or 0),
        }
        for r in rows
    ]


# ── Konversi Harian (tabel) ───────────────────────────────────────────────────

async def konversi_harian(days: int = 14) -> list[dict]:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = await _fetch("""
        SELECT
          DATE(created_at) AS tgl,
          COUNT(*) AS total,
          SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END) AS paid
        FROM donations
        WHERE DATE(created_at) >= %s
        GROUP BY DATE(created_at)
        ORDER BY DATE(created_at) DESC
    """, (since,))
    return [
        {
            "tgl":       str(r["tgl"]),
            "total":     int(r["total"] or 0),
            "paid":      int(r["paid"] or 0),
            "rate":      round(int(r["paid"] or 0) / int(r["total"] or 1) * 100, 0),
        }
        for r in rows
    ]


# ── Transaksi Terbaru ─────────────────────────────────────────────────────────

async def transaksi_terbaru(limit: int = 10) -> list[dict]:
    rows = await _fetch("""
        SELECT
          d.reference,
          d.donor_name,
          d.amount,
          d.status,
          c.title AS campaign,
          d.created_at,
          d.paid_at
        FROM donations d
        LEFT JOIN campaigns c ON c.id = d.campaign_id
        ORDER BY d.id DESC
        LIMIT %s
    """, (limit,))
    return [
        {
            "reference":  r["reference"],
            "donor_name": r["donor_name"] or "Hamba Allah",
            "amount":     float(r["amount"] or 0),
            "status":     r["status"],
            "campaign":   (r["campaign"] or "")[:50] + ("…" if len(r["campaign"] or "") > 50 else ""),
            "created_at": str(r["created_at"]),
            "paid_at":    str(r["paid_at"]) if r["paid_at"] else None,
        }
        for r in rows
    ]


async def kpi_transaksi_range(date_from: str, date_to: str) -> dict:
    """KPI transaksi online untuk rentang tanggal spesifik."""
    rows = await _fetch("""
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN status = 'paid'      THEN 1 ELSE 0 END) AS paid,
          SUM(CASE WHEN status = 'initiated' THEN 1 ELSE 0 END) AS pending,
          SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END) AS revenue
        FROM donations
        WHERE DATE(created_at) BETWEEN %s AND %s
    """, (date_from, date_to))
    r = rows[0] if rows else {}
    total = int(r.get("total") or 0)
    paid  = int(r.get("paid") or 0)
    return {
        "total":       total,
        "paid":        paid,
        "pending":     int(r.get("pending") or 0),
        "revenue":     float(r.get("revenue") or 0),
        "conv_rate":   round(paid / total * 100, 1) if total else 0,
    }


async def stuck_initiated(max_days: int = 3) -> list[dict]:
    """Transaksi initiated yang belum berubah > max_days hari (stuck payments)."""
    rows = await _fetch("""
        SELECT d.reference, d.donor_name, d.amount,
               c.title AS campaign, d.created_at,
               DATEDIFF(NOW(), d.created_at) AS days_waiting
        FROM donations d
        LEFT JOIN campaigns c ON c.id = d.campaign_id
        WHERE d.status = 'initiated'
          AND d.created_at < DATE_SUB(NOW(), INTERVAL %s DAY)
        ORDER BY d.created_at ASC
        LIMIT 20
    """, (max_days,))
    return [
        {
            "reference":    r["reference"],
            "donor_name":   r["donor_name"] or "Hamba Allah",
            "amount":       float(r["amount"] or 0),
            "campaign":     (r["campaign"] or "")[:45],
            "created_at":   str(r["created_at"]),
            "days_waiting": int(r["days_waiting"] or 0),
        }
        for r in rows
    ]
