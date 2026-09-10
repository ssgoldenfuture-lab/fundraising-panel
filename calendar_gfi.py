"""
calendar_gfi.py — Integrasi Google Calendar untuk Kalender Konten GFI
Fetch events dari iCal, korelasikan dengan data donasi harian.
"""
import re
import logging
import httpx
import aiosqlite
from datetime import datetime, date, timedelta
from pathlib import Path

log = logging.getLogger("calendar_gfi")

ICAL_URL = "https://calendar.google.com/calendar/ical/zuld8321%40gmail.com/public/full.ics"
DB_PATH  = Path(__file__).parent / "fundraising.db"

# Window maksimum kalau tidak ada konten berikutnya
MAX_WINDOW_DAYS = 14


# ── Parse iCal ────────────────────────────────────────────────────────────────

def _unescape(text: str) -> str:
    """Unescape iCal escaped characters."""
    return text.replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _strip_html(text: str) -> str:
    """Strip HTML tags dari description."""
    clean = re.sub(r'<[^>]+>', '', text)
    clean = clean.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    clean = re.sub(r'\n{3,}', '\n\n', clean)
    return clean.strip()


def _parse_ical_date(raw: str) -> date | None:
    """Parse DTSTART value ke date object."""
    raw = raw.strip()
    try:
        if "T" in raw:
            return datetime.strptime(raw[:8], "%Y%m%d").date()
        return datetime.strptime(raw[:8], "%Y%m%d").date()
    except Exception:
        return None


def _parse_ical(content: str) -> list[dict]:
    """Parse iCal content menjadi list of event dicts."""
    events = []
    blocks = content.split("BEGIN:VEVENT")

    for block in blocks[1:]:
        ev = {}

        # Unfold lines (iCal wrap panjang dengan newline + spasi/tab)
        block = re.sub(r'\r?\n[ \t]', '', block)

        # Summary / judul
        m = re.search(r'^SUMMARY:(.*?)$', block, re.MULTILINE)
        ev["judul"] = _unescape(m.group(1).strip()) if m else ""

        # Date start
        m = re.search(r'^DTSTART[^:]*:(.*?)$', block, re.MULTILINE)
        ev["tanggal"] = _parse_ical_date(m.group(1)) if m else None

        # Date end
        m = re.search(r'^DTEND[^:]*:(.*?)$', block, re.MULTILINE)
        ev["tanggal_selesai"] = _parse_ical_date(m.group(1)) if m else ev.get("tanggal")

        # Description / narasi
        m = re.search(r'^DESCRIPTION:(.*?)(?=^[A-Z])', block, re.MULTILINE | re.DOTALL)
        if m:
            raw_desc = _unescape(m.group(1))
            ev["narasi_html"] = raw_desc.strip()
            ev["narasi"]      = _strip_html(raw_desc)
        else:
            ev["narasi"] = ev["narasi_html"] = ""

        # Attachments (Google Drive links)
        attachments = re.findall(r'^ATTACH[^:]*:(https?://[^\r\n]+)', block, re.MULTILINE)
        ev["media"] = [a.strip() for a in attachments]

        # Attachment dengan nama file
        att_titles = re.findall(r'^ATTACH;FILENAME=([^:]+):(.+)$', block, re.MULTILINE)
        ev["media_named"] = [{"name": t.strip(), "url": u.strip()} for t, u in att_titles]

        # UID
        m = re.search(r'^UID:(.*?)$', block, re.MULTILINE)
        ev["uid"] = m.group(1).strip() if m else ""

        # Skip event tanpa judul / tanpa tanggal
        if not ev["judul"] or not ev["tanggal"] or ev["judul"] == "Busy":
            continue

        events.append(ev)

    # Urutkan berdasarkan tanggal terbaru dulu
    events.sort(key=lambda e: e["tanggal"], reverse=True)
    return events


async def fetch_events() -> list[dict]:
    """Fetch dan parse events dari Google Calendar iCal."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(ICAL_URL)
            resp.raise_for_status()
        return _parse_ical(resp.text)
    except Exception as e:
        log.error(f"Gagal fetch kalender: {e}")
        return []


# ── Deteksi program dari judul event ─────────────────────────────────────────

# Mapping keyword judul → kode program (auto-detect)
_KEYWORD_PROGRAM = {
    "gaza":        ["P7", "WBP"],
    "palestina":   ["P7", "WBP"],
    "air bersih":  ["WBP", "WBA", "WBS"],
    "sumur":       ["WBP", "WBA", "WBS"],
    "wbp":         ["WBP"],
    "yatim":       ["P1", "P4"],
    "zakat":       ["ZAK", "ZAKP"],
    "qurban":      ["QA", "QP", "QY"],
    "bencana":     ["EWP", "DUP"],
    "kebencanaan": ["EWP", "DUP"],
    "kemiskinan":  ["KEI"],
    "wakaf":       ["WBA", "WBS", "WBI", "WS"],
    "ramadan":     ["RP"],
    "infaq":       ["UM", "SU"],
    "sedekah":     ["UM", "SU", "EWP"],
}

def detect_programs(judul: str) -> list[str]:
    """Deteksi kode program dari judul event."""
    judul_lower = judul.lower()
    found = []
    for keyword, codes in _KEYWORD_PROGRAM.items():
        if keyword in judul_lower:
            for c in codes:
                if c not in found:
                    found.append(c)
    return found


# ── Korelasi donasi ───────────────────────────────────────────────────────────

async def get_donation_stats(
    blast_date: date,
    programs: list[str] | None = None,
    window_days: int = MAX_WINDOW_DAYS,
) -> dict:
    """
    Ambil total donasi dalam window blast_date sampai blast_date + window_days.
    Kalau programs ada → filter per program.
    """
    date_from = blast_date.isoformat()
    date_to   = (blast_date + timedelta(days=window_days)).isoformat()

    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute("PRAGMA journal_mode=WAL")  # izinkan concurrent read
            db.row_factory = aiosqlite.Row

            if programs:
                placeholders = ",".join("?" * len(programs))
                rows = await db.execute_fetchall(f"""
                    SELECT SUM(nominal) AS total, COUNT(*) AS jumlah,
                           kode_program
                    FROM donations
                    WHERE tanggal BETWEEN ? AND ?
                      AND is_institusional = 0
                      AND kode_program IN ({placeholders})
                    GROUP BY kode_program ORDER BY total DESC
                """, [date_from, date_to] + programs)
            else:
                rows = await db.execute_fetchall("""
                    SELECT SUM(nominal) AS total, COUNT(*) AS jumlah
                    FROM donations
                    WHERE tanggal BETWEEN ? AND ?
                      AND is_institusional = 0
                """, [date_from, date_to])

            # Per-hari dalam window
            daily = await db.execute_fetchall("""
                SELECT tanggal, SUM(nominal) AS total, COUNT(*) AS jumlah
                FROM donations
                WHERE tanggal BETWEEN ? AND ?
                  AND is_institusional = 0
                GROUP BY tanggal ORDER BY tanggal
            """, [date_from, date_to])

            if programs and rows:
                total  = sum(r["total"] or 0 for r in rows)
                jumlah = sum(r["jumlah"] or 0 for r in rows)
                by_prog = [{"program": r["kode_program"], "total": r["total"] or 0} for r in rows]
            elif rows:
                total  = rows[0]["total"] or 0
                jumlah = rows[0]["jumlah"] or 0
                by_prog = []
            else:
                total = jumlah = 0
                by_prog = []

            return {
                "total_rp":      int(total),
                "jumlah_trx":    int(jumlah),
                "date_from":     date_from,
                "date_to":       date_to,
                "by_program":    by_prog,
                "daily":         [dict(r) for r in daily],
                "window_days":   window_days,
            }
    except Exception as e:
        log.warning(f"Gagal ambil donation stats: {e}")
        return {"total_rp": 0, "jumlah_trx": 0, "by_program": [], "daily": []}


async def get_all_content_performance() -> dict:
    """
    Ambil SEMUA event dari kalender (tanpa batas waktu) + korelasikan donasi.
    Return ringkasan per-program untuk AI context yang efisien.
    """
    events = await get_calendar_with_stats(months=None)  # semua event

    # Ringkasan per program/tema dari seluruh history
    by_program: dict[str, dict] = {}
    all_blasts = []

    for ev in events:
        if ev.get("is_future") or not ev.get("donasi_stats"):
            continue
        stats = ev["donasi_stats"]
        judul = ev["judul"]
        progs = ev.get("kode_program_detected") or ["UMUM"]

        all_blasts.append({
            "tanggal": ev["tanggal_iso"],
            "judul":   judul,
            "program": progs,
            "total_rp": stats["total_rp"],
            "jumlah_trx": stats["jumlah_trx"],
            "window": ev.get("window_label", "-"),
        })

        for p in progs:
            if p not in by_program:
                by_program[p] = {"total_rp": 0, "jumlah_trx": 0, "count_blast": 0, "blasts": []}
            by_program[p]["total_rp"]    += stats["total_rp"]
            by_program[p]["jumlah_trx"]  += stats["jumlah_trx"]
            by_program[p]["count_blast"] += 1
            by_program[p]["blasts"].append({"tanggal": ev["tanggal_iso"], "judul": judul, "total_rp": stats["total_rp"]})

    # Hitung rata-rata per blast per program
    for p, d in by_program.items():
        d["avg_per_blast"] = int(d["total_rp"] / d["count_blast"]) if d["count_blast"] > 0 else 0

    # Sort by total
    ranked = sorted(by_program.items(), key=lambda x: x[1]["total_rp"], reverse=True)

    return {
        "per_program": {k: v for k, v in ranked},
        "semua_blast": sorted(all_blasts, key=lambda x: x["total_rp"], reverse=True),
        "total_event": len(all_blasts),
    }


# ── Main function ─────────────────────────────────────────────────────────────

async def get_calendar_with_stats(
    months: int | None = 3,   # None = semua (tanpa batas)
    manual_window: int | None = None
) -> list[dict]:
    """
    Fetch events dari Google Calendar + korelasikan dengan donasi.
    months=None → baca SEMUA events tanpa batas waktu.
    """
    events = await fetch_events()

    # Filter berdasarkan range waktu
    if months is not None:
        cutoff = date.today() - timedelta(days=months * 30)
        relevant = [ev for ev in events if ev["tanggal"] and ev["tanggal"] >= cutoff]
    else:
        relevant = [ev for ev in events if ev["tanggal"]]  # semua, tanpa batas

    # Urutkan ascending untuk hitung window konten-ke-konten
    relevant_sorted = sorted(relevant, key=lambda e: e["tanggal"])

    # Buat lookup: tanggal event -> tanggal event BERIKUTNYA
    next_event_dates: dict[str, date] = {}
    for i, ev in enumerate(relevant_sorted):
        if i + 1 < len(relevant_sorted):
            next_ev = relevant_sorted[i + 1]
            if next_ev["tanggal"] != ev["tanggal"]:  # skip kalau same day
                next_event_dates[ev["tanggal"].isoformat()] = next_ev["tanggal"]

    # Buat set tanggal yang relevan untuk filter di loop
    relevant_dates = {ev["tanggal"] for ev in relevant}

    result = []
    for ev in events:  # kembali ke urutan descending (terbaru dulu)
        if not ev["tanggal"] or ev["tanggal"] not in relevant_dates:
            continue

        programs = detect_programs(ev["judul"])
        ev["kode_program_detected"] = programs

        # Tentukan window untuk event ini
        if manual_window:
            # Mode manual: pakai N hari
            window_days = manual_window
            ev["window_mode"]  = "manual"
            ev["window_label"] = f"{window_days} hari"
        else:
            # Mode auto: dari blast sampai konten berikutnya (max MAX_WINDOW_DAYS)
            tgl_iso = ev["tanggal"].isoformat()
            next_date = next_event_dates.get(tgl_iso)
            if next_date:
                window_days = (next_date - ev["tanggal"]).days
                window_days = min(window_days, MAX_WINDOW_DAYS)  # cap
                ev["window_mode"]      = "auto"
                ev["window_label"]     = f"{ev['tanggal'].strftime('%d %b')} → {next_date.strftime('%d %b')} ({window_days}h)"
                ev["next_event_date"]  = next_date.isoformat()
            else:
                # Event terakhir → tidak ada berikutnya, pakai max window
                window_days = MAX_WINDOW_DAYS
                ev["window_mode"]  = "auto-last"
                ev["window_label"] = f"Konten terakhir ({window_days}h)"

        ev["window_days"] = window_days

        # Ambil stats donasi (hanya untuk event yang sudah lewat)
        if ev["tanggal"] <= date.today():
            ev["donasi_stats"] = await get_donation_stats(
                ev["tanggal"], programs, window_days=window_days
            )
        else:
            ev["donasi_stats"] = None  # event masa depan

        # Format tanggal untuk display
        ev["tanggal_str"]    = ev["tanggal"].strftime("%d %b %Y")
        ev["tanggal_iso"]    = ev["tanggal"].isoformat()
        ev["is_today"]       = ev["tanggal"] == date.today()
        ev["is_future"]      = ev["tanggal"] > date.today()
        ev["narasi_preview"] = ev["narasi"][:200] + "..." if len(ev["narasi"]) > 200 else ev["narasi"]

        result.append(ev)

    return result
