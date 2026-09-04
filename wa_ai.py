"""
wa_ai.py — Gemini AI Agent untuk WA Bot Fundraising GFI

Alur kerja:
1. Ambil snapshot data real dari dashboard (CRM, ranking CS, program, dll)
2. Kirim ke Gemini 1.5 Flash dengan system prompt berisi data
3. Gemini jawab pertanyaan user secara natural berdasarkan data nyata

Credentials: GOOGLE_API_KEY sudah ada di .env (dipakai juga untuk Sheets)
"""
import os
import json
import logging
import requests
from datetime import date

log = logging.getLogger("wa_ai")

# Pakai GEMINI_API_KEY (dari Google AI Studio: aistudio.google.com/app/apikey)
# Fallback ke GOOGLE_API_KEY kalau belum diset terpisah
GOOGLE_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# Model list — primary dulu, fallback kalau 503
_MODELS = [
    "gemini-3.1-flash-lite",      # ✅ jalan, gratis
    "gemini-flash-lite-latest",   # ✅ fallback gratis
    "gemini-flash-latest",        # kadang 503 tapi lebih kuat
]

_SYSTEM_TEMPLATE = """\
Kamu adalah asisten data internal tim fundraising Golden Future Indonesia (GFI). \
Nama kamu bisa dipanggil "GFI Bot" atau sekedar dibalas santai.

PERSONA KAMU:
- Kayak teman satu tim yang kebetulan punya akses ke semua data fundraising
- Santai, casual, tapi tetap profesional kalau konteksnya serius
- Bisa pakai kata-kata gaul Indonesia yang wajar: "nih", "yuk", "sih", "dong", "nah", dll
- Kalau ada insight bagus, kasih tau dengan antusias. Kalau ada masalah, bilang terus terang
- TIDAK kaku seperti laporan formal. Jawab seperti kamu lagi ngobrol di WA tim

CARA MENJAWAB:
- Jawab LANGSUNG ke intinya, jangan basa-basi panjang
- Pakai angka dari data, tapi INTERPRETASIKAN juga (naik/turun? bagus/perlu perhatian?)
- Max 6-8 baris buat pesan WA — kalau lebih panjang, ringkas
- Emoji boleh, tapi jangan kebanyakan (2-4 per pesan cukup)
- Format WA: pakai *bold* untuk angka penting, _italic_ untuk catatan
- Kalau ditanya sesuatu yang datanya ada, langsung jawab berdasarkan data
- Kalau datanya nggak ada, jujur aja: "data ini belum ke-track di dashboard kita"
- Boleh kasih rekomendasi / opini berdasarkan data

JANGAN:
- Jangan jawab hal di luar topik GFI/fundraising/donasi
- Jangan pake format markdown (##, **, ---) — ini WA bukan dokumen
- Jangan terlalu formal atau kaku
- Jangan sebut "Berdasarkan data yang diberikan..." — langsung jawab aja

KONTEKS BISNIS GFI:
- GFI adalah organisasi donasi/fundraising Islam
- Ada tim CS (Customer Service) yang handle donor via WA
- Ada berbagai program donasi (kode program)
- Target bulanan per CS dan per program

DATA DASHBOARD REAL-TIME ({tanggal}):
{data}
"""


async def _get_dashboard_snapshot(aggregates, home_agg, berdonasi_db=None) -> dict:
    """
    Kumpulkan snapshot data dashboard untuk context Gemini.
    Mencakup berbagai window waktu agar bisa jawab pertanyaan temporal.
    """
    from datetime import timedelta
    today = date.today()
    d0_bln  = today.replace(day=1).isoformat()
    d1      = today.isoformat()
    d7      = (today - timedelta(days=6)).isoformat()   # 7 hari terakhir
    d3      = (today - timedelta(days=2)).isoformat()   # 3 hari terakhir
    d_lbln  = (today.replace(day=1) - timedelta(days=1)).replace(day=1).isoformat()  # awal bulan lalu
    d_lbln_end = (today.replace(day=1) - timedelta(days=1)).isoformat()             # akhir bulan lalu
    d_3bln  = (today - timedelta(days=90)).isoformat()                              # 90 hari terakhir (3 bulan)

    snap = {
        "tanggal_hari_ini": today.isoformat(),
        "bulan": today.strftime("%B %Y"),
    }

    # ── Donasi hari ini ──────────────────────────────────────────────────────
    try:
        kpi_today = await aggregates.kpi_crm(d1, d1)
        snap["donasi_hari_ini"] = {
            "total_rp": kpi_today.get("total", 0),
            "jumlah_transaksi": kpi_today.get("jumlah", 0),
        }
    except Exception as e:
        log.debug(f"kpi_today: {e}")

    # ── 3 hari terakhir (per hari) ───────────────────────────────────────────
    try:
        tren3 = await aggregates.tren_donasi(d3, d1, granularity="day")
        snap["3_hari_terakhir"] = [
            {"tanggal": r["label"], "total_rp": r["total"], "jumlah": r["jumlah"]}
            for r in tren3
        ]
    except Exception as e:
        log.debug(f"tren3: {e}")

    # ── 7 hari terakhir (per hari) ───────────────────────────────────────────
    try:
        tren7 = await aggregates.tren_donasi(d7, d1, granularity="day")
        snap["7_hari_terakhir"] = [
            {"tanggal": r["label"], "total_rp": r["total"], "jumlah": r["jumlah"]}
            for r in tren7
        ]
    except Exception as e:
        log.debug(f"tren7: {e}")

    # ── Bulan ini (aggregat) ─────────────────────────────────────────────────
    try:
        crm = await home_agg.crm_bulan_ini()
        snap["crm_bulan_ini"] = {
            "total_donasi_rp": crm.get("total", 0),
            "jumlah_transaksi": crm.get("jumlah", 0),
        }
    except Exception as e:
        log.debug(f"crm_bulan_ini: {e}")

    # ── Bulan lalu (untuk perbandingan) ─────────────────────────────────────
    try:
        kpi_lbln = await aggregates.kpi_crm(d_lbln, d_lbln_end)
        snap["crm_bulan_lalu"] = {
            "total_donasi_rp": kpi_lbln.get("total", 0),
            "jumlah_transaksi": kpi_lbln.get("jumlah", 0),
        }
    except Exception as e:
        log.debug(f"kpi_lbln: {e}")

    # ── 3 Bulan terakhir (per bulan) ─────────────────────────────────────────
    try:
        tren3bln = await aggregates.tren_donasi(d_3bln, d1, granularity="month")
        snap["3_bulan_terakhir"] = [
            {"bulan": r["label"], "total_rp": r["total"], "jumlah_transaksi": r["jumlah"]}
            for r in tren3bln
        ]
    except Exception as e:
        log.debug(f"tren3bln: {e}")

    # ── Ranking CS bulan ini (top 5) ─────────────────────────────────────────
    try:
        cs_list = await aggregates.ranking_cs(d0_bln, d1)
        snap["ranking_cs_bulan_ini"] = [
            {
                "cs": x["cs"],
                "total_rp": x["total"],
                "jumlah_transaksi": x["jumlah"],
                "program_favorit": x.get("program_favorit", "-"),
            }
            for x in cs_list[:5]
        ]
    except Exception as e:
        log.debug(f"ranking_cs: {e}")

    # ── Ranking CS 3 hari terakhir ───────────────────────────────────────────
    try:
        cs_3d = await aggregates.ranking_cs(d3, d1)
        snap["ranking_cs_3_hari"] = [
            {"cs": x["cs"], "total_rp": x["total"], "jumlah": x["jumlah"]}
            for x in cs_3d[:5]
        ]
    except Exception as e:
        log.debug(f"cs_3d: {e}")

    # ── Ranking program bulan ini (top 15) ────────────────────────────────────
    try:
        prog_list = await aggregates.ranking_program(d0_bln, d1)
        snap["ranking_program_bulan_ini"] = [
            {"program": x["program"], "total_rp": x["total"], "jumlah_transaksi": x["jumlah"]}
            for x in prog_list[:15]
        ]
    except Exception as e:
        log.debug(f"ranking_program: {e}")

    # ── Ranking program bulan lalu (top 15) ──────────────────────────────────
    try:
        prog_lbln = await aggregates.ranking_program(d_lbln, d_lbln_end)
        snap["ranking_program_bulan_lalu"] = [
            {"program": x["program"], "total_rp": x["total"], "jumlah_transaksi": x["jumlah"]}
            for x in prog_lbln[:15]
        ]
    except Exception as e:
        log.debug(f"ranking_program_lbln: {e}")

    # ── Ranking program 3 bulan terakhir (top 15) ────────────────────────────
    try:
        prog_3bln = await aggregates.ranking_program(d_3bln, d1)
        snap["ranking_program_3_bulan_terakhir"] = [
            {"program": x["program"], "total_rp": x["total"], "jumlah_transaksi": x["jumlah"]}
            for x in prog_3bln[:15]
        ]
    except Exception as e:
        log.debug(f"ranking_program_3bln: {e}")

    # ── Sumber channel donasi ────────────────────────────────────────────────
    try:
        channels = await aggregates.sumber_traffic(d0_bln, d1)
        snap["channel_donasi_bulan_ini"] = [
            {"channel": x["channel"], "total_rp": x["total"], "jumlah": x["jumlah"]}
            for x in channels[:5]
        ]
    except Exception as e:
        log.debug(f"sumber_traffic: {e}")

    # ── CS alert (performa turun) ────────────────────────────────────────────
    try:
        alerts = await home_agg.crm_cs_alert()
        snap["cs_perlu_perhatian"] = alerts[:3] if alerts else []
    except Exception as e:
        log.debug(f"cs_alert: {e}")

    # ── Rekapitulasi Tahunan (Histori sejak 2023 dll) ────────────────────────
    try:
        years = await aggregates.available_years()
        if years:
            snap["rekap_tahunan"] = {}
            for y in years:
                kpi_yr = await aggregates.kpi_crm("", "", years=[y])
                prog_yr = await aggregates.ranking_program("", "", years=[y])
                snap["rekap_tahunan"][str(y)] = {
                    "total_donasi_rp": kpi_yr.get("total", 0),
                    "jumlah_transaksi": kpi_yr.get("jumlah", 0),
                    "top_program": [
                        {"program": p["program"], "total_rp": p["total"]}
                        for p in prog_yr[:5]
                    ]
                }
    except Exception as e:
        log.debug(f"rekap_tahunan: {e}")

    # ── Donatur baru besar ───────────────────────────────────────────────────
    try:
        new_big = await home_agg.crm_donatur_baru_besar()
        snap["donatur_besar_baru"] = [
            {"nama": d.get("donor_name", "-"), "nominal_rp": d.get("nominal", 0)}
            for d in new_big[:3]
        ]
    except Exception as e:
        log.debug(f"donatur_baru_besar: {e}")

    # ── Transaksi online (MySQL) — opsional ──────────────────────────────────
    if berdonasi_db:
        try:
            online = await home_agg.online_bulan_ini(berdonasi_db)
            snap["transaksi_online_bulan_ini"] = {
                "revenue_rp": online.get("revenue", 0),
                "konversi_persen": online.get("conv_rate", 0),
                "pending": online.get("pending", 0),
            }
        except Exception as e:
            log.debug(f"online_bulan_ini: {e}")

    # ── Donasi terbesar bulan ini (per transaksi) ─────────────────────────────
    try:
        top_trx = await aggregates._fetch(f"""
            SELECT donor_name, nominal, tanggal, kode_program, cs
            FROM donations
            WHERE tanggal BETWEEN ? AND ? AND is_institusional = 0
            ORDER BY nominal DESC LIMIT 5
        """, (d0_bln, d1))
        snap["donasi_terbesar_bulan_ini"] = [
            {"donor": r["donor_name"], "nominal_rp": r["nominal"],
             "tanggal": r["tanggal"], "program": r["kode_program"]}
            for r in top_trx
        ]
    except Exception as e:
        log.debug(f"top_trx: {e}")

    # ── Donasi terbesar bulan lalu (per transaksi) ────────────────────────────
    try:
        top_trx_lbln = await aggregates._fetch(f"""
            SELECT donor_name, nominal, tanggal, kode_program, cs
            FROM donations
            WHERE tanggal BETWEEN ? AND ? AND is_institusional = 0
            ORDER BY nominal DESC LIMIT 5
        """, (d_lbln, d_lbln_end))
        snap["donasi_terbesar_bulan_lalu"] = [
            {"donor": r["donor_name"], "nominal_rp": r["nominal"],
             "tanggal": r["tanggal"], "program": r["kode_program"]}
            for r in top_trx_lbln
        ]
    except Exception as e:
        log.debug(f"top_trx_lbln: {e}")

    # ── Top 5 donatur bulan ini (by total cumulative) ─────────────────────────
    try:
        top_donor = await aggregates._fetch(f"""
            SELECT donor_name, SUM(nominal) AS total, COUNT(*) AS jumlah
            FROM donations
            WHERE tanggal BETWEEN ? AND ? AND is_institusional = 0
              AND donor_name != ''
            GROUP BY donor_name ORDER BY total DESC LIMIT 5
        """, (d0_bln, d1))
        snap["top_donor_bulan_ini"] = [
            {"nama": r["donor_name"], "total_rp": r["total"], "jumlah_donasi": r["jumlah"]}
            for r in top_donor
        ]
    except Exception as e:
        log.debug(f"top_donor: {e}")

    return snap


def _call_gemini(question: str, snap: dict) -> str:
    """
    Kirim pertanyaan + snapshot data ke Gemini.
    Coba model satu per satu dari _MODELS list.
    Retry 2x per model kalau dapat 503 (free tier overloaded).
    """
    if not GOOGLE_API_KEY:
        return "❌ Konfigurasi AI belum selesai (GEMINI_API_KEY tidak ditemukan)."

    data_json = json.dumps(snap, ensure_ascii=False, indent=2)
    system_prompt = _SYSTEM_TEMPLATE.format(
        tanggal=snap.get("tanggal_hari_ini", "hari ini"),
        data=data_json,
    )

    payload = {
        "system_instruction": {
            "parts": [{"text": system_prompt}]
        },
        "contents": [
            {"role": "user", "parts": [{"text": question}]}
        ],
        "generationConfig": {
            "maxOutputTokens": 400,
            "temperature":     0.75,
            "topP":            0.90,
            "topK":            40,
        },
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT",        "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH",       "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ],
    }

    last_error = None
    for model in _MODELS:
        url = f"{_BASE_URL}/{model}:generateContent?key={GOOGLE_API_KEY}"
        for attempt in range(2):  # retry 1x per model kalau 503
            try:
                r = requests.post(url, json=payload, timeout=15)
                if r.status_code == 503:
                    log.warning(f"Gemini 503 ({model} attempt {attempt+1}), retry...")
                    import time; time.sleep(1.5)
                    continue
                r.raise_for_status()
                result = r.json()
                text   = result["candidates"][0]["content"]["parts"][0]["text"]
                log.info(f"Gemini OK via {model}")
                return text.strip()
            except requests.exceptions.Timeout:
                log.warning(f"Gemini timeout ({model}), trying next model")
                last_error = "timeout"
                break  # timeout → langsung coba model berikutnya
            except Exception as e:
                last_error = str(e)
                log.warning(f"Gemini error ({model}): {e}")
                break

    raise RuntimeError(f"Semua model Gemini gagal. Last error: {last_error}")


async def answer(question: str, aggregates, home_agg, berdonasi_db=None) -> str:
    """
    Entry point utama — dipanggil dari wa_webhook.py.
    Async: ambil data dulu (IO), lalu call Gemini di executor (CPU/network).
    """
    import asyncio

    try:
        snap = await _get_dashboard_snapshot(aggregates, home_agg, berdonasi_db)
    except Exception as e:
        log.error(f"Gagal ambil snapshot: {e}")
        snap = {}

    try:
        loop = asyncio.get_event_loop()
        reply = await loop.run_in_executor(
            None, lambda: _call_gemini(question, snap)
        )
        return reply
    except requests.exceptions.Timeout:
        return "⏳ Maaf, AI sedang sibuk. Coba lagi sebentar."
    except Exception as e:
        log.error(f"Gemini error: {e}", exc_info=True)
        return "Maaf, terjadi kesalahan saat memproses pertanyaan. 🙏"
