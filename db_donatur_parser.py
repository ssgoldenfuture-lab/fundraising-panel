"""
db_donatur_parser.py — parsing & pemrosesan batch untuk fitur input massal
Database Donatur (Fase 1a).

Kontrak parsing (hasil obrolan langsung sama user, bukan asumsi):
- Satu baris = satu kontak, format "no_hp,sisanya" — dipisah di KOMA PERTAMA saja.
  Contoh sumber nyata (copas dari Sebaran, hasil pilih Label -> daftar Penerima):
      6282194654378,Hamba Allah
      6285254347775,+62 852-5434-7775
- no_hp = SATU-SATUNYA identifier/patokan. Kolom "sisanya" (nama_donatur) BOLEH
  BERISI APA SAJA — nama asli, "Hamba Allah", bahkan nomor HP lain yang
  diformat ulang WA (kayak baris kedua di contoh atas). Jangan divalidasi
  sebagai nama, terima apa adanya — user eksplisit bilang "patokannya kolom A".
- Baris kosong dilewati diam-diam (bukan error).
- Baris tanpa koma sama sekali dianggap error (gak bisa dipisah no_hp vs sisanya),
  masuk ke daftar errors, TIDAK menggagalkan baris lain di batch yang sama.
- Belum ada normalisasi format nomor (62/8/0) — itu masuk Fase 2, exact-match
  dulu di Fase 1a (niruin persis formula COUNTIF yang dipakai manual sekarang).
"""
from __future__ import annotations

import aiosqlite


def parse_paste_block(raw_text: str) -> tuple[list[dict], list[str]]:
    """
    Parse blok teks paste jadi list of dict {"no_hp": str, "nama_donatur": str}.

    Return (rows, errors). errors berisi pesan untuk baris yang gagal diparse —
    dikembalikan sebagai data, BUKAN exception, supaya satu baris rusak di
    tengah paste 30 baris gak bikin seluruh batch gagal diproses.
    """
    rows: list[dict] = []
    errors: list[str] = []

    for i, line in enumerate(raw_text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        if "," not in line:
            preview = line[:50]
            errors.append(f"Baris {i}: tidak ada koma, dilewati ({preview!r})")
            continue
        no_hp, _, sisanya = line.partition(",")
        no_hp = no_hp.strip()
        sisanya = sisanya.strip()
        if not no_hp:
            errors.append(f"Baris {i}: nomor HP kosong sebelum koma, dilewati")
            continue
        rows.append({"no_hp": no_hp, "nama_donatur": sisanya})

    return rows, errors


async def proses_batch(
    db: aiosqlite.Connection,
    rows: list[dict],
    no_hp_cs: str,
    divisi: str,
) -> dict:
    """
    Proses satu batch (hasil parse_paste_block) ke db_donatur + db_duplikat_antrian.

    Asumsi penting soal `db`:
    - `db.row_factory` HARUS sudah di-set ke `aiosqlite.Row` oleh caller
      (supaya `existing["id"]` bisa diakses by-name, bukan by-index).
    - Commit dipanggil PER BARIS (bukan sekali di akhir loop) — sengaja niru
      fix bug "database is locked" yang sudah kebukti di sheets.py, karena
      batch ini walau kecil (puluhan baris) tetap nulis ke tabel yang sama
      yang dibaca banyak route lain.

    Duplikat terhadap data LAMA dan duplikat ANTAR BARIS DALAM BATCH YANG SAMA
    ditangani dengan cara yang identik, tanpa case khusus: begitu sebuah no_hp
    sudah ada di db_donatur — entah itu dari sebelumnya, atau dari baris
    sebelumnya di batch ini yang barusan di-insert — baris berikutnya dengan
    no_hp yang sama masuk antrian review, TIDAK menimpa otomatis.

    Return: {"masuk": int, "antri": int}
    """
    masuk = 0
    antri = 0

    for row in rows:
        async with db.execute(
            "SELECT id FROM db_donatur WHERE no_hp = ?", (row["no_hp"],)
        ) as cur:
            existing = await cur.fetchone()

        if existing is None:
            await db.execute(
                "INSERT INTO db_donatur (no_hp, nama_donatur, no_hp_cs, divisi) "
                "VALUES (?, ?, ?, ?)",
                (row["no_hp"], row["nama_donatur"], no_hp_cs, divisi),
            )
            await db.commit()
            masuk += 1
        else:
            await db.execute(
                "INSERT INTO db_duplikat_antrian "
                "(no_hp, existing_id, nama_baru, no_hp_cs_baru, divisi_baru) "
                "VALUES (?, ?, ?, ?, ?)",
                (row["no_hp"], existing["id"], row["nama_donatur"], no_hp_cs, divisi),
            )
            await db.commit()
            antri += 1

    return {"masuk": masuk, "antri": antri}
