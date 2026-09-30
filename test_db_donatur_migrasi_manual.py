"""
Test manual buat db_donatur_migrasi.py — pakai data contoh yang bentuknya
PERSIS kayak apa yang dibalikin Google Sheets API (list of list of string),
TANPA pernah manggil network sama sekali (pakai _raw_rows_override).
"""
import asyncio
import os
import sys

import aiosqlite

sys.path.insert(0, os.path.dirname(__file__))
from models import CREATE_SQL
from db_donatur_migrasi import cs_matches, parse_rows_for_cs, migrasi_cs

TEST_DB = "/tmp/test_db_donatur_migrasi.db"

# Simulasi persis kolom A-G DB MASTER, termasuk header yang HARUS dilewati
# (walau di sini nggak ada baris header karena fetch_sheet_rows sudah minta
# range A2:G — jadi raw_rows yang masuk ke parse_rows_for_cs itu memang
# sudah tanpa header).
RAW_ROWS_CONTOH = [
    # No HP, Panggilan, Nama Donatur, No HP CS, Nama Label, Duplikasi(diabaikan), Divisi
    ["6281111111101", "Kak", "Donatur Rani A", "Rani 1 628112380705", "DB FIX BC", "1", "SSU"],
    ["6281111111102", "Kak", "Donatur Rani B", "Rani 1 628112380705", "DB QURBAN", "1", "SSU"],
    ["6281111111103", "Kak", "Donatur Rani C", "Rani 2 628113333333", "", "1", "OWN"],   # Rani, divisi BEDA (kasus nyata)
    ["6281111111104", "Kak", "Donatur Faridah", "Faridah 1 628114444444", "", "1", "SSU"],  # BUKAN Rani
    ["", "Kak", "Nomor kosong, harus di-skip", "Rani 3 628115555555", "", "1", "SSU"],  # no_hp kosong
    ["6281111111105", "Kak", "No HP CS kosong", "", "", "1", "SSU"],  # no_hp_cs kosong -> ga match siapa2
    ["0811111111066", "Kak", "Format nomor leading-zero dipertahankan", "Rani 1 628112380705", "", "1", "SSU"],
]


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

    # Patch DB_PATH modul ke file test SEBELUM test manapun yang nyentuh DB —
    # db_donatur_migrasi.py defaultnya nunjuk ke fundraising.db asli di repo,
    # yang di sandbox ini belum tentu ada/punya tabelnya.
    import db_donatur_migrasi
    db_donatur_migrasi.DB_PATH = TEST_DB

    print("=== TEST 1: cs_matches — exact match token pertama, case-insensitive ===")
    assert_eq(cs_matches("Rani 1 628112380705", "Rani"), True, "match normal")
    assert_eq(cs_matches("rani 1 628112380705", "Rani"), True, "match case-insensitive")
    assert_eq(cs_matches("Faridah 1 628114444444", "Rani"), False, "CS lain tidak match")
    assert_eq(cs_matches("", "Rani"), False, "string kosong tidak match")
    assert_eq(cs_matches("Raniapaja 1 628", "Rani"), False, "substring TANPA spasi tidak match (bukan token utuh)")
    print("-> cs_matches: LOLOS\n")

    print("=== TEST 2: parse_rows_for_cs — filter + transform ===")
    cocok = parse_rows_for_cs(RAW_ROWS_CONTOH, "Rani")
    assert_eq(len(cocok), 4, "jumlah baris cocok untuk Rani (3 normal + 1 leading-zero, baris kosong & CS lain ke-skip)")
    no_hps = [r["no_hp"] for r in cocok]
    assert_eq("6281111111104" in no_hps, False, "baris Faridah tidak ikut ke-filter")
    assert_eq("0811111111066" in [r["no_hp"] for r in cocok], True, "leading zero DIPERTAHANKAN apa adanya, tidak hilang")
    divisi_set = {r["divisi"] for r in cocok}
    assert_eq(divisi_set, {"SSU", "OWN"}, "1 CS (Rani) bisa punya lebih dari 1 divisi dalam hasil filter")
    print("-> parse_rows_for_cs: LOLOS\n")

    print("=== TEST 3: migrasi_cs dry_run=True — TIDAK menulis apa-apa ke DB ===")
    result_dry = await migrasi_cs(
        spreadsheet_id="dummy", sheet_name="dummy", cs_name="Rani",
        dry_run=True, _raw_rows_override=RAW_ROWS_CONTOH,
    )
    assert_eq(result_dry["dry_run"], True, "flag dry_run")
    assert_eq(result_dry["masuk"], 4, "dry-run: 4 baris AKAN masuk")
    assert_eq(result_dry["skip_sudah_ada"], 0, "dry-run: belum ada yang skip (DB masih kosong)")
    async with aiosqlite.connect(TEST_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT COUNT(*) c FROM db_donatur") as cur:
            total_setelah_dryrun = (await cur.fetchone())["c"]
    assert_eq(total_setelah_dryrun, 0, "PENTING: dry-run beneran TIDAK nulis apa-apa ke db_donatur")
    os.remove(result_dry["log_file"])
    print("-> dry_run: LOLOS, DB tetap kosong setelahnya\n")

    print("=== TEST 4: migrasi_cs dry_run=False — beneran nulis ===")
    result_exec1 = await migrasi_cs(
        spreadsheet_id="dummy", sheet_name="dummy", cs_name="Rani",
        dry_run=False, _raw_rows_override=RAW_ROWS_CONTOH,
    )
    assert_eq(result_exec1["masuk"], 4, "eksekusi pertama: 4 baris masuk beneran")
    assert_eq(result_exec1["skip_sudah_ada"], 0, "eksekusi pertama: belum ada yang skip")
    async with aiosqlite.connect(TEST_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT COUNT(*) c FROM db_donatur") as cur:
            total = (await cur.fetchone())["c"]
        async with db.execute(
            "SELECT divisi FROM db_donatur WHERE no_hp='6281111111103'"
        ) as cur:
            divisi_c = (await cur.fetchone())["divisi"]
    assert_eq(total, 4, "4 row beneran ada di db_donatur setelah eksekusi")
    assert_eq(divisi_c, "OWN", "baris ke-3 Rani (divisi beda) tersimpan dengan divisi OWN, bukan ke-generalisir SSU")
    os.remove(result_exec1["log_file"])
    print("-> eksekusi pertama: LOLOS\n")

    print("=== TEST 5: migrasi_cs dijalankan ULANG (simulasi interupsi & lanjut) — harus idempotent ===")
    result_exec2 = await migrasi_cs(
        spreadsheet_id="dummy", sheet_name="dummy", cs_name="Rani",
        dry_run=False, _raw_rows_override=RAW_ROWS_CONTOH,
    )
    assert_eq(result_exec2["masuk"], 0, "eksekusi KEDUA: 0 baris baru masuk (semua udah ada)")
    assert_eq(result_exec2["skip_sudah_ada"], 4, "eksekusi KEDUA: semua 4 baris ke-skip otomatis (BUKAN error, BUKAN dobel)")
    async with aiosqlite.connect(TEST_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT COUNT(*) c FROM db_donatur") as cur:
            total_setelah_run_kedua = (await cur.fetchone())["c"]
    assert_eq(total_setelah_run_kedua, 4, "TOTAL TETAP 4 (tidak dobel) — inilah yang bikin proses ini aman diinterupsi & dilanjut")
    os.remove(result_exec2["log_file"])
    print("-> idempotency (jalan ulang setelah 'interupsi'): LOLOS\n")

    os.remove(TEST_DB)
    print("=== SEMUA TEST LOLOS ===")


if __name__ == "__main__":
    asyncio.run(main())
