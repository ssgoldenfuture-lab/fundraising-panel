"""
knowledge.py — Sistem memori/pembelajaran bot GFI
Bot menyimpan fakta yang dipelajari dari tim ke file JSON.
Fakta-fakta ini dimuat sebagai konteks tambahan ke Gemini.
"""
import json
import logging
import pathlib
import re
from datetime import datetime

log = logging.getLogger("knowledge")

_KNOWLEDGE_FILE = pathlib.Path(__file__).parent / "knowledge.json"

# ── Default knowledge (bootstrap awal) ───────────────────────────────────────
_DEFAULTS = {
    "kode_program": {
        "P7":    "Gaza / Palestina — program air bersih & kemanusiaan Gaza (terbesar)",
        "WBP":   "Air Bersih Palestina / Sumur Air Gaza",
        "KEI":   "?? (belum diisi tim)",
        "EWP":   "?? (belum diisi tim)",
        "PU":    "Pundi Umat / program umum",
        "SU":    "Shadaqah Umum",
        "ZAK":   "Zakat",
        "ZAKP":  "Zakat Profesi",
        "ZAKS":  "Zakat Saham",
        "QA":    "Qurban",
        "QP":    "Qurban Patungan",
        "QY":    "Qurban Yatim",
        "QS":    "Qurban Sapi",
        "QR":    "Qurban Regular",
        "QKA":   "Qurban Kambing",
        "QI":    "Qurban Idul Adha",
        "P1":    "?? (belum diisi tim)",
        "P2":    "?? (belum diisi tim)",
        "P4":    "?? (belum diisi tim)",
        "P8":    "?? (belum diisi tim)",
        "WBA":   "Wakaf / Sumur Bor Air",
        "WBS":   "Wakaf Sumur / Sarana Air",
        "WBI":   "Wakaf Bangunan / Infrastruktur",
        "MAA":   "?? (belum diisi tim)",
        "DUP":   "Dana Umat Program",
        "DUS":   "Dana Umat Sosial",
        "MSI":   "?? (belum diisi tim)",
        "EV31":  "Event ke-31",
        "EV14":  "Event ke-14",
        "EV8":   "Event ke-8",
        "EV18":  "Event ke-18",
        "EV20":  "Event ke-20",
        "RP":    "?? (belum diisi tim)",
        "UM":    "Umum / Non-spesifik",
        "PR":    "?? (belum diisi tim)",
        "IND6":  "Indonesia Program ke-6",
        "RI":    "?? (belum diisi tim)",
        "TSPU":  "Tanda Sayang / Program Umum",
        "TSMAI": "Tanda Sayang MAI",
        "RS":    "?? (belum diisi tim)",
        "MDL":   "?? (belum diisi tim)",
        "MDS":   "?? (belum diisi tim)",
        "MDR":   "?? (belum diisi tim)",
        "AK1":   "?? (belum diisi tim)",
        "DUS":   "?? (belum diisi tim)",
        "GM9":   "?? (belum diisi tim)",
        "P9":    "?? (belum diisi tim)",
        "P3":    "?? (belum diisi tim)",
        "RO1":   "?? (belum diisi tim)",
        "WS":    "Wakaf Sarana",
        "RI":    "?? (belum diisi tim)",
    },
    "fakta_umum": {},
    "pending_questions": {},  # {kode: "pertanyaan yang ditanya bot"}
    "metadata": {
        "created_at": datetime.now().isoformat(),
        "last_updated": datetime.now().isoformat(),
        "total_updates": 0,
    }
}


def load() -> dict:
    """Load knowledge dari file, buat default kalau belum ada."""
    if not _KNOWLEDGE_FILE.exists():
        save(_DEFAULTS)
        return _DEFAULTS.copy()
    try:
        data = json.loads(_KNOWLEDGE_FILE.read_text(encoding='utf-8'))
        # Merge defaults untuk key yang belum ada
        for key, val in _DEFAULTS.items():
            if key not in data:
                data[key] = val
        return data
    except Exception as e:
        log.warning(f"Gagal load knowledge: {e}")
        return _DEFAULTS.copy()


def save(data: dict):
    """Simpan knowledge ke file."""
    try:
        data.setdefault("metadata", {})
        data["metadata"]["last_updated"] = datetime.now().isoformat()
        _KNOWLEDGE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )
        log.info("Knowledge disimpan.")
    except Exception as e:
        log.error(f"Gagal simpan knowledge: {e}")


def learn(key: str, value: str, category: str = "fakta_umum") -> str:
    """
    Simpan fakta baru ke knowledge base.
    category bisa: 'kode_program' atau 'fakta_umum'
    Returns pesan konfirmasi.
    """
    data = load()
    key = key.strip().upper() if category == "kode_program" else key.strip()
    value = value.strip()

    if category not in data:
        data[category] = {}

    old = data[category].get(key, None)
    data[category][key] = value
    data["metadata"]["total_updates"] = data["metadata"].get("total_updates", 0) + 1

    # Hapus dari pending questions kalau ada
    if key in data.get("pending_questions", {}):
        del data["pending_questions"][key]

    save(data)

    if old and old != value and "??" not in old:
        return f"✅ Oke, aku update! {key}: '{old}' → '{value}' 📝"
    return f"✅ Noted! {key} = {value} — aku catat ya 📝"


def add_pending_question(key: str, question: str):
    """Bot catat bahwa dia sudah tanya tentang 'key' ini."""
    data = load()
    data.setdefault("pending_questions", {})
    data["pending_questions"][key.upper()] = {
        "question": question,
        "asked_at": datetime.now().isoformat()
    }
    save(data)


def get_unknown_programs() -> list[str]:
    """Ambil list kode program yang belum diketahui (masih ??)."""
    data = load()
    return [
        k for k, v in data.get("kode_program", {}).items()
        if "??" in str(v)
    ]


def get_pending_questions() -> dict:
    """Ambil pertanyaan yang masih pending jawaban."""
    data = load()
    return data.get("pending_questions", {})


def to_context_string() -> str:
    """
    Format knowledge sebagai string untuk dimasukkan ke system prompt Gemini.
    """
    data = load()
    lines = ["=== PENGETAHUAN YANG DIPELAJARI TIM ==="]

    prog = data.get("kode_program", {})
    if prog:
        lines.append("\nKode Program GFI:")
        for k, v in sorted(prog.items()):
            if "??" not in v:
                lines.append(f"  {k} = {v}")
        unknown = [k for k, v in prog.items() if "??" in v]
        if unknown:
            lines.append(f"\n  [Belum diketahui: {', '.join(sorted(unknown))}]")

    fakta = data.get("fakta_umum", {})
    if fakta:
        lines.append("\nFakta lain:")
        for k, v in fakta.items():
            lines.append(f"  {k}: {v}")

    return "\n".join(lines)


def parse_learn_command(text: str) -> tuple[str | None, str | None, str]:
    """
    Parse command !belajar dari pesan WA.
    Format: !belajar: KEY = VALUE
    atau:   !belajar program: P1 = Yatim
    Returns: (key, value, category)
    """
    text = text.strip()
    # Cek apakah ini reply ke pertanyaan bot (format sederhana: kode = penjelasan)
    m = re.match(r'!belajar(?:\s+program)?[:\s]+(.+?)\s*=\s*(.+)', text, re.IGNORECASE)
    if m:
        key = m.group(1).strip()
        val = m.group(2).strip()
        # Heuristik: kalau key-nya kayak kode program (huruf kapital pendek) → kode_program
        category = "kode_program" if re.match(r'^[A-Z0-9]{1,8}$', key.upper()) else "fakta_umum"
        return key, val, category
    return None, None, "fakta_umum"


def check_reply_to_pending(text: str) -> tuple[str | None, str | None]:
    """
    Cek apakah pesan ini adalah jawaban atas pertanyaan pending bot.
    Return: (key, value) atau (None, None)
    """
    pending = get_pending_questions()
    text_upper = text.upper().strip()

    for key in pending:
        # Kalau reply mengandung kode yang ditanya
        if key in text_upper:
            # Coba parse "KEI = Kemiskinan" atau "KEI : Kemiskinan" atau "KEI Kemiskinan"
            m = re.search(
                rf'{re.escape(key)}\s*[=:]\s*(.+)',
                text, re.IGNORECASE
            )
            if m:
                return key, m.group(1).strip()
    return None, None
