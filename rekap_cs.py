"""
rekap_cs.py — Helper untuk fitur Rekap CS otomatis
CS upload screenshot bukti transfer → Gemini extract data → submit ke Google Sheet
"""
import json
import logging
import os
import re
import requests

log = logging.getLogger("rekap_cs")

# ── API key khusus rekap CS (pisah dari bot WA supaya limit tidak konflik) ─
_REKAP_API_KEY = os.getenv("REKAP_CS_API_KEY", "")
_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_VISION_MODELS = ["gemini-2.0-flash", "gemini-1.5-flash"]

# ── Daftar CS (sesuai nama tab di Google Sheet) ────────────────────────────
CS_LIST = [
    "Annisa", "Eka", "Evi", "Faridah", "Fitri", "FitriSS",
    "Nabila", "Osa", "Rahma", "Rani", "Roswita",
    "Tri", "TriSS", "TriWaba", "Viona",
]

# ── Daftar Asal Donasi (dari Google Sheet aktual, semua 39 opsi) ───────────
ASAL_DONASI_OPTIONS = [
    "BCA 00-859-990-07 FREE TRAFFIC",
    "BCA  1567.00.00.10 SS INTAN",
    "BCA  1568.000.900 OWN TRAFFIC (ZISCO)",
    "BCA 1563333131 INDONESIA PEDULI",
    "BCA 1564011111 PAID TRAFFIC",
    "BCA 1564466444 BNI 17.8556.7069 SS INTAN",
    "BCA 1564466444 SS UGUN",
    "BNI  115.156.2313 OWN TRAFFIC (ZISCO)",
    "BNI 08.2117.6801 PAID TRAFFIC",
    "BNI 131-216-022-6 FREE TRAFFIC",
    "BNI 17.8556.7069 SS INTAN",
    "BNI 20.1104.2127 SS INTAN",
    "BNI 808.197.00.088 INDONESIA PEDULI",
    "BNI 90.6322.317 SS UGUN",
    "BRI  028.601.002.084.563 OWN TRAFFIC (ZISCO)",
    "BRI  028.601.002.085.569 INDONESIA PEDULI",
    "BRI (GFF) 0407.0100.030.3569 FREE TRAFFIC (MEDIA)",
    "BRI (MUSLIM CARES) 028601002119562 GOLDEN MEDIKA",
    "BRI 028.601.002.083.567 SS UGUN",
    "BRI 028.601.002.087.561 SS INTAN",
    "BRI 028.601.002.088.567 SS UGUN",
    "BRI 0407.01.000805.56.1 PAID TRAFFIC",
    "BSI  74.4077.77.77 PAID TRAFFIC",
    "BSI  790.888.888.4 OWN TRAFFIC (ZISCO)",
    "BSI 710-814-27-87 FREE TRAFFIC",
    "BSI 744.432.228.9 PARTNERSHIP",
    "BSI 773.000.443 SS INTAN",
    "BSI 788.100.788.8 SS UGUN",
    "BSI 788.533.533.5 INDONESIA PEDULI",
    "MANDIRI  130.00.30.131216 OWN TRAFFIC (ZISCO)",
    "MANDIRI  130.000.161213.7 PARTNERSHIP",
    "MANDIRI (TIM-TENG) 130.0006.131216 GOLDEN MEDIKA",
    "MANDIRI 130.0030.161213 INDONESIA PEDULI",
    "MANDIRI 130.0030.161312 SS UGUN",
    "MANDIRI 1300030161312",
    "MANDIRI KONVEN  130.000.131216.7 SS INTAN",
    "MUAMALAT  1010.10.7272 PAID TRAFFIC",
    "Mandiri 130-000-246-666-5 FREE TRAFFIC",
    "Mandiri 130.0022.555.521 PAID TRAFFIC",
]

# ── Prompt OCR untuk Gemini ────────────────────────────────────────────────
_PROMPT_OCR = """Ini adalah screenshot/foto bukti transfer bank dari donatur.

Tugasmu: extract informasi berikut dari gambar ini secara akurat.

Kembalikan HANYA JSON valid, tidak ada teks lain di luar JSON:
{
  "nama_donatur": "nama lengkap pengirim/rekening sumber (HURUF KAPITAL)",
  "nominal": "angka nominal transfer (angka saja, tanpa Rp, titik, atau koma)",
  "tanggal": "tanggal transfer format DD/MM/YYYY",
  "bank_asal": "nama bank pengirim (BCA/Mandiri/BNI/BRI/BSI/Muamalat/dll)",
  "catatan": "info tambahan jika ada (nomor referensi, keterangan, dll)"
}

Panduan:
- "nama_donatur": cari di field "Rekening Sumber", "Pengirim", "From", "Nama Pengirim", atau "Rekening Asal"
- "nominal": cari "Nominal Transfer", "Total Transaksi", "Jumlah Transfer" — ambil angka saja
- "tanggal": format DD/MM/YYYY — konversi jika perlu (misal "30 Sep 2026" → "30/09/2026")
- "bank_asal": bank pengirim, bukan penerima
- Jika field tidak ditemukan, isi dengan string kosong ""
"""


def _call_gemini_ocr(image_b64: str, mime_type: str) -> str:
    """Panggil Gemini vision pakai REKAP_CS_API_KEY (bukan key bot WA)."""
    if not _REKAP_API_KEY:
        raise RuntimeError("REKAP_CS_API_KEY belum di-set di .env server")

    payload = {
        "contents": [{"role": "user", "parts": [
            {"inline_data": {"mime_type": mime_type, "data": image_b64}},
            {"text": _PROMPT_OCR},
        ]}],
        "generationConfig": {"maxOutputTokens": 300, "temperature": 0.1},
    }
    last_err = None
    for model in _VISION_MODELS:
        url = f"{_BASE_URL}/{model}:generateContent?key={_REKAP_API_KEY}"
        try:
            r = requests.post(url, json=payload, timeout=20)
            if r.status_code == 503:
                continue
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            last_err = e
            log.warning(f"OCR model {model} error: {e}")
    raise RuntimeError(f"Semua model vision gagal: {last_err}")


def extract_from_bukti_transfer(image_b64: str, mime_type: str) -> dict:
    """
    Kirim gambar bukti transfer ke Gemini (key rekap CS), extract field rekap.
    Return dict: {nama_donatur, nominal, tanggal, bank_asal, catatan, error}
    """
    try:
        raw = _call_gemini_ocr(image_b64, mime_type)
        raw = re.sub(r"```(?:json)?", "", raw).strip().strip("`").strip()
        data = json.loads(raw)
        for k in ("nama_donatur", "nominal", "tanggal", "bank_asal", "catatan"):
            data.setdefault(k, "")
        data["nominal"] = re.sub(r"[^\d]", "", str(data["nominal"]))
        if data["nama_donatur"]:
            data["nama_donatur"] = data["nama_donatur"].upper().strip()
        data["error"] = ""
        log.info(f"OCR OK: {data}")
        return data
    except json.JSONDecodeError as e:
        log.warning(f"OCR JSON parse error: {e}")
        return {"nama_donatur": "", "nominal": "", "tanggal": "", "bank_asal": "", "catatan": "", "error": "Gagal baca format AI — isi manual"}
    except Exception as e:
        log.error(f"OCR error: {e}")
        return {"nama_donatur": "", "nominal": "", "tanggal": "", "bank_asal": "", "catatan": "", "error": str(e)}


def suggest_asal_donasi(bank_asal: str) -> list[str]:
    """
    Berikan saran Asal Donasi berdasarkan bank_asal yang terdeteksi.
    Kembalikan list opsi yang cocok (muncul duluan di dropdown).
    """
    if not bank_asal:
        return ASAL_DONASI_OPTIONS
    bank_upper = bank_asal.upper().strip()
    matched = [o for o in ASAL_DONASI_OPTIONS if bank_upper in o.upper()]
    rest = [o for o in ASAL_DONASI_OPTIONS if o not in matched]
    return matched + rest


def bulan_dari_tanggal(tanggal_str: str) -> str:
    """Konversi 'DD/MM/YYYY' → 'JANUARI', 'FEBRUARI', dst"""
    BULAN = {
        1: "JANUARI", 2: "FEBRUARI", 3: "MARET", 4: "APRIL",
        5: "MEI", 6: "JUNI", 7: "JULI", 8: "AGUSTUS",
        9: "SEPTEMBER", 10: "OKTOBER", 11: "NOVEMBER", 12: "DESEMBER"
    }
    try:
        parts = tanggal_str.strip().split("/")
        return BULAN.get(int(parts[1]), "")
    except Exception:
        return ""
