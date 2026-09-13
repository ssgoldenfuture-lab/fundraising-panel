"""
main.py â€” FastAPI app: auth, routing, scheduled sync
"""
import os, logging, aiosqlite
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, date
from pathlib import Path

from fastapi import FastAPI, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer, BadSignature
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from models import init_db, get_user, check_password, create_user
from sheets import sync_from_sheets
import aggregates as agg
import web_analytics as wa
import berdonasi_db as bdb
import home_aggregates as hagg
import wa_bot
import wa_webhook

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s â€” %(message)s")
log = logging.getLogger("main")

SECRET_KEY = os.getenv("SECRET_KEY", "changeme-insecure")
_signer    = URLSafeTimedSerializer(SECRET_KEY)
COOKIE_NAME = "fr_session"
BASE_DIR   = Path(__file__).parent

# â”€â”€ Scheduler â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
scheduler = AsyncIOScheduler(timezone="Asia/Jakarta")

async def _startup_sync():
    """Jalankan sync pertama di background — tidak blocking startup port."""
    try:
        await sync_from_sheets()
        log.info("Startup sync selesai")
    except Exception as e:
        log.warning(f"Startup sync gagal (non-fatal): {e}")


async def _ensure_wa_log_table():
    """Buat tabel wa_broadcast_log kalau belum ada."""
    async with aiosqlite.connect(agg.DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS wa_broadcast_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                sent_at     TEXT NOT NULL,
                target      TEXT NOT NULL,
                message     TEXT,
                status      TEXT NOT NULL,
                error       TEXT,
                trigger     TEXT DEFAULT 'scheduled'
            )
        """)
        await db.commit()


async def _log_wa_broadcast(target: str, message: str, status: str,
                              error: str = "", trigger: str = "scheduled"):
    """Catat hasil broadcast WA ke DB."""
    async with aiosqlite.connect(agg.DB_PATH) as db:
        await db.execute("""
            INSERT INTO wa_broadcast_log (sent_at, target, message, status, error, trigger)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (datetime.now().isoformat(), target, message[:500], status, error, trigger))
        await db.commit()


async def _send_wa_report():
    """Job terjadwal: kirim laporan harian ke grup WA."""
    group_id = os.getenv("WA_GROUP_ID", "")
    if not group_id:
        log.warning("WA_GROUP_ID tidak diset — laporan harian WA dilewati")
        return

    try:
        crm_now    = await hagg.crm_bulan_ini()
        online_now = {"revenue": 0, "conv_rate": 0, "pending": 0}
        try:
            online_now = await hagg.online_bulan_ini(bdb)
        except Exception:
            pass
        cs_alert   = await hagg.crm_cs_alert()
        stuck_list = []
        try:
            stuck_list = await hagg.online_stuck_payments(bdb)
        except Exception:
            pass
        hari_buruk = []
        try:
            hari_buruk = await hagg.hari_konversi_buruk(bdb)
        except Exception:
            pass
        new_big = await hagg.crm_donatur_baru_besar()

        msg = wa_bot.format_laporan_harian(
            crm_now, online_now, cs_alert, stuck_list, hari_buruk, new_big
        )

        import asyncio
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: wa_bot.send_message(group_id, msg, is_group=True)
        )
        status = "ok" if result.get("ok") else "error"
        error  = result.get("error", "")
        log.info(f"WA laporan harian: status={status} target={group_id}")
        await _log_wa_broadcast(group_id, msg, status, error)

    except Exception as e:
        log.error(f"_send_wa_report error: {e}", exc_info=True)
        await _log_wa_broadcast(group_id, "", "error", str(e))

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    log.info("DB initialized")

    # Inisialisasi web analytics table
    wa.DB_PATH = agg.DB_PATH
    await wa.ensure_table()
    hagg.DB_PATH = agg.DB_PATH
    await _ensure_wa_log_table()

    # MySQL berdonasi credentials
    bdb.DB_CFG = {
        "host":   "127.0.0.1",
        "port":   3306,
        "db":     "berdonasi",
        "user":   "berdonasi_user",
        "password": os.getenv("BERDONASI_DB_PASS", "GFI_db_2025_@Xk9"),
        "charset": "utf8mb4",
        "autocommit": True,
    }
    ok = await bdb.test_connection()
    log.info(f"berdonasi MySQL: {'OK' if ok else 'GAGAL — transaksi online tidak tersedia'}")

    # Sync pertama saat startup — jalankan di background agar port langsung tersedia
    import asyncio
    asyncio.create_task(_startup_sync())

    # Sync tiap 10 menit
    scheduler.add_job(sync_from_sheets, "interval", minutes=10, id="sheets_sync",
                      misfire_grace_time=120)

    # Laporan WA harian — default jam 07:00 WIB, bisa diubah dari settings
    wa_hour = int(os.getenv("WA_REPORT_HOUR", "7"))
    scheduler.add_job(_send_wa_report, "cron", hour=wa_hour, minute=0,
                      id="wa_daily_report", misfire_grace_time=3600)

    scheduler.start()
    log.info(f"Scheduler started — sync tiap 10 menit + WA report jam {wa_hour}:00")

    yield

    scheduler.shutdown(wait=False)

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Format rupiah di template
def fmt_rp(v):
    try:
        return "Rp " + f"{int(v):,}".replace(",", ".")
    except Exception:
        return "Rp 0"

templates.env.filters["rp"] = fmt_rp

def fmt_num(v):
    try:
        return f"{int(v):,}".replace(",", ".")
    except Exception:
        return "0"

templates.env.filters["format_num"] = fmt_num

# â”€â”€ Auth helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def make_session(username: str, role: str) -> str:
    return _signer.dumps({"u": username, "r": role})

def read_session(token: str) -> dict | None:
    try:
        return _signer.loads(token, max_age=86400 * 7)  # 7 hari
    except BadSignature:
        return None

def get_current_user(request: Request) -> dict | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return read_session(token)

def require_user(request: Request) -> dict:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return user

def is_faq_reviewer(username: str) -> bool:
    reviewers = [u.strip() for u in os.getenv("FAQ_REVIEWERS", "admin").split(",")]
    return username in reviewers

templates.env.globals["is_faq_reviewer"] = is_faq_reviewer

# Kategori tetap untuk FAQ — sesuai dokumen "FAQ Fundraising — Golden Future Indonesia"
FAQ_CATEGORIES = [
    "A. Terkait Website & Platform Donasi",
    "B. Cara Handle Donatur",
    "C. Alur Kerja & Koordinasi",
    "D. Struktur & Pembagian Peran",
]

# â”€â”€ Date helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def default_range() -> tuple[str, str]:
    today = date.today()
    d0 = (today - timedelta(days=90)).isoformat()
    d1 = today.isoformat()
    return d0, d1

def parse_range(request: Request) -> tuple[str, str]:
    df = request.query_params.get("from")
    dt = request.query_params.get("to")
    if df and dt:
        return df, dt
    return default_range()

# â”€â”€ Routes: Auth â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse("login.html", {"request": request, "error": error})

@app.post("/login")
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    user = await get_user(username)
    if not user or not check_password(password, user["pw_hash"]):
        return RedirectResponse("/login?error=1", status_code=303)
    token = make_session(username, user["role"])
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", max_age=86400*7)
    return resp

@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp

_KOTAK_TANYA_EMPTY_FORM = {"topik": [], "topik_other": "", "question_raw": "", "saran": ""}

@app.get("/kotak-tanya", response_class=HTMLResponse)
async def kotak_tanya_page(request: Request, sent: str = ""):
    return templates.TemplateResponse("kotak_tanya.html", {
        "request": request, "sent": sent, "error": None,
        "form": dict(_KOTAK_TANYA_EMPTY_FORM), "stage": "form",
    })

@app.post("/kotak-tanya")
async def kotak_tanya_post(
    request: Request,
    topik: list[str] = Form([]),
    topik_other: str = Form(""),
    question_raw: str = Form(...),
    saran: str = Form(""),
    stage: str = Form(""),
):
    form_values = {
        "topik": topik, "topik_other": topik_other,
        "question_raw": question_raw, "saran": saran,
    }

    # stage="edit" → balik ke form editable apa adanya, tanpa validasi ulang
    if stage == "edit":
        return templates.TemplateResponse("kotak_tanya.html", {
            "request": request, "sent": "", "error": None, "form": form_values,
            "stage": "form",
        })

    error = None
    if not question_raw.strip():
        error = "Pertanyaan tidak boleh kosong."
    elif not topik and not topik_other.strip():
        error = "Pilih minimal satu topik, atau isi kolom Other."

    if error:
        return templates.TemplateResponse("kotak_tanya.html", {
            "request": request, "sent": "", "error": error, "form": form_values,
            "stage": "form",
        })

    if stage != "confirmed":
        # Submit pertama & valid — tampilkan tahap review, belum disimpan ke DB
        return templates.TemplateResponse("kotak_tanya.html", {
            "request": request, "sent": "", "error": None, "form": form_values,
            "stage": "review",
        })

    # stage="confirmed" & valid — baru simpan ke DB
    topik_list = list(topik)
    if topik_other.strip():
        topik_list.append(topik_other.strip())
    topik_value = ", ".join(topik_list)

    async with aiosqlite.connect(agg.DB_PATH) as db:
        await db.execute(
            "INSERT INTO faq_submissions (question_raw, topik, saran, status) VALUES (?, ?, ?, ?)",
            (question_raw.strip(), topik_value, saran.strip() or None, "pending")
        )
        await db.commit()
    return RedirectResponse("/kotak-tanya?sent=1", status_code=303)

@app.get("/faq", response_class=HTMLResponse)
async def faq_public_page(request: Request):
    async with aiosqlite.connect(agg.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT category, question_public, answer FROM faq_entries "
            "WHERE status = 'terjawab' ORDER BY category, updated_at"
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        async with db.execute(
            "SELECT question_public, rencana_pembahasan FROM faq_entries "
            "WHERE status = 'diagendakan' ORDER BY updated_at"
        ) as cur:
            diagendakan = [dict(r) for r in await cur.fetchall()]

        async with db.execute(
            "SELECT question_public, ranah_divisi, jalur_disarankan FROM faq_entries "
            "WHERE status = 'luar_lingkup' ORDER BY updated_at"
        ) as cur:
            luar_lingkup = [dict(r) for r in await cur.fetchall()]

        async with db.execute("SELECT MAX(updated_at) AS last_updated FROM faq_entries") as cur:
            last_updated_row = await cur.fetchone()
            last_updated = last_updated_row["last_updated"] if last_updated_row else None

    groups: dict[str, list] = {cat: [] for cat in FAQ_CATEGORIES}
    lainnya = []
    for r in rows:
        cat = r["category"] or ""
        if cat in groups:
            groups[cat].append(r)
        else:
            lainnya.append(r)
    if lainnya:
        groups["Lainnya"] = lainnya

    return templates.TemplateResponse("faq_public.html", {
        "request": request, "groups": groups, "categories": FAQ_CATEGORIES,
        "diagendakan": diagendakan, "luar_lingkup": luar_lingkup,
        "last_updated": last_updated,
    })

# â”€â”€ Routes: Main pages â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    return RedirectResponse("/home")

@app.get("/home", response_class=HTMLResponse)
async def home_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")

    # CRM data (SQLite)
    crm_now  = await hagg.crm_bulan_ini()
    crm_a2a  = await hagg.crm_apple_to_apple()
    programs = await hagg.crm_top_programs(5)
    cs_alert = await hagg.crm_cs_alert()
    new_big  = await hagg.crm_donatur_baru_besar(7, 500_000)

    # Online transaksi (MySQL berdonasi)
    try:
        online_now   = await hagg.online_bulan_ini(bdb)
        stuck        = await hagg.online_stuck_payments(bdb, 3)
        hari_buruk   = await hagg.hari_konversi_buruk(bdb, 7, 50.0)
        online_ok    = True
    except Exception as e:
        log.warning(f"home MySQL error: {e}")
        online_now = {"total":0,"paid":0,"revenue":0,"conv_rate":0}
        stuck = []; hari_buruk = []; online_ok = False

    # Narasi otomatis
    narasi = hagg.generate_narasi(crm_now, crm_a2a, online_now)

    return templates.TemplateResponse("home.html", {
        "request": request, "user": user,
        "crm_now": crm_now, "crm_a2a": crm_a2a,
        "programs": programs, "cs_alert": cs_alert, "new_big": new_big,
        "online_now": online_now, "online_ok": online_ok,
        "stuck": stuck, "hari_buruk": hari_buruk,
        "narasi": narasi,
    })

@app.get("/crm", response_class=HTMLResponse)
async def crm_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    d0, d1 = parse_range(request)
    gran = request.query_params.get("gran", "auto")

    # Filter tahun — ?years=2023,2024,2025 atau kosong = semua
    years_raw = request.query_params.get("years", "")
    years: list[int] | None = None
    if years_raw:
        try:
            years = [int(y.strip()) for y in years_raw.split(",") if y.strip().isdigit()]
        except Exception:
            years = None

    kpi      = await agg.kpi_crm(d0, d1, years)
    tren     = await agg.tren_donasi(d0, d1, gran, years)
    cs_rank  = await agg.ranking_cs(d0, d1, years)
    programs = await agg.ranking_program(d0, d1, years)
    channels = await agg.sumber_traffic(d0, d1, years)
    inst     = await agg.donasi_institusional(d0, d1)
    last_syn = await agg.last_sync_info()
    all_yrs  = await agg.available_years()

    return templates.TemplateResponse("crm.html", {
        "request": request, "user": user,
        "kpi": kpi, "tren": tren, "cs_rank": cs_rank,
        "programs": programs, "channels": channels, "inst": inst,
        "date_from": d0, "date_to": d1, "gran": gran,
        "last_sync": last_syn,
        "all_years": all_yrs,
        "selected_years": years or [],
    })

@app.get("/donor", response_class=HTMLResponse)
async def donor_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    search = request.query_params.get("q", "")
    kpi_d  = await agg.kpi_donatur()
    donors = await agg.tabel_donatur(search=search, limit=100)

    # Load institutional exclusions untuk tampil di halaman ini
    async with aiosqlite.connect(agg.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM institutional_exclusion ORDER BY nominal DESC") as cur:
            exclusions = [dict(r) for r in await cur.fetchall()]

    return templates.TemplateResponse("donor.html", {
        "request": request, "user": user,
        "kpi": kpi_d, "donors": donors, "search": search,
        "exclusions": exclusions,
    })

@app.get("/ads", response_class=HTMLResponse)
async def ads_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    days     = int(request.query_params.get("days", 30))
    # Web tracker analytics
    kpi      = await wa.kpi_web(days)
    tren     = await wa.tren_harian(days)
    sources  = await wa.traffic_sources(days)
    pages    = await wa.top_pages(days)
    camps    = await wa.utm_campaigns(days)
    # Transaksi online dari MySQL berdonasi
    try:
        txn_kpi   = await bdb.kpi_transaksi(days)
        txn_tren  = await bdb.tren_harian(days)
        txn_camps = await bdb.top_campaigns(days)
        txn_conv  = await bdb.konversi_harian(days)
        txn_list  = await bdb.transaksi_terbaru(10)
        txn_ok    = True
    except Exception as e:
        log.warning(f"MySQL berdonasi error: {e}")
        txn_kpi = txn_tren = txn_camps = txn_conv = txn_list = None
        txn_ok = False
    return templates.TemplateResponse("ads.html", {
        "request": request, "user": user, "days": days,
        # web tracker
        "kpi": kpi, "tren": tren, "sources": sources,
        "pages": pages, "camps": camps,
        # transaksi online
        "txn_ok": txn_ok, "txn_kpi": txn_kpi, "txn_tren": txn_tren,
        "txn_camps": txn_camps, "txn_conv": txn_conv, "txn_list": txn_list,
    })

@app.get("/admin/faq/entries", response_class=HTMLResponse)
async def faq_entries_list_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    if not is_faq_reviewer(user["u"]):
        return RedirectResponse("/home")

    async with aiosqlite.connect(agg.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, status, category, question_public, updated_at FROM faq_entries "
            "ORDER BY updated_at DESC"
        ) as cur:
            entries = [dict(r) for r in await cur.fetchall()]

    return templates.TemplateResponse("faq_entries_list.html", {
        "request": request, "user": user, "entries": entries,
    })

def _faq_entry_validate(status: str, category: str, answer: str,
                         rencana_pembahasan: str, ranah_divisi: str, jalur_disarankan: str):
    if status == "terjawab" and not category.strip():
        return "Kategori wajib dipilih untuk status Terjawab."
    if status == "terjawab" and not answer.strip():
        return "Jawaban wajib diisi untuk status Terjawab."
    if status == "diagendakan" and not rencana_pembahasan.strip():
        return "Rencana Pembahasan wajib diisi untuk status Diagendakan."
    if status == "luar_lingkup" and (not ranah_divisi.strip() or not jalur_disarankan.strip()):
        return "Ranah/Divisi dan Jalur Disarankan wajib diisi untuk status Luar Lingkup."
    return None

@app.get("/admin/faq/entries/{entry_id}/edit", response_class=HTMLResponse)
async def faq_entry_edit_page(request: Request, entry_id: int):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    if not is_faq_reviewer(user["u"]):
        return RedirectResponse("/home")

    async with aiosqlite.connect(agg.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT status, category, question_public, answer, rencana_pembahasan, "
            "ranah_divisi, jalur_disarankan FROM faq_entries WHERE id = ?",
            (entry_id,)
        ) as cur:
            row = await cur.fetchone()

    if not row:
        return RedirectResponse("/admin/faq/entries")

    return templates.TemplateResponse("faq_entry_edit.html", {
        "request": request, "user": user, "entry_id": entry_id,
        "form": dict(row), "categories": FAQ_CATEGORIES, "error": None,
    })

@app.post("/admin/faq/entries/{entry_id}/edit")
async def faq_entry_edit_post(
    request: Request,
    entry_id: int,
    status: str = Form(...),
    question_public: str = Form(...),
    category: str = Form(""),
    answer: str = Form(""),
    rencana_pembahasan: str = Form(""),
    ranah_divisi: str = Form(""),
    jalur_disarankan: str = Form(""),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    if not is_faq_reviewer(user["u"]):
        return RedirectResponse("/home")

    form_values = {
        "status": status, "question_public": question_public,
        "category": category, "answer": answer,
        "rencana_pembahasan": rencana_pembahasan,
        "ranah_divisi": ranah_divisi, "jalur_disarankan": jalur_disarankan,
    }

    error = _faq_entry_validate(status, category, answer, rencana_pembahasan, ranah_divisi, jalur_disarankan)
    if error:
        return templates.TemplateResponse("faq_entry_edit.html", {
            "request": request, "user": user, "entry_id": entry_id,
            "form": form_values, "categories": FAQ_CATEGORIES, "error": error,
        })

    reviewer = await get_user(user["u"])
    reviewer_id = reviewer["id"] if reviewer else None

    # Cuma simpan field yang relevan dengan status yang dipilih — sisanya NULL
    if status == "terjawab":
        category_v, answer_v = category.strip() or None, answer.strip() or None
        rencana_v = ranah_v = jalur_v = None
    elif status == "diagendakan":
        rencana_v = rencana_pembahasan.strip() or None
        category_v = answer_v = ranah_v = jalur_v = None
    elif status == "luar_lingkup":
        ranah_v = ranah_divisi.strip() or None
        jalur_v = jalur_disarankan.strip() or None
        category_v = answer_v = rencana_v = None
    else:
        raise HTTPException(status_code=400, detail="Status tidak valid")

    async with aiosqlite.connect(agg.DB_PATH) as db:
        await db.execute("""
            UPDATE faq_entries
            SET status = ?, category = ?, question_public = ?, answer = ?,
                rencana_pembahasan = ?, ranah_divisi = ?, jalur_disarankan = ?,
                updated_by = ?, updated_at = datetime('now')
            WHERE id = ?
        """, (
            status, category_v, question_public.strip(), answer_v, rencana_v,
            ranah_v, jalur_v, reviewer_id, entry_id
        ))
        await db.commit()

    return RedirectResponse("/admin/faq/entries", status_code=303)

@app.get("/admin/faq/entries/{entry_id}/delete", response_class=HTMLResponse)
async def faq_entry_delete_page(request: Request, entry_id: int):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    if not is_faq_reviewer(user["u"]):
        return RedirectResponse("/home")

    async with aiosqlite.connect(agg.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, status, category, question_public, answer, rencana_pembahasan, "
            "ranah_divisi, jalur_disarankan, updated_at FROM faq_entries WHERE id = ?",
            (entry_id,)
        ) as cur:
            row = await cur.fetchone()

    if not row:
        return RedirectResponse("/admin/faq/entries")

    return templates.TemplateResponse("faq_entry_delete.html", {
        "request": request, "user": user, "entry": dict(row), "error": None,
    })

@app.post("/admin/faq/entries/{entry_id}/delete")
async def faq_entry_delete_post(request: Request, entry_id: int, confirm: str = Form("")):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    if not is_faq_reviewer(user["u"]):
        return RedirectResponse("/home")

    async with aiosqlite.connect(agg.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, status, category, question_public, answer, rencana_pembahasan, "
            "ranah_divisi, jalur_disarankan, updated_at, source_submission_id FROM faq_entries WHERE id = ?",
            (entry_id,)
        ) as cur:
            row = await cur.fetchone()

    if not row:
        return RedirectResponse("/admin/faq/entries")

    if not confirm.strip():
        return templates.TemplateResponse("faq_entry_delete.html", {
            "request": request, "user": user, "entry": dict(row),
            "error": "Kamu harus mencentang konfirmasi dulu sebelum menghapus.",
        })

    async with aiosqlite.connect(agg.DB_PATH) as db:
        await db.execute("DELETE FROM faq_entries WHERE id = ?", (entry_id,))
        if row["source_submission_id"]:
            await db.execute(
                "UPDATE faq_submissions SET status = 'pending' WHERE id = ?",
                (row["source_submission_id"],)
            )
        await db.commit()

    return RedirectResponse("/admin/faq/entries", status_code=303)

@app.get("/admin/faq", response_class=HTMLResponse)
async def faq_review_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    if not is_faq_reviewer(user["u"]):
        return RedirectResponse("/home")

    async with aiosqlite.connect(agg.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, question_raw, submitted_at FROM faq_submissions "
            "WHERE status = 'pending' ORDER BY id"
        ) as cur:
            submissions = [dict(r) for r in await cur.fetchall()]

    return templates.TemplateResponse("faq_review.html", {
        "request": request, "user": user, "submissions": submissions,
    })

@app.get("/admin/faq/{submission_id}", response_class=HTMLResponse)
async def faq_review_detail_page(request: Request, submission_id: int):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    if not is_faq_reviewer(user["u"]):
        return RedirectResponse("/home")

    async with aiosqlite.connect(agg.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, question_raw, topik, saran, status, submitted_at FROM faq_submissions WHERE id = ?",
            (submission_id,)
        ) as cur:
            row = await cur.fetchone()

    if not row or row["status"] == "processed":
        return RedirectResponse("/admin/faq")

    return templates.TemplateResponse("faq_review_detail.html", {
        "request": request, "user": user, "submission": dict(row), "form": None,
        "categories": FAQ_CATEGORIES,
    })

@app.post("/admin/faq/{submission_id}")
async def faq_review_detail_post(
    request: Request,
    submission_id: int,
    status: str = Form(...),
    question_public: str = Form(...),
    category: str = Form(""),
    answer: str = Form(""),
    rencana_pembahasan: str = Form(""),
    ranah_divisi: str = Form(""),
    jalur_disarankan: str = Form(""),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    if not is_faq_reviewer(user["u"]):
        return RedirectResponse("/home")

    form_values = {
        "status": status, "question_public": question_public,
        "category": category, "answer": answer,
        "rencana_pembahasan": rencana_pembahasan,
        "ranah_divisi": ranah_divisi, "jalur_disarankan": jalur_disarankan,
    }

    error = None
    if status == "terjawab" and not category.strip():
        error = "Kategori wajib dipilih untuk status Terjawab."
    elif status == "terjawab" and not answer.strip():
        error = "Jawaban wajib diisi untuk status Terjawab."
    elif status == "diagendakan" and not rencana_pembahasan.strip():
        error = "Rencana Pembahasan wajib diisi untuk status Diagendakan."
    elif status == "luar_lingkup" and (not ranah_divisi.strip() or not jalur_disarankan.strip()):
        error = "Ranah/Divisi dan Jalur Disarankan wajib diisi untuk status Luar Lingkup."

    if error:
        async with aiosqlite.connect(agg.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, question_raw, topik, saran, status, submitted_at FROM faq_submissions WHERE id = ?",
                (submission_id,)
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return RedirectResponse("/admin/faq")
        return templates.TemplateResponse("faq_review_detail.html", {
            "request": request, "user": user, "submission": dict(row),
            "error": error, "form": form_values, "categories": FAQ_CATEGORIES,
        })

    reviewer = await get_user(user["u"])
    reviewer_id = reviewer["id"] if reviewer else None

    # Cuma simpan field yang relevan dengan status yang dipilih — sisanya NULL
    if status == "terjawab":
        category_v, answer_v = category.strip() or None, answer.strip() or None
        rencana_v = ranah_v = jalur_v = None
    elif status == "diagendakan":
        rencana_v = rencana_pembahasan.strip() or None
        category_v = answer_v = ranah_v = jalur_v = None
    elif status == "luar_lingkup":
        ranah_v = ranah_divisi.strip() or None
        jalur_v = jalur_disarankan.strip() or None
        category_v = answer_v = rencana_v = None
    else:
        raise HTTPException(status_code=400, detail="Status tidak valid")

    async with aiosqlite.connect(agg.DB_PATH) as db:
        await db.execute("""
            INSERT INTO faq_entries
                (status, category, question_public, answer, rencana_pembahasan,
                 ranah_divisi, jalur_disarankan, source_submission_id, updated_by)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            status, category_v, question_public.strip(), answer_v, rencana_v,
            ranah_v, jalur_v, submission_id, reviewer_id
        ))
        await db.execute(
            "UPDATE faq_submissions SET status = 'processed' WHERE id = ?",
            (submission_id,)
        )
        await db.commit()

    return RedirectResponse("/admin/faq", status_code=303)

# ── API: web analytics tracker ────────────────────────────────────────────────────────────────

ALLOWED_ORIGINS = {
    "https://goldenfutureindonesia.org",
    "https://berdonasi.goldenfutureindonesia.org",
    "http://localhost", "http://127.0.0.1",
}

@app.options("/api/track")
async def track_preflight(request: Request):
    """CORS preflight untuk tracker.js"""
    origin = request.headers.get("origin", "")
    headers = {
        "Access-Control-Allow-Origin":  origin if origin in ALLOWED_ORIGINS else "",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age":       "86400",
    }
    return JSONResponse(None, headers=headers)

@app.post("/api/track")
async def api_track(request: Request):
    """Terima pageview/event dari tracker.js — zero-auth, CORS open."""
    origin = request.headers.get("origin", "")
    cors_origin = origin if origin in ALLOWED_ORIGINS else "*"
    try:
        data = await request.json()
        ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "")
        await wa.insert_event(data, ip=ip.split(",")[0].strip())
        return JSONResponse({"ok": True}, headers={"Access-Control-Allow-Origin": cors_origin})
    except Exception as e:
        log.warning(f"tracker error: {e}")
        return JSONResponse({"ok": False}, status_code=400, headers={"Access-Control-Allow-Origin": cors_origin})

# ── API endpoints (JSON, untuk chart update & modal) ─────────────────────────

@app.get("/api/cs-detail")
async def api_cs_detail(request: Request, cs: str, date_from: str = "", date_to: str = ""):
    if not get_current_user(request):
        raise HTTPException(401)
    d0 = date_from or default_range()[0]
    d1 = date_to   or default_range()[1]
    return await agg.detail_cs(cs, d0, d1)

@app.get("/api/donor-detail")
async def api_donor_detail(request: Request, phone: str):
    if not get_current_user(request):
        raise HTTPException(401)
    return await agg.detail_donatur(phone)

@app.get("/api/tren")
async def api_tren(request: Request, date_from: str = "", date_to: str = "", gran: str = "auto"):
    if not get_current_user(request):
        raise HTTPException(401)
    d0 = date_from or default_range()[0]
    d1 = date_to   or default_range()[1]
    return await agg.tren_donasi(d0, d1, gran)

@app.post("/api/sync")
async def api_sync(request: Request):
    """Manual trigger sync (admin only)."""
    user = get_current_user(request)
    if not user or user.get("r") != "admin":
        raise HTTPException(403)
    result = await sync_from_sheets()
    return {"ok": True, "result": result}

@app.get("/api/sync-info")
async def api_sync_info(request: Request):
    """Info sync terakhir â€” untuk polling badge tanpa reload halaman."""
    if not get_current_user(request):
        raise HTTPException(401)
    return await agg.last_sync_info()

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = get_current_user(request)
    if not user or user.get("r") != "admin":
        return RedirectResponse("/")

    async with aiosqlite.connect(agg.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM sheet_sources ORDER BY id") as cur:
            sources = [dict(r) for r in await cur.fetchall()]
        async with db.execute("SELECT * FROM institutional_exclusion ORDER BY tanggal DESC") as cur:
            exclusions = [dict(r) for r in await cur.fetchall()]
        async with db.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 5") as cur:
            sync_logs = [dict(r) for r in await cur.fetchall()]
        async with db.execute("SELECT COUNT(*) AS cnt FROM donations") as cur:
            total_rows = (await cur.fetchone())["cnt"]

    return templates.TemplateResponse("settings.html", {
        "request": request, "user": user,
        "sources": sources, "exclusions": exclusions,
        "sync_logs": sync_logs, "total_rows": total_rows,
    })

# â”€â”€ CRUD Sumber Data â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.post("/settings/sources/add")
async def source_add(request: Request,
                     label: str = Form(...),
                     spreadsheet_id: str = Form(...),
                     sheet_name: str = Form("rekap seluruh cs")):
    user = get_current_user(request)
    if not user or user.get("r") != "admin":
        raise HTTPException(403)
    # Validasi spreadsheet_id â€” ekstrak dari URL jika penuh
    import re
    m = re.search(r"/spreadsheets/d/([^/]+)", spreadsheet_id)
    if m:
        spreadsheet_id = m.group(1)
    spreadsheet_id = spreadsheet_id.strip()
    async with aiosqlite.connect(agg.DB_PATH) as db:
        await db.execute(
            "INSERT INTO sheet_sources (label, spreadsheet_id, sheet_name) VALUES (?,?,?)",
            (label.strip(), spreadsheet_id, sheet_name.strip())
        )
        await db.commit()
    return RedirectResponse("/settings?ok=source_added", status_code=303)

@app.post("/settings/sources/toggle")
async def source_toggle(request: Request, source_id: int = Form(...)):
    user = get_current_user(request)
    if not user or user.get("r") != "admin":
        raise HTTPException(403)
    async with aiosqlite.connect(agg.DB_PATH) as db:
        await db.execute(
            "UPDATE sheet_sources SET is_active = CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id=?",
            (source_id,)
        )
        await db.commit()
    return RedirectResponse("/settings?ok=toggled", status_code=303)

@app.post("/settings/sources/delete")
async def source_delete(request: Request, source_id: int = Form(...)):
    user = get_current_user(request)
    if not user or user.get("r") != "admin":
        raise HTTPException(403)
    async with aiosqlite.connect(agg.DB_PATH) as db:
        await db.execute("DELETE FROM sheet_sources WHERE id=?", (source_id,))
        await db.commit()
    return RedirectResponse("/settings?ok=deleted", status_code=303)

# â”€â”€ CRUD Donasi Institusional â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.post("/settings/exclusions/add")
async def exclusion_add(request: Request,
                        donor_name: str = Form(...),
                        tanggal: str = Form(...),
                        nominal: int = Form(...),
                        note: str = Form("")):
    user = get_current_user(request)
    if not user or user.get("r") != "admin":
        raise HTTPException(403)
    async with aiosqlite.connect(agg.DB_PATH) as db:
        await db.execute(
            "INSERT INTO institutional_exclusion (donor_name, tanggal, nominal, note) VALUES (?,?,?,?)",
            (donor_name.strip(), tanggal, nominal, note.strip())
        )
        await db.commit()
    # Re-mark existing donations
    async with aiosqlite.connect(agg.DB_PATH) as db:
        await db.execute("""
            UPDATE donations SET is_institusional = 1
            WHERE donor_name LIKE ? AND tanggal = ? AND nominal = ?
        """, (f"%{donor_name.strip()}%", tanggal, nominal))
        await db.commit()
    return RedirectResponse("/settings?ok=exclusion_added", status_code=303)

@app.post("/settings/exclusions/delete")
async def exclusion_delete(request: Request, exclusion_id: int = Form(...)):
    user = get_current_user(request)
    if not user or user.get("r") != "admin":
        raise HTTPException(403)
    async with aiosqlite.connect(agg.DB_PATH) as db:
        await db.execute("DELETE FROM institutional_exclusion WHERE id=?", (exclusion_id,))
        await db.commit()
    return RedirectResponse("/settings?ok=exclusion_deleted", status_code=303)


# ── WhatsApp Bot routes ────────────────────────────────────────────────────────

@app.post("/webhook/replai")
async def webhook_replai(request: Request):
    """Webhook dari Replai.id — dipanggil saat ada pesan masuk."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    log.info(f"Replai webhook: {payload}")
    import asyncio
    async def _process():
        reply_text = await wa_webhook.handle_webhook(payload, agg, wa_bot, hagg, bdb)
        if reply_text:
            sender   = payload.get("from", "")
            is_group = payload.get("type") == "group"
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, lambda: wa_bot.reply_message(sender, reply_text, is_group=is_group)
            )
            await _log_wa_broadcast(sender, reply_text,
                                    "ok" if result.get("ok") else "error",
                                    result.get("error", ""), trigger="webhook")
    asyncio.create_task(_process())
    return JSONResponse({"ok": True})


@app.post("/api/wa/test-send")
async def api_wa_test_send(request: Request):
    """Kirim test message manual dari admin."""
    user = get_current_user(request)
    if not user or user.get("r") != "admin":
        raise HTTPException(403)
    body    = await request.json()
    phone   = body.get("phone", "").strip()
    text    = body.get("text", "Halo dari Fundraising GFI! 👋").strip()
    is_grp  = body.get("is_group", False)
    if not phone:
        return JSONResponse({"ok": False, "error": "phone diperlukan"}, status_code=400)
    import asyncio
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: wa_bot.send_message(phone, text, is_group=is_grp))
    await _log_wa_broadcast(phone, text,
                            "ok" if result.get("ok") else "error",
                            result.get("error", ""), trigger="manual_test")
    return JSONResponse(result)


@app.post("/api/wa/send-report-now")
async def api_wa_send_report_now(request: Request):
    """Trigger laporan WA sekarang (test)."""
    user = get_current_user(request)
    if not user or user.get("r") != "admin":
        raise HTTPException(403)
    try:
        await _send_wa_report()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/wa/device-status")
async def api_wa_device_status(request: Request):
    if not get_current_user(request):
        raise HTTPException(401)
    import asyncio
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, wa_bot.check_device)
    return JSONResponse(result)


@app.get("/api/wa/groups")
async def api_wa_groups(request: Request):
    user = get_current_user(request)
    if not user or user.get("r") != "admin":
        raise HTTPException(403)
    import asyncio
    loop = asyncio.get_event_loop()
    groups = await loop.run_in_executor(None, wa_bot.get_group_list)
    return JSONResponse({"ok": True, "groups": groups})


@app.get("/api/wa/logs")
async def api_wa_logs(request: Request, limit: int = 20):
    if not get_current_user(request):
        raise HTTPException(401)
    async with aiosqlite.connect(agg.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM wa_broadcast_log ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            logs = [dict(r) for r in await cur.fetchall()]
    return JSONResponse(logs)


@app.post("/api/wa/config")
async def api_wa_config_save(request: Request):
    """Simpan konfigurasi WA Bot ke .env dan reschedule."""
    user = get_current_user(request)
    if not user or user.get("r") != "admin":
        raise HTTPException(403)
    body     = await request.json()
    group_id = body.get("group_id", "").strip()
    wa_hour  = int(body.get("hour", 7))
    env_path = BASE_DIR / ".env"
    lines    = env_path.read_text().splitlines() if env_path.exists() else []
    new_lines = [l for l in lines
                 if not l.startswith("WA_GROUP_ID=") and not l.startswith("WA_REPORT_HOUR=")]
    if group_id:
        new_lines.append(f"WA_GROUP_ID={group_id}")
    new_lines.append(f"WA_REPORT_HOUR={wa_hour}")
    env_path.write_text("\n".join(new_lines) + "\n")
    os.environ["WA_GROUP_ID"]    = group_id
    os.environ["WA_REPORT_HOUR"] = str(wa_hour)
    try:
        scheduler.remove_job("wa_daily_report")
    except Exception:
        pass
    scheduler.add_job(_send_wa_report, "cron", hour=wa_hour, minute=0,
                      id="wa_daily_report", misfire_grace_time=3600)
    return JSONResponse({"ok": True})


# Jalankan sekali: python -c "import asyncio; from main import seed_user; asyncio.run(seed_user())"

async def seed_user(username="admin", password="GANTI_INI_SEBELUM_PAKAI", role="admin"):
    await init_db()
    try:
        await create_user(username, password, role)
        print(f"User '{username}' berhasil dibuat (role={role})")
    except Exception as e:
        print(f"Gagal: {e}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 5055))
    uvicorn.run("main:app", host="127.0.0.1", port=port, reload=False)
