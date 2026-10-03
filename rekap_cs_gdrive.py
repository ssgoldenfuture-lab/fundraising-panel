"""
rekap_cs_gdrive.py — Kirim data rekap CS ke Apps Script webhook
                     (Apps Script yang handle Sheet append + Drive upload)
Setelah upload sukses, file lokal dihapus dari server.
"""
import base64
import json
import logging
import os
import re
from pathlib import Path

import httpx

log = logging.getLogger("rekap_cs_gdrive")

# URL Apps Script Web App — isi dari .env setelah deploy
APPS_SCRIPT_URL = os.getenv("REKAP_CS_SCRIPT_URL", "")


def _img_to_b64(path: str) -> tuple[str, str]:
    """Baca file gambar, return (base64_string, mime_type)."""
    p = Path(path)
    ext = p.suffix.lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png", ".webp": "image/webp",
                ".gif": "image/gif"}
    mime = mime_map.get(ext, "image/jpeg")
    with open(p, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return b64, mime


async def kirim_ke_appscript(
    cs_name: str,
    tanggal: str,          # YYYY-MM-DD
    nama_donatur: str,
    nomor_hp: str,
    username_ig: str,
    nominal: int,
    kode_program: str,
    asal_donasi: str,
    bank_asal: str,
    keterangan: str,
    bulan: str,            # JANUARI, FEBRUARI, dst
    foto_path: str | None = None,
) -> dict:
    """
    Kirim data rekap ke Apps Script. Return:
    {"status": "ok", "drive_url": "...", "message": "..."}
    atau {"status": "error", "message": "..."}
    """
    if not APPS_SCRIPT_URL:
        return {"status": "error", "message": "REKAP_CS_SCRIPT_URL belum dikonfigurasi di .env"}

    # Encode foto jika ada
    foto_b64 = ""
    foto_mime = "image/jpeg"
    foto_name = f"bukti_{cs_name}_{tanggal}_{nama_donatur[:10]}.jpg".replace(" ", "_")

    if foto_path and os.path.isfile(foto_path):
        try:
            foto_b64, foto_mime = _img_to_b64(foto_path)
            foto_name = Path(foto_path).name
        except Exception as e:
            log.warning(f"Gagal encode foto: {e}")

    payload = {
        "cs_name":      cs_name,
        "tanggal":      tanggal,
        "nama_donatur": nama_donatur,
        "nomor_hp":     nomor_hp,
        "username_ig":  username_ig,
        "nominal":      nominal,
        "kode_program": kode_program,
        "asal_donasi":  asal_donasi,
        "bank_asal":    bank_asal,
        "keterangan":   keterangan,
        "bulan":        bulan,
        "foto_base64":  foto_b64,
        "foto_mime":    foto_mime,
        "foto_name":    foto_name,
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                APPS_SCRIPT_URL,
                content=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                follow_redirects=True,
            )
            resp.raise_for_status()
            result = resp.json()
    except httpx.HTTPStatusError as e:
        log.error(f"Apps Script HTTP error: {e.response.status_code} — {e.response.text[:200]}")
        return {"status": "error", "message": f"HTTP {e.response.status_code}"}
    except Exception as e:
        log.error(f"Apps Script error: {e}")
        return {"status": "error", "message": str(e)}

    # Hapus file lokal kalau upload berhasil
    if result.get("status") == "ok" and foto_path and os.path.isfile(foto_path):
        try:
            os.remove(foto_path)
            log.info(f"File lokal dihapus: {foto_path}")
        except Exception as e:
            log.warning(f"Gagal hapus file lokal {foto_path}: {e}")

    return result
