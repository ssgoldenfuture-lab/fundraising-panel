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

# Bot LID/phone — diisi dari env
# REPLAI_BOT_LID: LID format (15 digit, mulai 1) — muncul di @mention grup
# REPLAI_BOT_PHONE: nomor WA bot (628xxx) — fallback jika LID belum diketahui
_BOT_LID:   str = os.getenv("REPLAI_BOT_LID", "").strip()
_BOT_PHONE: str = os.getenv("REPLAI_BOT_PHONE", "").strip()  # misal: 6285187290682



# Kata kunci TAMBAHAN untuk trigger di grup (selain tag langsung)
GROUP_TRIGGERS = ["!laporan", "!cs", "!donasi", "!status", "!bot"]

# Whitelist nomor yang boleh query lewat DM (kosong = semua orang boleh)
# Whitelist nomor yang boleh query lewat DM
# Kosong = semua orang boleh. Diisi = hanya nomor ini yang dilayani.
DM_WHITELIST: list[str] = [
    "6282130536385",  # Ilham
    "6285187290654",  # Ugun
    "6288219892230",  # Fitri
]


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
    """
    Cek apakah BOT di-tag dalam pesan grup.
    Cek 3 format:
    1. LID format: @21634488488118  (dari REPLAI_BOT_LID)
    2. Phone format: @6285187290682 (dari REPLAI_BOT_PHONE)
    3. Fallback: ANY @{digits 10+} — kalau grup sudah aktif dan ada mention,
       kemungkinan besar bot yang di-tag (tidak ada member lain yang namanya angka)
    """
    if not message:
        return False
    # Format 1: LID (paling akurat)
    if _BOT_LID and f"@{_BOT_LID}" in message:
        return True
    # Format 2: phone number langsung
    if _BOT_PHONE and f"@{_BOT_PHONE}" in message:
        return True
    return False


# Kata kunci INTI yang sangat spesifik ke data/fundraising.
# Kita HAPUS kata tanya umum (apa, siapa, gimana, ?) supaya bot tidak nimbrung
# saat tim ngobrol biasa (misal: "gimana nih kabarnya?", "kumaha am?").
_DATA_KEYWORDS = [
    # Keyword utama fundraising
    "donasi", "program", "ranking", "rangking", "laporan",
    "rekap", "performa", "target", "capaian",
    "transaksi", "revenue", "konversi", "insight",
    "terbesar", "terkecil", "terbanyak", "tertinggi",
    # Keyword follow-up (lanjutan)
    "perbulan", "pertahun", "rincian", "detail",
    "data", "grafik", "urutkan", "bandingkan", "dibanding",
    # Keyword kalender konten & blasting
    "konten", "blast", "blasting", "jadwal", "kalender",
    "besok", "minggu", "hari ini", "narasii", "narasi",
    "efektif", "terbaik", "hasil blast", "performa konten",
    # Keyword analisis konten / copywriting (trigger baru)
    "nilai", "review", "copywriting", "copy", "broadcast",
    "rekomen", "rekomendasi", "saranin", "saran blast",
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
    - DM: selalu respon (dengan whitelist opsional)
    - Grup di-tag langsung (LID/phone match): selalu respon
    - Grup aktif + ada @mention angka apapun: respon (kemungkinan besar di-tag)
    - Grup aktif + pesan = pertanyaan data: respon
    - Grup belum aktif: hanya trigger eksplisit (!laporan dll)
    """
    msg_type = payload.get("type", "single")
    message  = payload.get("message") or ""
    text     = message.lower().strip()
    sender   = payload.get("from", "")
    sender_name = payload.get("from_name", "") or payload.get("name", "")

    # Abaikan pesan dari bot sendiri (echo webhook)
    if _BOT_PHONE and sender_name and "fundraising assistant" in sender_name.lower():
        return False

    if msg_type == "single":
        if not DM_WHITELIST:
            return True
        phone_clean = sender.split("@")[0]
        return any(phone_clean in w for w in DM_WHITELIST)

    elif msg_type == "group":
        # 1. Bot di-tag langsung (LID atau phone number match) → selalu respon
        if _is_bot_mentioned(message):
            return True
        # 2. Grup aktif + ada @mention (angka 8+ digit) → kemungkinan tag bot
        #    Lebih aman dari fallback global karena hanya untuk grup yang sudah aktif
        if _has_active_session(sender) and re.search(r'@\d{8,}', message):
            return True
        # 3. Grup aktif + pertanyaan data → respon
        if _has_active_session(sender):
            return _is_data_question(text)
        # 4. Trigger keyword eksplisit (!laporan dll) → aktifkan grup + respon
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

    # Command belajar — update knowledge base
    if t.startswith("!belajar"):
        return "belajar"

    # Analisis copywriting
    if t.startswith("!nilai") or t.startswith("!review"):
        return "nilai_copy"

    # Rekomendasi blast
    if t.startswith("!rekomen") or t.startswith("!saran"):
        return "rekomen_blast"

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


def _extract_image_from_payload(payload: dict) -> tuple[str | None, str]:
    """
    Ekstrak gambar dari payload Replai.
    Return: (base64_string_atau_None, mime_type)
    Replai bisa kirim: media_url, image_url, media_base64, file_url
    """
    import base64, urllib.request

    mime_type = "image/jpeg"  # default

    # Cek media_type kalau ada
    mt = payload.get("media_type") or payload.get("mime_type") or ""
    if mt and mt.startswith("image"):
        mime_type = mt

    # Prioritas 1: sudah base64
    b64 = payload.get("media_base64") or payload.get("image_base64")
    if b64:
        return b64, mime_type

    # Prioritas 2: URL → download → konversi ke base64
    url = (payload.get("media_url") or payload.get("image_url")
           or payload.get("file_url") or payload.get("url"))
    if url and url.startswith("http"):
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = resp.read()
                # Deteksi mime dari header kalau ada
                ct = resp.headers.get("Content-Type", "")
                if ct.startswith("image"):
                    mime_type = ct.split(";")[0].strip()
            return base64.b64encode(data).decode(), mime_type
        except Exception as e:
            log.warning(f"Gagal download image dari {url}: {e}")

    return None, mime_type


def _is_image_message(payload: dict) -> bool:
    """Cek apakah payload mengandung gambar."""
    msg_type = payload.get("message_type") or payload.get("type_message") or ""
    if msg_type.lower() in ("image", "imageMessage", "photo"):
        return True
    # Cek field media
    has_media = bool(
        payload.get("media_url") or payload.get("image_url")
        or payload.get("media_base64") or payload.get("image_base64")
        or payload.get("file_url")
    )
    # Pastikan ada mime image (hindari false positive video/document)
    mt = payload.get("media_type") or payload.get("mime_type") or ""
    if has_media and (not mt or mt.startswith("image")):
        return True
    return False


import wa_ai      # Gemini AI agent
import knowledge  # Self-learning knowledge base


async def handle_webhook(payload: dict, aggregates, wa_bot, home_agg, berdonasi_db) -> str | None:
    """
    Proses payload webhook Replai.
    - Fast-path: intent jelas -> query DB langsung -> format jawaban
    - AI path: pertanyaan bebas -> Gemini + data real -> jawaban natural
    - Image path: ada gambar -> analisis visual konten
    - Copy path: !nilai [teks] -> analisis copywriting
    - Rekomen path: !rekomen -> rekomendasi blast
    """
    if not _should_respond(payload):
        log.info(f"Webhook ignored: type={payload.get('type')} from={payload.get('from')}")
        return None

    raw_text = payload.get("message", "") or ""
    text     = _clean_text(raw_text)
    sender   = payload.get("from", "")
    is_grp   = payload.get("type") == "group"
    intent   = _detect_intent(text)

    log.info(f"WA webhook: intent={intent} clean_text='{text[:80]}' from={sender} group={is_grp}")

    try:
        # ── Cek dulu: ada gambar? Prioritas tertinggi kalau ada media ──
        if _is_image_message(payload):
            log.info("Gambar terdeteksi — mode analisis visual konten")
            image_b64, mime_type = _extract_image_from_payload(payload)
            if image_b64:
                caption = text  # caption = teks yang menyertai gambar
                return await wa_ai.analyze_content_visual(
                    image_b64, mime_type, caption,
                    aggregates, home_agg, berdonasi_db
                )
            else:
                return (
                    "Hmm, gambarnya tidak bisa diakses. Coba kirim ulang atau "
                    "pastikan gambar terkirim sempurna dulu ya. 🙏"
                )

        if intent == "belajar":
            # Update knowledge base
            key, val, cat = knowledge.parse_learn_command(text)
            if key and val:
                msg = knowledge.learn(key, val, cat)
                return msg
            else:
                return "Format: *!belajar: KODE = Penjelasan*\nContoh: !belajar: KEI = Kemiskinan Indonesia"

        elif intent == "nilai_copy":
            # Analisis copywriting
            # Strip command: "!nilai " atau "!review " dari awal
            copy_text = text
            for prefix in ("!nilai ", "!review ", "!nilai:", "!review:"):
                if copy_text.lower().startswith(prefix.lower()):
                    copy_text = copy_text[len(prefix):].strip()
                    break
            if not copy_text or len(copy_text) < 10:
                return (
                    "Kirim teks broadcast-nya setelah perintah ya! •\n"
                    "Contoh: `!nilai Bismillah, mari bantu anak yatim...`"
                )
            return await wa_ai.analyze_copywriting(
                copy_text, aggregates, home_agg, berdonasi_db
            )

        elif intent == "rekomen_blast":
            # Rekomendasi program blast
            return await wa_ai.recommend_blast(aggregates, home_agg, berdonasi_db)

        elif intent == "help":
            # Tag tanpa pesan / pesan kosong -> AI perkenalkan diri dengan data
            return await wa_ai.answer(
                "Kamu baru di-tag. Perkenalkan dirimu secara singkat dan sebutkan "
                "apa saja yang bisa kamu bantu berdasarkan data fundraising. "
                "Sebutin juga fitur baru: !nilai [teks] untuk analisis copywriting, "
                "!rekomen untuk rekomendasi blast, dan kirim gambar untuk analisis visual konten.",
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
            log.info(f"Routing ke Gemini AI: '{text[:60]}'")
            return await wa_ai.answer(text, aggregates, home_agg, berdonasi_db)

    except Exception as e:
        log.error(f"WA webhook handler error: {e}", exc_info=True)
        return "Maaf, terjadi kesalahan saat mengambil data. Coba lagi nanti. 🙏"
