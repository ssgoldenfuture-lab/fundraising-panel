"""
Test manual buat db_donatur_parser.py — pakai data contoh bikinan sendiri,
BUKAN data donatur asli. Dijalankan langsung (bukan pytest) biar gampang
diliat hasilnya satu-satu.
"""
import asyncio
import os
import sys

import aiosqlite

sys.path.insert(0, os.path.dirname(__file__))
from models import CREATE_SQL
from db_donatur_parser import parse_paste_block, proses_batch

TEST_DB = "/tmp/test_db_donatur.db"


def assert_eq(actual, expected, label):
    status = "OK  " if actual == expected else "GAGAL"
    print(f"[{status}] {label}: dapat={actual!r} harapan={expected!r}")
    if actual != expected:
        raise AssertionError(f"{label}: {actual!r} != {expected!r}")


async def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)

    async with aiosqlite.connect(TEST_DB) as db:
        await db.executescript(CREATE_SQL)
        await db.commit()

    print("=== TEST 1: parse_paste_block — kasus normal + edge case ===")
    raw = (
        "6282194654378,Hamba Allah\n"
        "6282183615098,Desy Tri Ferawati\n"
        "\n"                                    # baris kosong -> dilewati
        "6285263830413,Ildefniza\n"
        "6285254347775,+62 852-5434-7775\n"      # nama = nomor lain (kasus nyata dari user)
        "nomorrusak-tanpa-koma\n"                # error: tanpa koma
        "  6281918259367 , Kodri  \n"             # spasi berlebih, harus di-strip
    )
    rows, errors = parse_paste_block(raw)
    assert_eq(len(rows), 5, "jumlah rows valid")
    assert_eq(len(errors), 1, "jumlah errors")
    assert_eq(rows[3]["no_hp"], "6285254347775", "no_hp baris nama=nomor lain")
    assert_eq(rows[3]["nama_donatur"], "+62 852-5434-7775", "nama_donatur boleh berisi nomor lain")
    assert_eq(rows[4]["no_hp"], "6281918259367", "no_hp ke-strip dari spasi berlebih")
    assert_eq(rows[4]["nama_donatur"], "Kodri", "nama ke-strip dari spasi berlebih")
    assert "tidak ada koma" in errors[0]
    print("-> parse_paste_block: SEMUA LOLOS\n")

    print("=== TEST 2: proses_batch — batch pertama, semua db baru (belum ada apa-apa) ===")
    async with aiosqlite.connect(TEST_DB) as db:
        db.row_factory = aiosqlite.Row
        result = await proses_batch(db, rows, no_hp_cs="Annisa 1 6281111111111", divisi="OWN")
    assert_eq(result, {"masuk": 5, "antri": 0}, "hasil batch pertama (semua baru)")
    print("-> proses_batch (batch baru): LOLOS\n")

    print("=== TEST 3: proses_batch — batch kedua, ada duplikat vs data LAMA ===")
    raw2 = (
        "6282194654378,Nama Ganti Duplikat Lama\n"   # duplikat vs data test 2 (row lama)
        "6289999999999,Donatur Baru Beneran\n"        # benar-benar baru
    )
    rows2, errors2 = parse_paste_block(raw2)
    assert_eq(len(errors2), 0, "tidak ada error di batch kedua")
    async with aiosqlite.connect(TEST_DB) as db:
        db.row_factory = aiosqlite.Row
        result2 = await proses_batch(db, rows2, no_hp_cs="Fitri 3 6282222222222", divisi="SS")
    assert_eq(result2, {"masuk": 1, "antri": 1}, "hasil batch kedua (1 baru, 1 duplikat vs lama)")

    async with aiosqlite.connect(TEST_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT COUNT(*) c FROM db_donatur") as cur:
            total_donatur = (await cur.fetchone())["c"]
        async with db.execute("SELECT COUNT(*) c FROM db_duplikat_antrian WHERE status='pending'") as cur:
            total_antrian = (await cur.fetchone())["c"]
        async with db.execute(
            "SELECT nama_donatur FROM db_donatur WHERE no_hp='6282194654378'"
        ) as cur:
            baris_lama = await cur.fetchone()
    assert_eq(total_donatur, 6, "total row db_donatur (5 + 1 baru, duplikat TIDAK nambah row)")
    assert_eq(total_antrian, 1, "total antrian pending")
    assert_eq(baris_lama["nama_donatur"], "Hamba Allah", "data lama TIDAK tertimpa otomatis saat duplikat")
    print("-> proses_batch (duplikat vs lama): LOLOS — data lama aman, tidak ke-overwrite otomatis\n")

    print("=== TEST 4: proses_batch — duplikat ANTAR BARIS DALAM SATU BATCH YANG SAMA ===")
    raw3 = (
        "6281000000001,Kontak Pertama\n"
        "6281000000001,Kontak Kedua Nomor Sama\n"   # duplikat internal batch, no_hp sama persis
        "6281000000002,Kontak Ketiga\n"
    )
    rows3, _ = parse_paste_block(raw3)
    async with aiosqlite.connect(TEST_DB) as db:
        db.row_factory = aiosqlite.Row
        result3 = await proses_batch(db, rows3, no_hp_cs="Rani 2 6283333333333", divisi="Paid Traffic")
    assert_eq(result3, {"masuk": 2, "antri": 1}, "baris ke-2 (no_hp sama dgn baris ke-1 di batch sendiri) masuk antrian, bukan ke-insert dobel")

    async with aiosqlite.connect(TEST_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT COUNT(*) c FROM db_donatur WHERE no_hp='6281000000001'"
        ) as cur:
            jumlah_row_nomor_sama = (await cur.fetchone())["c"]
    assert_eq(jumlah_row_nomor_sama, 1, "UNIQUE constraint: no_hp yang sama TIDAK PERNAH jadi 2 row di db_donatur")
    print("-> proses_batch (duplikat dalam batch sendiri): LOLOS — tertangkap tanpa case khusus\n")

    print("=== TEST 5: constraint UNIQUE no_hp ditegakkan di level DB, bukan cuma di logic aplikasi ===")
    async with aiosqlite.connect(TEST_DB) as db:
        gagal_seperti_harapan = False
        try:
            await db.execute(
                "INSERT INTO db_donatur (no_hp, nama_donatur) VALUES (?, ?)",
                ("6281000000001", "Coba insert langsung skip proses_batch"),
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            gagal_seperti_harapan = True
    assert_eq(gagal_seperti_harapan, True, "INSERT langsung yang skip proses_batch tetap ditolak DB (safety net)")
    print("-> UNIQUE constraint level DB: LOLOS\n")

    os.remove(TEST_DB)
    print("=== SEMUA TEST LOLOS ===")


if __name__ == "__main__":
    asyncio.run(main())
