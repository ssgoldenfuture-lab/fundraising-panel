"""
wa_bot.py — Replai.id unofficial WhatsApp API client
Docs: https://chat.replai.id (Non-Official / device_key based)

Auth: credentials di body JSON, tidak perlu Bearer token.
Send:  POST /api-app/whatsapp/send-message
Chats: GET  /api-app/whatsapp/chats/{device_key}?api_key=...&is_group=true
Check: POST /api-app/integration/checking-device
"""
import os
import logging
import requests
from datetime import datetime

log = logging.getLogger("wa_bot")

BASE_URL    = os.getenv("REPLAI_BASE_URL", "https://chat.replai.id")
DEVICE_KEY  = os.getenv("REPLAI_DEVICE_KEY", "")
API_KEY     = os.getenv("REPLAI_API_KEY", "")

_TIMEOUT = 15  # detik


def _post(path: str, body: dict) -> dict:
    """POST ke Replai API, return parsed JSON atau raise."""
    url = BASE_URL.rstrip("/") + path
    r = requests.post(url, json=body, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _get(path: str, params: dict = None) -> dict:
    url = BASE_URL.rstrip("/") + path
    r = requests.get(url, params=params or {}, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


# ── Status device ─────────────────────────────────────────────────────────────

def check_device() -> dict:
    """Cek apakah device terhubung ke WhatsApp."""
    try:
        data = _post("/api-app/integration/checking-device", {
            "api_key":    API_KEY,
            "device":     DEVICE_KEY,
        })
        return {"ok": True, "data": data}
    except Exception as e:
        log.warning(f"check_device error: {e}")
        return {"ok": False, "error": str(e)}


# ── Kirim pesan ───────────────────────────────────────────────────────────────

def _normalize_phone(from_jid: str, is_group: bool = False) -> str:
    """
    Normalize 'from' field dari webhook Replai ke format yang diterima send API.
    
    Replai strips @suffix dari JID, jadi kita harus detect & tambahkan kembali:
    - Group ID (is_group=True) → pakai apa adanya (Replai handle via is_group flag)
    - LID (15 digit, mulai 1) → append @lid  ← WA Privacy feature baru
    - Nomor biasa (628xxx) → pakai apa adanya
    - Sudah ada @suffix → pakai apa adanya
    """
    if '@' in from_jid:
        return from_jid  # sudah punya suffix, biarkan

    phone = from_jid.strip()

    if is_group:
        return phone  # group ID digunakan langsung dengan is_group=True

    # LID detection: 15 digit mulai dari "1"
    if len(phone) == 15 and phone.startswith("1"):
        return f"{phone}@lid"

    # Nomor biasa (628xx, format e164)
    return phone


def send_message(phone: str, text: str, is_group: bool = False) -> dict:
    """
    Kirim teks ke nomor personal atau grup.
    phone: dari webhook 'from' field atau group_id dari settings.
    """
    if not DEVICE_KEY or not API_KEY:
        return {"ok": False, "error": "Credentials tidak dikonfigurasi"}
    try:
        normalized = _normalize_phone(phone, is_group)
        body = {
            "device_key": DEVICE_KEY,
            "api_key":    API_KEY,
            "phone":      normalized,
            "method":     "text",
            "text":       text,
        }
        if is_group:
            body["is_group"] = True
        data = _post("/api-app/whatsapp/send-message", body)
        log.info(f"WA sent to {normalized}: {data}")
        return {"ok": True, "data": data}
    except Exception as e:
        log.warning(f"send_message error to {phone}: {e}")
        return {"ok": False, "error": str(e)}


def reply_message(to: str, text: str, is_group: bool = False) -> dict:
    """Alias send_message untuk balas webhook."""
    return send_message(to, text, is_group=is_group)


# ── Daftar grup/chat ──────────────────────────────────────────────────────────

def get_group_list() -> list[dict]:
    """Ambil daftar grup yang terhubung ke device."""
    try:
        data = _get(f"/api-app/whatsapp/chats/{DEVICE_KEY}", {
            "api_key":  API_KEY,
            "is_group": "true",
        })
        chats = data.get("data", data)
        if isinstance(chats, list):
            return chats
        return []
    except Exception as e:
        log.warning(f"get_group_list error: {e}")
        return []


# ── Format laporan harian ─────────────────────────────────────────────────────

def format_laporan_harian(crm_now: dict, online_now: dict,
                           cs_alert: list, stuck: list,
                           hari_buruk: list, new_big: list) -> str:
    """
    Format laporan ringkas untuk WA — bullet points, max ~400 kata.
    Bukan copy-paste dari web, tapi versi ringkas yang enak dibaca di WA.
    """
    def rp(v):
        v = int(v or 0)
        if v >= 1_000_000_000: return f"Rp {v/1_000_000_000:.1f} M"
        if v >= 1_000_000:     return f"Rp {v/1_000_000:.1f} jt"
        return f"Rp {v:,}".replace(",", ".")

    now = datetime.now()
    bulan_ini = now.strftime("%B %Y")
    tanggal   = now.strftime("%d %b %Y %H:%M")

    crm_total    = crm_now.get("total", 0)
    crm_jumlah   = crm_now.get("jumlah", 0)
    online_rev   = online_now.get("revenue", 0)
    online_conv  = online_now.get("conv_rate", 0)
    online_pend  = online_now.get("pending", 0)
    gabungan     = crm_total + online_rev

    lines = [
        f"📊 *Laporan Fundraising GFI*",
        f"_{bulan_ini} · dikirim {tanggal}_",
        "",
        "💰 *Penerimaan Bulan Ini*",
        f"• CRM (konfirmasi CS): {rp(crm_total)} ({crm_jumlah} txn)",
        f"• Transaksi online: {rp(online_rev)} (konversi {online_conv:.0f}%)" if online_rev > 0 else "• Transaksi online: —",
        f"• *Total gabungan: {rp(gabungan)}*",
        "",
    ]

    # Alert
    alerts = []
    if hari_buruk:
        hari_str = ", ".join(h["label"] for h in hari_buruk[:3])
        alerts.append(f"📉 Konversi online rendah: {hari_str}")
    if stuck:
        alerts.append(f"⏳ {len(stuck)} transaksi tertahan >3 hari")
    if cs_alert:
        cs_str = ", ".join(f"{x['cs']} ({x['pct']}%)" for x in cs_alert[:3])
        alerts.append(f"📊 CS performa turun: {cs_str}")
    if new_big:
        new_str = ", ".join(
            f"{d.get('donor_name','—')} ({rp(d.get('nominal',0))})"
            for d in new_big[:2]
        )
        alerts.append(f"🆕 Donatur baru besar: {new_str}")
    if online_pend > 0:
        alerts.append(f"⏸ {online_pend} transaksi menunggu konfirmasi")

    if alerts:
        lines.append("⚠️ *Perlu Perhatian*")
        lines.extend(f"• {a}" for a in alerts)
    else:
        lines.append("✅ Tidak ada hal yang perlu perhatian.")

    lines += ["", "_Dashboard: fundraising.goldenfutureindonesia.org_"]
    return "\n".join(lines)


# ── Format jawaban query dari WA ──────────────────────────────────────────────

def format_donasi_hari_ini(data: dict) -> str:
    def rp(v):
        v = int(v or 0)
        if v >= 1_000_000_000: return f"Rp {v/1_000_000_000:.1f} M"
        if v >= 1_000_000:     return f"Rp {v/1_000_000:.1f} jt"
        return f"Rp {v:,}".replace(",", ".")

    total = data.get("total", 0)
    jumlah = data.get("jumlah", 0)
    tanggal = datetime.now().strftime("%d %b %Y")
    return (
        f"📊 *Donasi Hari Ini ({tanggal})*\n"
        f"• Total: {rp(total)}\n"
        f"• Jumlah transaksi: {jumlah}\n"
        f"\n_Lihat detail di fundraising.goldenfutureindonesia.org_"
    )


def format_cs_ranking(cs_list: list) -> str:
    def rp(v):
        v = int(v or 0)
        if v >= 1_000_000: return f"Rp {v/1_000_000:.1f} jt"
        return f"Rp {v:,}".replace(",", ".")

    if not cs_list:
        return "❌ Belum ada data CS bulan ini."

    bulan = datetime.now().strftime("%B %Y")
    lines = [f"🏆 *Ranking CS Bulan Ini ({bulan})*"]
    medals = ["🥇", "🥈", "🥉"]
    for i, cs in enumerate(cs_list[:5]):
        icon = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{icon} {cs['cs']}: {rp(cs['total'])} ({cs['jumlah']} txn)")
    return "\n".join(lines)


def format_alert_singkat(cs_alert: list, stuck: list, hari_buruk: list) -> str:
    lines = ["⚠️ *Status Terkini*"]
    if not (cs_alert or stuck or hari_buruk):
        return "✅ Semua lancar, tidak ada hal yang perlu perhatian saat ini."

    if hari_buruk:
        lines.append(f"📉 Hari dengan konversi rendah (7 hari terakhir): {len(hari_buruk)} hari")
    if stuck:
        lines.append(f"⏳ Transaksi tertahan >3 hari: {len(stuck)} transaksi")
    if cs_alert:
        lines.append(f"📊 CS dengan performa turun: {len(cs_alert)} CS")

    return "\n".join(lines)
