"""
web_analytics.py — Aggregate queries untuk web analytics (self-hosted tracker)
"""
import aiosqlite
from datetime import datetime, timedelta

DB_PATH: str = ""  # diisi dari main.py saat import

async def _fetch(sql: str, params=()) -> list[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def ensure_table():
    """Buat tabel web_events kalau belum ada."""
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS web_events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           TEXT    NOT NULL DEFAULT (datetime('now')),
                event        TEXT    NOT NULL DEFAULT 'pageview',
                session_id   TEXT,
                path         TEXT,
                referrer     TEXT,
                utm_source   TEXT,
                utm_medium   TEXT,
                utm_campaign TEXT,
                utm_content  TEXT,
                utm_term     TEXT,
                ip_hash      TEXT
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_we_ts ON web_events(ts)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_we_session ON web_events(session_id)")
        await db.commit()


async def insert_event(data: dict, ip: str = ""):
    """Simpan satu event tracker."""
    import hashlib
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:16] if ip else ""
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("""
            INSERT INTO web_events
              (event, session_id, path, referrer,
               utm_source, utm_medium, utm_campaign, utm_content, utm_term, ip_hash)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            data.get("event", "pageview"),
            data.get("session_id", ""),
            data.get("path", ""),
            data.get("referrer", ""),
            data.get("utm_source", ""),
            data.get("utm_medium", ""),
            data.get("utm_campaign", ""),
            data.get("utm_content", ""),
            data.get("utm_term", ""),
            ip_hash,
        ))
        await db.commit()


# ── KPI ───────────────────────────────────────────────────────────────────────

async def kpi_web(days: int = 30) -> dict:
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    rows = await _fetch("""
        SELECT
          COUNT(*)                                      AS pageviews,
          COUNT(DISTINCT session_id)                    AS sessions,
          COUNT(DISTINCT ip_hash)                       AS visitors
        FROM web_events
        WHERE ts >= ? AND event = 'pageview'
    """, (since,))
    r = rows[0] if rows else {}

    # Hari ini
    today_rows = await _fetch("""
        SELECT COUNT(*) AS pv, COUNT(DISTINCT session_id) AS sess
        FROM web_events
        WHERE date(ts) = date('now') AND event = 'pageview'
    """)
    t = today_rows[0] if today_rows else {}

    return {
        "pageviews":       int(r.get("pageviews", 0)),
        "sessions":        int(r.get("sessions", 0)),
        "visitors":        int(r.get("visitors", 0)),
        "today_pv":        int(t.get("pv", 0)),
        "today_sessions":  int(t.get("sess", 0)),
        "days":            days,
    }


async def tren_harian(days: int = 30) -> list[dict]:
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    return await _fetch("""
        SELECT date(ts) AS label,
               COUNT(*) AS pageviews,
               COUNT(DISTINCT session_id) AS sessions
        FROM web_events
        WHERE ts >= ? AND event = 'pageview'
        GROUP BY date(ts)
        ORDER BY date(ts)
    """, (since,))


async def traffic_sources(days: int = 30) -> list[dict]:
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    return await _fetch("""
        SELECT
          CASE
            WHEN utm_source != '' THEN utm_source
            WHEN referrer LIKE '%google%' THEN 'google'
            WHEN referrer LIKE '%facebook%' OR referrer LIKE '%fb.com%' THEN 'facebook'
            WHEN referrer LIKE '%instagram%' THEN 'instagram'
            WHEN referrer LIKE '%tiktok%' THEN 'tiktok'
            WHEN referrer LIKE '%twitter%' OR referrer LIKE '%t.co%' THEN 'twitter'
            WHEN referrer LIKE '%whatsapp%' OR referrer LIKE '%wa.me%' THEN 'whatsapp'
            WHEN referrer = '' THEN 'direct'
            ELSE 'referral'
          END AS source,
          COUNT(*) AS pageviews,
          COUNT(DISTINCT session_id) AS sessions
        FROM web_events
        WHERE ts >= ? AND event = 'pageview'
        GROUP BY source
        ORDER BY sessions DESC
        LIMIT 15
    """, (since,))


async def top_pages(days: int = 30) -> list[dict]:
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    return await _fetch("""
        SELECT path,
               COUNT(*) AS pageviews,
               COUNT(DISTINCT session_id) AS sessions
        FROM web_events
        WHERE ts >= ? AND event = 'pageview' AND path != ''
        GROUP BY path
        ORDER BY pageviews DESC
        LIMIT 20
    """, (since,))


async def utm_campaigns(days: int = 30) -> list[dict]:
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    return await _fetch("""
        SELECT
          utm_campaign,
          utm_source,
          utm_medium,
          COUNT(*) AS pageviews,
          COUNT(DISTINCT session_id) AS sessions
        FROM web_events
        WHERE ts >= ? AND event = 'pageview' AND utm_campaign != ''
        GROUP BY utm_campaign, utm_source, utm_medium
        ORDER BY sessions DESC
        LIMIT 30
    """, (since,))


async def recent_events(limit: int = 50) -> list[dict]:
    return await _fetch("""
        SELECT ts, event, path, utm_source, utm_campaign, referrer
        FROM web_events
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))


# ── Donation analytics (pull dari tabel donations yang sama) ──────────────────

async def donation_kpi(days: int = 30) -> dict:
    """KPI donasi dalam N hari terakhir."""
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()[:10]
    rows = await _fetch("""
        SELECT
          COUNT(*)                       AS jumlah,
          COALESCE(SUM(nominal), 0)      AS total,
          COALESCE(AVG(nominal), 0)      AS rata,
          COUNT(DISTINCT donor_phone)    AS donor_unik
        FROM donations
        WHERE tanggal >= ? AND is_institusional = 0
    """, (since,))
    r = rows[0] if rows else {}
    return {
        "jumlah":      int(r.get("jumlah", 0)),
        "total":       int(r.get("total", 0)),
        "rata":        int(r.get("rata", 0)),
        "donor_unik":  int(r.get("donor_unik", 0)),
        "days":        days,
    }


async def tren_donasi_harian(days: int = 30) -> list[dict]:
    """Donasi per hari — untuk di-overlay dengan web traffic."""
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()[:10]
    return await _fetch("""
        SELECT tanggal AS label,
               COUNT(*) AS jumlah,
               COALESCE(SUM(nominal), 0) AS total
        FROM donations
        WHERE tanggal >= ? AND is_institusional = 0
        GROUP BY tanggal
        ORDER BY tanggal
    """, (since,))


async def donasi_per_program(days: int = 30) -> list[dict]:
    """Top program donasi dalam periode."""
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()[:10]
    return await _fetch("""
        SELECT kode_program AS program,
               COUNT(*) AS jumlah,
               SUM(nominal) AS total
        FROM donations
        WHERE tanggal >= ? AND is_institusional = 0 AND kode_program != ''
        GROUP BY kode_program
        ORDER BY total DESC
        LIMIT 10
    """, (since,))


async def donasi_per_source(days: int = 30) -> list[dict]:
    """Donasi per sumber (asal_donasi / platform)."""
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()[:10]
    return await _fetch("""
        SELECT
          COALESCE(NULLIF(platform,''), NULLIF(asal_donasi,''), 'Lainnya') AS source,
          COUNT(*) AS jumlah,
          SUM(nominal) AS total
        FROM donations
        WHERE tanggal >= ? AND is_institusional = 0
        GROUP BY source
        ORDER BY total DESC
        LIMIT 10
    """, (since,))


async def combined_daily(days: int = 30) -> list[dict]:
    """
    Gabungkan web traffic + donation per hari.
    Return list of {date, pageviews, sessions, donations, revenue}
    """
    since_dt  = datetime.utcnow() - timedelta(days=days)
    since_iso = since_dt.isoformat()
    since_date = since_dt.strftime("%Y-%m-%d")

    web = await _fetch("""
        SELECT date(ts) AS d, COUNT(*) AS pv, COUNT(DISTINCT session_id) AS sess
        FROM web_events WHERE ts >= ? AND event = 'pageview'
        GROUP BY date(ts)
    """, (since_iso,))
    web_map = {r["d"]: r for r in web}

    don = await _fetch("""
        SELECT tanggal AS d, COUNT(*) AS cnt, SUM(nominal) AS rev
        FROM donations WHERE tanggal >= ? AND is_institusional = 0
        GROUP BY tanggal
    """, (since_date,))
    don_map = {r["d"]: r for r in don}

    # Gabungkan semua tanggal yang ada
    all_dates = sorted(set(list(web_map.keys()) + list(don_map.keys())))
    result = []
    for d in all_dates:
        w = web_map.get(d, {})
        dn = don_map.get(d, {})
        result.append({
            "label":      d,
            "pageviews":  int(w.get("pv", 0)),
            "sessions":   int(w.get("sess", 0)),
            "donations":  int(dn.get("cnt", 0)),
            "revenue":    int(dn.get("rev", 0)),
        })
    return result
