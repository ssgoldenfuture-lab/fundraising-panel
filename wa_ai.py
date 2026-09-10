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
import knowledge          # Self-learning knowledge base
import calendar_gfi      # Google Calendar + content performance
import program_analytics # DB-based program analysis (all time)

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

ATURAN DATA PENTING — WAJIB DIIKUTI:
- Data snapshot di bawah adalah SUMBER KEBENARAN. Jangan bilang "data tidak ada" kalau data itu JELAS ADA di snapshot
- Cek field: rekap_tahunan, tren_per_bulan_per_tahun, ranking_cs_3_bulan_terakhir, detail_cs_spesifik, dll
- Kalau ada nama CS di pertanyaan, PASTI cek field detail_cs_spesifik dan ranking_cs_* dulu
- Untuk pertanyaan bulan YoY (year-on-year), gunakan field tren_per_bulan_per_tahun
- JANGAN pernah sebut data tidak tersedia kalau kamu belum cek SEMUA field di snapshot
- Kalau memang benar-benar tidak ada, baru bilang tidak ada

KALENDER KONTEN & ANALISIS BLAST:
- Field konten_hari_ini: konten yang diblast CS hari ini
- Field konten_mendatang: jadwal konten 7 hari ke depan
- Field ranking_konten_terbaik: ranking program berdasarkan kalender (kalau ada data kalender)
- Kalau ditanya "besok blast apa?" → cek konten_mendatang
- Window analisis = dari tanggal blast sampai konten berikutnya (otomatis)

ANALISIS PROGRAM DARI DATABASE (SELALU TERSEDIA, DATA 2023 - SEKARANG):
- Field analisis_program_db.ringkasan: satu baris per program, total & jumlah trx
- Field analisis_program_db.top_by_efisiensi_bulanan: top 10 program paling konsisten (avg per bulan)
  - avg_per_bulan = rata-rata donasi per bulan aktif (BUKAN per blast)
  - aktif_bulan = berapa bulan program ini dapat donasi
  - bulan_terbaik = bulan dengan donasi tertinggi untuk program ini
- Field analisis_program_db.top_per_tahun: program terbaik per tahun (2023, 2024, 2025, 2026)
- Kalau ditanya "program mana paling efektif?" → WAJIB cek analisis_program_db dulu!
- Kalau ditanya "Gaza vs Yatim mana yang lebih bagus?" → bandingkan dari analisis_program_db
- Kalau ditanya "trend program X?" → cek tren_6bln di top_by_efisiensi_bulanan
- PENTING: avg_per_bulan berbeda dari "efektivitas per blast" — ini lebih akurat untuk perbandingan jangka panjang

JANGAN:
- Jangan jawab hal di luar topik GFI/fundraising/donasi
- Jangan pake format markdown (##, **, ---) — ini WA bukan dokumen
- Jangan terlalu formal atau kaku
- Jangan sebut "Berdasarkan data yang diberikan..." — langsung jawab aja
- JANGAN bilang data tidak ada sebelum cek semua field snapshot

KONTEKS BISNIS GFI:
- GFI adalah organisasi donasi/fundraising Islam
- Ada tim CS (Customer Service) yang handle donor via WA
- CS melakukan blasting konten sesuai jadwal di Google Calendar
- Hasil blasting bisa dilihat dari donasi yang masuk H+0 sampai konten berikutnya
- Ada berbagai program donasi (kode program) yang bisa di-track per konten

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

    # ── Ranking CS bulan ini (SEMUA CS, bukan hanya top-5) ──────────────────
    try:
        cs_list = await aggregates.ranking_cs(d0_bln, d1)
        snap["ranking_cs_bulan_ini"] = [
            {
                "cs": x["cs"],
                "total_rp": x["total"],
                "jumlah_transaksi": x["jumlah"],
                "program_favorit": x.get("program_favorit", "-"),
            }
            for x in cs_list[:30]  # semua CS, max 30
        ]
        snap["daftar_semua_cs"] = [x["cs"] for x in cs_list]  # daftar nama lengkap
    except Exception as e:
        log.debug(f"ranking_cs: {e}")

    # ── Ranking CS 3 hari terakhir (semua) ───────────────────────────────
    try:
        cs_3d = await aggregates.ranking_cs(d3, d1)
        snap["ranking_cs_3_hari"] = [
            {"cs": x["cs"], "total_rp": x["total"], "jumlah": x["jumlah"]}
            for x in cs_3d[:30]
        ]
    except Exception as e:
        log.debug(f"cs_3d: {e}")

    # ── Ranking CS 3 bulan terakhir (SEMUA) ────────────────────────────
    try:
        cs_3bln = await aggregates.ranking_cs(d_3bln, d1)
        snap["ranking_cs_3_bulan_terakhir"] = [
            {
                "cs": x["cs"],
                "total_rp": x["total"],
                "jumlah_transaksi": x["jumlah"],
                "program_favorit": x.get("program_favorit", "-"),
            }
            for x in cs_3bln[:30]
        ]
    except Exception as e:
        log.debug(f"cs_3bln: {e}")

    # ── Ranking CS bulan lalu (semua) ────────────────────────────────
    try:
        cs_lbln = await aggregates.ranking_cs(d_lbln, d_lbln_end)
        snap["ranking_cs_bulan_lalu"] = [
            {"cs": x["cs"], "total_rp": x["total"], "jumlah_transaksi": x["jumlah"]}
            for x in cs_lbln[:30]
        ]
    except Exception as e:
        log.debug(f"cs_lbln: {e}")

    # ── Ranking program bulan ini (top 15) ───────────────────────────────────
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

    # ── Rekapitulasi Tahunan + breakdown per bulan per tahun ────────────────
    try:
        years = await aggregates.available_years()
        if years:
            snap["rekap_tahunan"] = {}
            snap["tren_per_bulan_per_tahun"] = {}  # matrix bulan x tahun
            for y in years:
                kpi_yr  = await aggregates.kpi_crm("", "", years=[y])
                prog_yr = await aggregates.ranking_program("", "", years=[y])
                # Per bulan dalam tahun ini
                tren_bln = await aggregates._fetch("""
                    SELECT substr(tanggal,1,7) AS bulan,
                           SUM(nominal) AS total, COUNT(*) AS jumlah
                    FROM donations
                    WHERE COALESCE(source_year, CAST(substr(tanggal,1,4) AS INTEGER)) = ?
                      AND is_institusional = 0
                    GROUP BY bulan ORDER BY bulan
                """, (y,))
                snap["rekap_tahunan"][str(y)] = {
                    "total_donasi_rp": kpi_yr.get("total", 0),
                    "jumlah_transaksi": kpi_yr.get("jumlah", 0),
                    "per_bulan": [
                        {"bulan": r["bulan"], "total_rp": r["total"], "jumlah": r["jumlah"]}
                        for r in tren_bln
                    ],
                    "top_program": [
                        {"program": p["program"], "total_rp": p["total"]}
                        for p in prog_yr[:5]
                    ]
                }
                # Tambahkan ke matrix per bulan
                for r in tren_bln:
                    bln = r["bulan"]  # format YYYY-MM
                    if bln not in snap["tren_per_bulan_per_tahun"]:
                        snap["tren_per_bulan_per_tahun"][bln] = {}
                    snap["tren_per_bulan_per_tahun"][bln][str(y)] = {
                        "total_rp": r["total"], "jumlah": r["jumlah"]
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

    # ── Kalender Konten + Performa Blast (ALL TIME) ───────────────────
    try:
        from datetime import timedelta

        # Recent events: untuk konten hari ini & mendatang (fetch 30 hari depan juga)
        cal_recent  = await calendar_gfi.get_calendar_with_stats(months=1)
        today_str   = today.isoformat()

        # Konten hari ini
        today_events = [e for e in cal_recent if e["tanggal_iso"] == today_str]
        snap["konten_hari_ini"] = [
            {"judul": e["judul"], "narasi_preview": e["narasi_preview"],
             "program": e["kode_program_detected"], "media": len(e["media"])}
            for e in today_events
        ]

        # Konten mendatang (30 hari ke depan)
        upcoming = [e for e in cal_recent if e["is_future"]]
        snap["konten_mendatang"] = [
            {"tanggal": e["tanggal_iso"], "judul": e["judul"],
             "program": e["kode_program_detected"]}
            for e in sorted(upcoming, key=lambda x: x["tanggal"])
        ]

        # ALL-TIME performance: semua event tanpa batas waktu
        all_perf = await calendar_gfi.get_all_content_performance()
        snap["performa_konten_all_time"] = {
            "total_blast_tercatat": all_perf["total_event"],
            "per_program": {
                prog: {
                    "total_rp":     d["total_rp"],
                    "jumlah_trx":   d["jumlah_trx"],
                    "count_blast":  d["count_blast"],
                    "avg_per_blast": d["avg_per_blast"],
                }
                for prog, d in list(all_perf["per_program"].items())
            },
            "top_blast_sepanjang_masa": all_perf["semua_blast"][:10],  # top 10
        }

        # Ranking konten terbaik (top 5 program by avg per blast)
        snap["ranking_konten_terbaik"] = sorted(
            [
                {"program": k, "avg_per_blast": v["avg_per_blast"],
                 "total_rp": v["total_rp"], "count_blast": v["count_blast"]}
                for k, v in all_perf["per_program"].items()
                if v["count_blast"] >= 1
            ],
            key=lambda x: x["avg_per_blast"], reverse=True
        )[:8]

    except Exception as e:
        log.warning(f"kalender_konten ERROR: {e}", exc_info=True)

    # ── Analisis Program dari DB (ALL TIME, tanpa kalender) ────────────────
    # Data ini selalu tersedia karena dari DB donasi langsung (2023 - sekarang)
    try:
        prog_perf = await program_analytics.get_program_performance_all_time()
        snap["analisis_program_db"] = {
            # Ringkasan satu baris per program (efisien untuk context)
            "ringkasan": prog_perf["ringkasan_singkat"],
            # Top 10 program by rata-rata bulanan (paling efisien)
            "top_by_efisiensi_bulanan": [
                {
                    "program":        p["program"],
                    "total_rp":       p["total_rp"],
                    "jumlah_trx":     p["jumlah_trx"],
                    "avg_per_bulan":  p["avg_per_bulan"],
                    "avg_per_trx":    p["avg_per_trx"],
                    "aktif_bulan":    p["aktif_bulan"],
                    "pertama":        p["pertama"],
                    "terakhir":       p["terakhir"],
                    "bulan_terbaik":  p["bulan_terbaik"],
                }
                for p in prog_perf["ranking_by_avg_bulan"]
            ],
            # Top per tahun
            "top_per_tahun": prog_perf["top_per_tahun"],
            # Total program yg pernah aktif
            "total_program_aktif": prog_perf["total_program"],
        }
    except Exception as e:
        log.warning(f"program_analytics ERROR: {e}", exc_info=True)

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

    # Tambahkan pengetahuan yang dipelajari tim ke system prompt
    knowledge_ctx = knowledge.to_context_string()

    system_prompt = _SYSTEM_TEMPLATE.format(
        tanggal=snap.get("tanggal_hari_ini", "hari ini"),
        data=data_json,
    ) + f"\n\n{knowledge_ctx}\n"

    payload = {
        "system_instruction": {
            "parts": [{"text": system_prompt}]
        },
        "contents": [
            {"role": "user", "parts": [{"text": question}]}
        ],
        "generationConfig": {
            "maxOutputTokens": 600,
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


async def _get_cs_targeted_detail(question: str, aggregates) -> dict:
    """
    Deteksi nama CS di pertanyaan, lalu query detail spesifik untuk CS tersebut.
    Fuzzy match: 'faridah' akan cocok dengan 'FaridahSS', 'FARIDAH', dll.
    """
    from datetime import timedelta
    today = date.today()
    d_3bln = (today - timedelta(days=90)).isoformat()
    d1 = today.isoformat()
    d0_bln = today.replace(day=1).isoformat()

    # Ambil semua CS yang ada di DB
    try:
        all_cs_rows = await aggregates._fetch(
            "SELECT DISTINCT cs FROM donations WHERE cs != '' ORDER BY cs"
        )
        all_cs_names = [r["cs"] for r in all_cs_rows]
    except Exception:
        return {}

    # Cari CS yang namanya disebut di pertanyaan (case-insensitive, partial match)
    question_lower = question.lower()
    matched_cs = []
    for cs_name in all_cs_names:
        # Ambil bagian nama tanpa suffix (FitriSS -> Fitri, TriWaba -> Tri)
        base = cs_name.lower().rstrip('ss').rstrip('waba').rstrip('ss')
        # Coba berbagai variasi: nama lengkap, base name
        variants = [
            cs_name.lower(),
            base,
            cs_name.lower().replace('ss', '').replace('waba', ''),
        ]
        if any(v and v in question_lower for v in variants):
            matched_cs.append(cs_name)

    if not matched_cs:
        return {}

    targeted = {}
    for cs in matched_cs[:3]:  # max 3 CS sekaligus
        try:
            detail_3bln = await aggregates.detail_cs(cs, d_3bln, d1)
            detail_bln  = await aggregates.detail_cs(cs, d0_bln, d1)

            # Tren per bulan 3 bulan terakhir
            tren_bln = await aggregates._fetch("""
                SELECT substr(tanggal,1,7) AS bulan,
                       SUM(nominal) AS total, COUNT(*) AS jumlah
                FROM donations
                WHERE cs = ? AND tanggal BETWEEN ? AND ?
                  AND is_institusional = 0
                GROUP BY bulan ORDER BY bulan
            """, (cs, d_3bln, d1))

            targeted[cs] = {
                "3_bulan_terakhir": detail_3bln.get("summary", {}),
                "bulan_ini":        detail_bln.get("summary", {}),
                "tren_per_bulan":   tren_bln,
                "program_3bln":     detail_3bln.get("programs", [])[:10],
            }
        except Exception as e:
            log.debug(f"targeted CS {cs}: {e}")

    return targeted


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

    # Tambahkan data spesifik kalau ada nama CS di pertanyaan
    try:
        targeted = await _get_cs_targeted_detail(question, aggregates)
        if targeted:
            snap["detail_cs_spesifik"] = targeted
            log.info(f"Targeted CS query: {list(targeted.keys())}")
    except Exception as e:
        log.debug(f"targeted detail: {e}")

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
