"""
wa_webhook.py — Handler untuk webhook dari Replai.id
Dipanggil dari main.py saat POST /webhook/replai diterima.

Payload dari Replai (unofficial):
{
  "from":      "6281290641111@s.whatsapp.net",  // sender JID
  "from_name": "Nama Pengirim",
  "type":      "single" | "group",             // single = DM, group = grup
  "message":   "Teks pesan",
  "device_key": "xxx"
}
"""
import logging
import re
import os
from datetime import date

log = logging.getLogger("wa_webhook")

# Bot LID/phone — diisi otomatis dari env, atau dari pertama kali dapat mention
# Contoh: REPLAI_BOT_LID=21634488488118
_BOT_LID: str = os.getenv("REPLAI_BOT_LID", "21634488488118")  # dari log: @21634488488118

# Kata kunci TAMBAHAN untuk trigger di grup (selain tag langsung)
GROUP_TRIGGERS = ["!laporan", "!cs", "!donasi", "!status", "!bot"]

# Whitelist nomor yang boleh query lewat DM (kosong = semua orang boleh)
DM_WHITELIST: list[str] = []

# ── Permanent Group Activation ───────────────────────────────────────────────
# Sekali grup tag bot → bot respon semua pesan di grup itu selamanya (tanpa tag lagi).
# Disimpan ke file JSON supaya tidak reset kalau service restart.
import json as _json
import pathlib as _pathlib

_ACTIVE_FILE = _pathlib.Path(__file__).parent / "active_groups.json"
_ACTIVATED_GROUPS: set[str] = set()

def _load_active_groups():
    global _ACTIVATED_GROUPS
    if _ACTIVE_FILE.exists():
        try:
            data = _json.loads(_ACTIVE_FILE.read_text())
            _ACTIVATED_GROUPS = set(data.get("groups", []))
            log.info(f"Loaded {len(_ACTIVATED_GROUPS)} active group(s)")
        except Exception:
            _ACTIVATED_GROUPS = set()

def _save_active_groups():
    try:
        _ACTIVE_FILE.write_text(_json.dumps({"groups": list(_ACTIVATED_GROUPS)}))
    except Exception as e:
        log.warning(f"Gagal simpan active_groups: {e}")

def _has_active_session(chat_id: str) -> bool:
    """Cek apakah grup ini sudah diaktifkan (permanen)."""
    return chat_id in _ACTIVATED_GROUPS

def open_session(chat_id: str):
    """Aktifkan grup — selamanya, disimpan ke disk."""
    if chat_id not in _ACTIVATED_GROUPS:
        _ACTIVATED_GROUPS.add(chat_id)
        _save_active_groups()
        log.info(f"Group activated (permanent): {chat_id}")

# Load saat modul pertama kali diimport
_load_active_groups()


def _is_bot_mentioned(message: str) -> bool:
    """Cek apakah BOT (bukan orang lain) di-tag dalam pesan grup."""
    if not message:
        return False
    # Hanya cocok kalau yang di-tag adalah bot (pakai LID bot yang spesifik)
    if _BOT_LID and f"@{_BOT_LID}" in message:
        return True
    # Jangan pakai fallback @\d{10,} — itu terlalu broad dan match siapapun yang di-tag
    return False


# Kata kunci INTI yang sangat spesifik ke data/fundraising.
# Kita HAPUS kata tanya umum (apa, siapa, gimana, ?) supaya bot tidak nimbrung
# saat tim ngobrol biasa (misal: "gimana nih kabarnya?", "kumaha am?").
_DATA_KEYWORDS = [
    # Keyword utama
    "donasi", "program", "ranking", "rangking", "laporan",
    "rekap", "performa", "target", "capaian",
    "transaksi", "revenue", "konversi", "insight",
    "terbesar", "terkecil", "terbanyak", "tertinggi",
    # Keyword follow-up (lanjutan)
    "perbulan", "pertahun", "rincian", "detail", 
    "data", "grafik", "urutkan", "bandingkan", "dibanding",
]


def _is_data_question(text: str) -> bool:
    """
    Cek apakah pesan ini adalah pertanyaan data yang relevan buat bot.
    Harus super strict agar bot tidak nimbrung obrolan tim.
    """
    t = text.lower().strip()
    if not t:
        return False
        
    # Harus ada minimal 1 kata kunci spesifik data/fundraising.
    # Tidak lagi merespon hanya karena ada tanda tanya (?).
    if any(k in t for k in _DATA_KEYWORDS):
        return True
        
    return False


def _should_respond(payload: dict) -> bool:
    """
    Return True kalau bot harus membalas.
    - DM: selalu respon
    - Grup di-tag langsung: selalu respon
    - Grup aktif (pernah di-tag): respon HANYA kalau isinya pertanyaan data,
      bukan obrolan random — supaya bot tidak nimbrung sembarangan
    """
    msg_type = payload.get("type", "single")
    message  = payload.get("message") or ""
    text     = message.lower().strip()
    sender   = payload.get("from", "")

    if msg_type == "single":
        if not DM_WHITELIST:
            return True
        phone_clean = sender.split("@")[0]
        return any(phone_clean in w for w in DM_WHITELIST)

    elif msg_type == "group":
        # 1. Bot di-tag langsung → selalu respon
        if _is_bot_mentioned(message):
            return True
        # 2. Grup aktif (permanen) → respon HANYA kalau pertanyaan data
        #    Kalau obrolan random → bot diam, tidak nimbrung
        if _has_active_session(sender):
            return _is_data_question(text)
        # 3. Trigger keyword eksplisit (!laporan dll)
        return any(trigger in text for trigger in GROUP_TRIGGERS)

    return False


def _clean_text(message: str) -> str:
    """Strip @mentions dan whitespace dari teks pesan."""
    # Hapus semua @<angka> (mentions WA)
    cleaned = re.sub(r'@\d+', '', message).strip()
    return cleaned


def _detect_intent(text: str) -> str:
    """
    Deteksi intent dari teks pesan (sudah di-strip @mention).
    Karena Gemini AI sudah aktif, semua pertanyaan natural kita lempar ke Gemini (unknown).
    Hanya perintah eksak dengan tanda seru (!) yang pakai format hardcoded/kaku.
    """
    t = _clean_text(text).lower()

    # Perintah eksplisit (kaku)
    if t.startswith("!laporan"):
        return "laporan_harian"
    if t.startswith("!ranking"):
        return "cs_ranking"
    if t.startswith("!hariini"):
        return "donasi_hari_ini"
    if t.startswith("!alert"):
        return "alert"

    # Pesan terlalu pendek / cuma mention -> kasih petunjuk via Gemini
    if not t.strip() or len(t.strip()) <= 3:
        return "help"

    # Semua pertanyaan lain (bahkan yang ada kata "ranking", "hari ini") -> Gemini AI
    return "unknown"

import wa_ai  # Gemini AI agent


async def handle_webhook(payload: dict, aggregates, wa_bot, home_agg, berdonasi_db) -> str | None:
    """
    Proses payload webhook Replai.
    - Fast-path: intent jelas -> query DB langsung -> format jawaban
    - AI path: pertanyaan bebas -> Gemini + data real -> jawaban natural
    """
    if not _should_respond(payload):
        log.info(f"Webhook ignored: type={payload.get('type')} from={payload.get('from')}")
        return None

    raw_text = payload.get("message", "")
    text     = _clean_text(raw_text)
    sender   = payload.get("from", "")
    is_grp   = payload.get("type") == "group"
    intent   = _detect_intent(text)

    log.info(f"WA webhook: intent={intent} clean_text='{text}' from={sender} group={is_grp}")

    try:
        if intent == "help":
            # Tag tanpa pesan / pesan kosong -> AI perkenalkan diri dengan data
            return await wa_ai.answer(
                "Kamu baru di-tag. Perkenalkan dirimu secara singkat dan sebutkan "
                "apa saja yang bisa kamu bantu berdasarkan data fundraising.",
                aggregates, home_agg, berdonasi_db
            )

        elif intent == "donasi_hari_ini":
            today = date.today().isoformat()
            data  = await aggregates.kpi_crm(today, today)
            return wa_bot.format_donasi_hari_ini(data)

        elif intent == "cs_ranking":
            today   = date.today()
            d0      = today.replace(day=1).isoformat()
            cs_list = await aggregates.ranking_cs(d0, today.isoformat())
            return wa_bot.format_cs_ranking(cs_list)

        elif intent == "laporan_harian":
            crm_now    = await home_agg.crm_bulan_ini()
            online_now = {"revenue": 0, "conv_rate": 0, "pending": 0}
            try:
                online_now = await home_agg.online_bulan_ini(berdonasi_db)
            except Exception:
                pass
            cs_alert   = await home_agg.crm_cs_alert()
            stuck_list = []
            try:
                stuck_list = await home_agg.online_stuck_payments(berdonasi_db)
            except Exception:
                pass
            hari_buruk = []
            try:
                hari_buruk = await home_agg.hari_konversi_buruk(berdonasi_db)
            except Exception:
                pass
            new_big = await home_agg.crm_donatur_baru_besar()
            return wa_bot.format_laporan_harian(
                crm_now, online_now, cs_alert, stuck_list, hari_buruk, new_big
            )

        elif intent == "alert":
            cs_alert   = await home_agg.crm_cs_alert()
            stuck_list = []
            try:
                stuck_list = await home_agg.online_stuck_payments(berdonasi_db)
            except Exception:
                pass
            hari_buruk = []
            try:
                hari_buruk = await home_agg.hari_konversi_buruk(berdonasi_db)
            except Exception:
                pass
            return wa_bot.format_alert_singkat(cs_alert, stuck_list, hari_buruk)

        else:
            # Pertanyaan bebas -> AI Agent dengan data real
            log.info(f"Routing ke Gemini AI: '{text}'")
            return await wa_ai.answer(text, aggregates, home_agg, berdonasi_db)

    except Exception as e:
        log.error(f"WA webhook handler error: {e}", exc_info=True)
        return "Maaf, terjadi kesalahan saat mengambil data. Coba lagi nanti. 🙏"
