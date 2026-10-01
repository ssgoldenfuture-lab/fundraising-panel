"""
db_donatur_view.py — logika halaman lihat & filter Database Donatur (READ-ONLY).

Dipisah dari main.py supaya bisa ditest tanpa menyalakan server. Route di
main.py cuma membungkus fungsi-fungsi di sini.

Keputusan desain (hasil obrolan dengan user):
- Satu halaman, dua dropdown bertingkat: Nomor CS dulu, lalu Label. Kalau dua-duanya
  dibiarkan "Semua", tabel menampilkan semua db. Tidak perlu dua mode terpisah.
- Pilihan Label hanya menampilkan label yang BENAR-BENAR ada di nomor CS terpilih.
- Satu db boleh punya beberapa label, ditulis digabung dipisah "~", mis.
  "DB FIX BC (pernah donasi) ~ DB QURBAN". Dropdown memecahnya per label satuan,
  jadi baris itu muncul di filter "DB FIX BC (pernah donasi)" maupun "DB QURBAN".
- Tabel dipaginasi (PAGE_SIZE baris per halaman). Satu nomor CS bisa berisi ribuan db.

Keamanan:
- SEMUA nilai dari user (cs, label, page) dipakai sebagai parameter SQL, tidak pernah
  disambung ke string query. Daftar nilai nama_label untuk klausa IN dihitung dari data
  sendiri, bukan dari input user.
- can_view() adalah pagar akses untuk seluruh area /database. Halaman ini menampilkan
  nama + nomor HP donatur dalam jumlah besar, jadi tidak cukup hanya "sudah login".
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter
from urllib.parse import urlencode

import aiosqlite

PAGE_SIZE = 50
TANPA_CS = "(tanpa nomor CS)"
TANPA_LABEL = "(tanpa label)"


# ── Akses ────────────────────────────────────────────────────────────────────
def can_view(user: dict | None) -> bool:
    """
    Siapa yang boleh membuka area /database (lihat, input, antrian).
    - role admin: boleh
    - username yang tercantum di env DB_VIEWERS (dipisah koma, tidak peka kapital): boleh
    - selain itu: tidak. Default AMAN: kalau DB_VIEWERS tidak diisi, hanya admin.
    """
    if not user:
        return False
    if user.get("r") == "admin":
        return True
    viewers = {
        u.strip().lower() for u in os.getenv("DB_VIEWERS", "").split(",") if u.strip()
    }
    return str(user.get("u", "")).strip().lower() in viewers


# ── Utilitas kecil ───────────────────────────────────────────────────────────
def fmt_id(n: int) -> str:
    """1257 -> '1.257' (pemisah ribuan gaya Indonesia)."""
    return f"{n:,}".replace(",", ".")


def split_labels(nama_label: str | None) -> list[str]:
    """
    'DB FIX BC (pernah donasi) ~ DB QURBAN' -> ['DB FIX BC (pernah donasi)', 'DB QURBAN'].
    Kosong / None / hanya pemisah -> [].
    """
    if not nama_label:
        return []
    return [p.strip() for p in nama_label.split("~") if p.strip()]


def _natural_key(s: str):
    """Urutan alami: 'Rani 2' sebelum 'Rani 10' (bukan urutan abjad biasa)."""
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", s)]


def _parse_page(raw) -> int:
    try:
        return max(1, int(str(raw).strip()))
    except (TypeError, ValueError):
        return 1


def _where_cs(cs: str) -> tuple[str, list]:
    """Klausa SQL untuk filter nomor CS. cs kosong = tanpa filter."""
    if not cs:
        return "", []
    if cs == TANPA_CS:
        return "(no_hp_cs IS NULL OR no_hp_cs = '')", []
    return "no_hp_cs = ?", [cs]


# ── Opsi dropdown ────────────────────────────────────────────────────────────
async def opsi_cs(db: aiosqlite.Connection) -> list[dict]:
    """Semua nomor CS + jumlah db, urutan alami. db tanpa nomor CS digabung jadi satu opsi."""
    counter: Counter = Counter()
    async with db.execute(
        "SELECT no_hp_cs, COUNT(*) FROM db_donatur GROUP BY no_hp_cs"
    ) as cur:
        for no_hp_cs, c in await cur.fetchall():
            counter[no_hp_cs if no_hp_cs else TANPA_CS] += c
    nilai_urut = sorted(counter, key=_natural_key)
    return [{"nilai": n, "jumlah": counter[n], "jumlah_fmt": fmt_id(counter[n])} for n in nilai_urut]


async def opsi_label(db: aiosqlite.Connection, cs: str) -> list[dict]:
    """
    Label satuan yang ada di scope nomor CS terpilih (cs kosong = semua), + jumlah db.
    Satu db dengan beberapa label dihitung sekali di tiap labelnya, jadi jumlah per
    label bisa lebih besar dari total (normal).
    """
    cond, params = _where_cs(cs)
    sql = "SELECT nama_label, COUNT(*) FROM db_donatur"
    if cond:
        sql += " WHERE " + cond
    sql += " GROUP BY nama_label"
    counter: Counter = Counter()
    async with db.execute(sql, params) as cur:
        for raw, c in await cur.fetchall():
            tokens = set(split_labels(raw))
            if not tokens:
                counter[TANPA_LABEL] += c
            for t in tokens:
                counter[t] += c
    nilai_urut = sorted(counter, key=lambda s: s.casefold())
    return [{"nilai": n, "jumlah": counter[n], "jumlah_fmt": fmt_id(counter[n])} for n in nilai_urut]


async def _cond_label(
    db: aiosqlite.Connection, cs_cond: str, cs_params: list, label: str
) -> tuple[str, list]:
    """
    Klausa SQL untuk filter satu label satuan. Dicari dulu nilai nama_label MENTAH
    (distinct) di scope ini yang mengandung label tsb, lalu difilter dengan IN (...).
    Cara ini cocok persis dan aman dari karakter khusus (%, _, kutip) di nama label.
    """
    sql = "SELECT DISTINCT nama_label FROM db_donatur"
    if cs_cond:
        sql += " WHERE " + cs_cond
    async with db.execute(sql, cs_params) as cur:
        raws = [r[0] for r in await cur.fetchall()]

    if label == TANPA_LABEL:
        cocok = [r for r in raws if not split_labels(r)]
    else:
        cocok = [r for r in raws if label in split_labels(r)]

    non_null = [r for r in cocok if r is not None]
    ada_null = any(r is None for r in cocok)
    bagian: list[str] = []
    params: list = []
    if non_null:
        bagian.append("nama_label IN (%s)" % ",".join("?" * len(non_null)))
        params += non_null
    if ada_null:
        bagian.append("nama_label IS NULL")
    if not bagian:
        return "0", []  # tidak ada yang cocok -> hasil kosong, bukan error
    return "(" + " OR ".join(bagian) + ")", params


# ── Halaman ──────────────────────────────────────────────────────────────────
async def siapkan_halaman(
    db: aiosqlite.Connection, cs: str = "", label: str = "", page="1"
) -> dict:
    """
    Semua data yang dibutuhkan template database_data.html. `db.row_factory`
    tidak perlu di-set oleh pemanggil (fungsi ini memakai indeks kolom).
    Filter yang tidak ada di pilihan (mis. diketik manual di URL) diabaikan.
    """
    cs = (cs or "").strip()
    label = (label or "").strip()

    cs_options = await opsi_cs(db)
    total_semua = sum(o["jumlah"] for o in cs_options)
    if cs not in {o["nilai"] for o in cs_options}:
        cs = ""

    label_options = await opsi_label(db, cs)
    if label not in {o["nilai"] for o in label_options}:
        label = ""

    conds: list[str] = []
    params: list = []
    cs_cond, cs_params = _where_cs(cs)
    if cs_cond:
        conds.append(cs_cond)
        params += cs_params
    if label:
        l_cond, l_params = await _cond_label(db, cs_cond, cs_params, label)
        conds.append(l_cond)
        params += l_params
    where = (" WHERE " + " AND ".join(conds)) if conds else ""

    async with db.execute("SELECT COUNT(*) FROM db_donatur" + where, params) as cur:
        total = (await cur.fetchone())[0]

    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page_n = min(_parse_page(page), total_pages)
    offset = (page_n - 1) * PAGE_SIZE

    async with db.execute(
        "SELECT id, no_hp, panggilan, nama_donatur, no_hp_cs, nama_label, divisi "
        "FROM db_donatur" + where + " ORDER BY id ASC LIMIT ? OFFSET ?",
        params + [PAGE_SIZE, offset],
    ) as cur:
        kolom = ["id", "no_hp", "panggilan", "nama_donatur", "no_hp_cs", "nama_label", "divisi"]
        rows = [dict(zip(kolom, r)) for r in await cur.fetchall()]

    dari = offset + 1 if total else 0
    sampai = offset + len(rows)
    return {
        "cs": cs,
        "label": label,
        "cs_options": cs_options,
        "label_options": label_options,
        "total_semua_fmt": fmt_id(total_semua),
        "rows": rows,
        "total": total,
        "total_fmt": fmt_id(total),
        "dari_fmt": fmt_id(dari),
        "sampai_fmt": fmt_id(sampai),
        "page": page_n,
        "total_pages": total_pages,
        "total_pages_fmt": fmt_id(total_pages),
        "has_prev": page_n > 1,
        "has_next": page_n < total_pages,
        "qs_base": urlencode({"cs": cs, "label": label}),
    }
