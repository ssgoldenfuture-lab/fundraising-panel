"""
Test manual db_donatur_view.py — data FIKTIF bikinan sendiri, bukan data donatur asli.
Dijalankan langsung (python test_db_donatur_view_manual.py).
"""
import asyncio
import os
import sys
import tempfile

import aiosqlite

sys.path.insert(0, os.path.dirname(__file__))
from models import CREATE_SQL
import db_donatur_view as v

TEST_DB = os.path.join(tempfile.gettempdir(), "test_db_donatur_view.db")


def assert_eq(actual, expected, label):
    status = "OK  " if actual == expected else "GAGAL"
    print(f"[{status}] {label}: dapat={actual!r} harapan={expected!r}")
    if actual != expected:
        raise AssertionError(f"{label}: {actual!r} != {expected!r}")


async def seed(db):
    n = 0

    async def add(cs, label, jumlah, divisi="SSU"):
        nonlocal n
        for _ in range(jumlah):
            n += 1
            await db.execute(
                "INSERT INTO db_donatur (no_hp, nama_donatur, no_hp_cs, nama_label, divisi) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"628000{n:07d}", f"Donatur Fiktif {n}", cs, label, divisi),
            )

    # Rani 1: 3 baris label gabungan, 2 baris satu label, 2 baris "Database", 1 baris tanpa label (None)
    await add("Rani 1 628111", "DB FIX BC (pernah donasi) ~ DB QURBAN", 3)
    await add("Rani 1 628111", "DB FIX BC (pernah donasi)", 2)
    await add("Rani 1 628111", "Database", 2)
    await add("Rani 1 628111", None, 1)
    # Rani 2 & Rani 10 untuk tes urutan alami
    await add("Rani 2 628222", "Database", 2)
    await add("Rani 2 628222", "", 1)
    await add("Rani 10 628999", "Database", 1)
    # CS lain
    await add("Fitri 1 628333", "DB QURBAN", 2)
    # tanpa nomor CS (kosong dan NULL)
    await add("", "Database", 1)
    await add(None, "Database", 1)
    # label dengan karakter khusus (uji parameterisasi)
    await add("Rani 1 628111", "DB 100% 'aman' _x_", 2)
    # Rani 3: banyak baris untuk uji paginasi (120 baris)
    await add("Rani 3 628444", "Database", 120)
    await db.commit()


async def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    async with aiosqlite.connect(TEST_DB) as db:
        await db.executescript(CREATE_SQL)
        await seed(db)

    print("=== TEST 1: split_labels ===")
    assert_eq(v.split_labels("DB FIX BC (pernah donasi) ~ DB QURBAN"),
              ["DB FIX BC (pernah donasi)", "DB QURBAN"], "label gabungan dipecah & di-strip")
    assert_eq(v.split_labels("Database"), ["Database"], "label tunggal")
    assert_eq(v.split_labels(None), [], "None -> kosong")
    assert_eq(v.split_labels(""), [], "string kosong -> kosong")
    assert_eq(v.split_labels(" ~ "), [], "hanya pemisah -> kosong")
    assert_eq(v.split_labels("A~B ~ C"), ["A", "B", "C"], "pemisah tanpa/dengan spasi")
    print("-> split_labels: LOLOS\n")

    print("=== TEST 2: can_view — pagar akses ===")
    os.environ.pop("DB_VIEWERS", None)
    assert_eq(v.can_view(None), False, "belum login -> ditolak")
    assert_eq(v.can_view({"u": "admin", "r": "admin"}), True, "role admin -> boleh")
    assert_eq(v.can_view({"u": "mifipsb", "r": "staff"}), False,
              "staff biasa, DB_VIEWERS belum diisi -> DITOLAK (default aman)")
    os.environ["DB_VIEWERS"] = "mifipsb, Fitri"
    assert_eq(v.can_view({"u": "mifipsb", "r": "staff"}), True, "username di DB_VIEWERS -> boleh")
    assert_eq(v.can_view({"u": "FITRI", "r": "staff"}), True, "tidak peka kapital")
    assert_eq(v.can_view({"u": "staff", "r": "staff"}), False, "username lain tetap ditolak")
    os.environ.pop("DB_VIEWERS", None)
    print("-> can_view: LOLOS\n")

    async with aiosqlite.connect(TEST_DB) as db:
        print("=== TEST 3: opsi_cs — jumlah, urutan alami, tanpa nomor CS ===")
        opsi = await v.opsi_cs(db)
        urut = [o["nilai"] for o in opsi]
        assert_eq(urut.index("Rani 2 628222") < urut.index("Rani 3 628444") < urut.index("Rani 10 628999"),
                  True, "urutan alami: Rani 2 < Rani 3 < Rani 10")
        jumlah = {o["nilai"]: o["jumlah"] for o in opsi}
        assert_eq(jumlah["Rani 1 628111"], 10, "jumlah Rani 1 (8 + 2 label khusus)")
        assert_eq(jumlah["Rani 3 628444"], 120, "jumlah Rani 3")
        assert_eq(jumlah[v.TANPA_CS], 2, "kosong dan NULL digabung jadi 1 opsi 'tanpa nomor CS'")
        assert_eq(sum(jumlah.values()), 10 + 3 + 1 + 120 + 2 + 2, "jumlah semua opsi = total baris")
        print("-> opsi_cs: LOLOS\n")

        print("=== TEST 4: opsi_label — dependen pada nomor CS terpilih ===")
        lab_r1 = {o["nilai"]: o["jumlah"] for o in await v.opsi_label(db, "Rani 1 628111")}
        assert_eq(lab_r1["DB FIX BC (pernah donasi)"], 5, "label muncul dari baris gabungan (3) + tunggal (2)")
        assert_eq(lab_r1["DB QURBAN"], 3, "label dari baris gabungan")
        assert_eq(lab_r1["Database"], 2, "label Database di Rani 1")
        assert_eq(lab_r1[v.TANPA_LABEL], 1, "baris label NULL -> (tanpa label)")
        assert_eq("DB QURBAN" in {o["nilai"] for o in await v.opsi_label(db, "Rani 2 628222")}, False,
                  "Rani 2 TIDAK menawarkan label yang tidak dia punya")
        lab_r2 = {o["nilai"]: o["jumlah"] for o in await v.opsi_label(db, "Rani 2 628222")}
        assert_eq(lab_r2[v.TANPA_LABEL], 1, "label string kosong -> (tanpa label)")
        lab_semua = {o["nilai"] for o in await v.opsi_label(db, "")}
        assert_eq("DB QURBAN" in lab_semua and "Database" in lab_semua, True, "cs kosong -> label dari semua CS")
        print("-> opsi_label: LOLOS\n")

        print("=== TEST 5: siapkan_halaman — filter ===")
        h = await v.siapkan_halaman(db, "", "", "1")
        assert_eq(h["total"], 138, "tanpa filter -> semua baris")
        h = await v.siapkan_halaman(db, "Rani 1 628111", "", "1")
        assert_eq(h["total"], 10, "filter nomor CS saja")
        assert_eq(all(r["no_hp_cs"] == "Rani 1 628111" for r in h["rows"]), True, "semua baris milik CS terpilih")
        h = await v.siapkan_halaman(db, "Rani 1 628111", "DB QURBAN", "1")
        assert_eq(h["total"], 3, "CS + label (label ada di baris gabungan)")
        h = await v.siapkan_halaman(db, "Rani 1 628111", "DB FIX BC (pernah donasi)", "1")
        assert_eq(h["total"], 5, "label muncul di baris gabungan dan tunggal")
        h = await v.siapkan_halaman(db, "", "DB QURBAN", "1")
        assert_eq(h["total"], 5, "label saja, lintas CS (3 Rani + 2 Fitri)")
        h = await v.siapkan_halaman(db, "Rani 1 628111", v.TANPA_LABEL, "1")
        assert_eq(h["total"], 1, "filter (tanpa label) -> baris label NULL")
        h = await v.siapkan_halaman(db, "Rani 2 628222", v.TANPA_LABEL, "1")
        assert_eq(h["total"], 1, "filter (tanpa label) -> baris label string kosong")
        h = await v.siapkan_halaman(db, v.TANPA_CS, "", "1")
        assert_eq(h["total"], 2, "filter (tanpa nomor CS) -> kosong + NULL")
        print("-> filter: LOLOS\n")

        print("=== TEST 6: siapkan_halaman — paginasi ===")
        h1 = await v.siapkan_halaman(db, "Rani 3 628444", "", "1")
        assert_eq((h1["total"], len(h1["rows"]), h1["total_pages"]), (120, 50, 3), "halaman 1: 50 baris dari 3 halaman")
        assert_eq((h1["has_prev"], h1["has_next"]), (False, True), "halaman 1: ada next, tidak ada prev")
        assert_eq((h1["dari_fmt"], h1["sampai_fmt"]), ("1", "50"), "rentang tampilan halaman 1")
        h3 = await v.siapkan_halaman(db, "Rani 3 628444", "", "3")
        assert_eq((len(h3["rows"]), h3["has_next"], h3["has_prev"]), (20, False, True), "halaman 3: sisa 20 baris")
        assert_eq((h3["dari_fmt"], h3["sampai_fmt"]), ("101", "120"), "rentang tampilan halaman 3")
        ids = [r["id"] for r in h1["rows"]] + [r["id"] for r in (await v.siapkan_halaman(db, "Rani 3 628444", "", "2"))["rows"]] + [r["id"] for r in h3["rows"]]
        assert_eq((len(ids), len(set(ids))), (120, 120), "3 halaman = 120 baris, tanpa duplikat/terlewat")
        hx = await v.siapkan_halaman(db, "Rani 3 628444", "", "999")
        assert_eq(hx["page"], 3, "halaman melebihi batas -> dikunci ke halaman terakhir")
        for bad in ["abc", "-5", "0", "", None]:
            hb = await v.siapkan_halaman(db, "Rani 3 628444", "", bad)
            assert_eq(hb["page"], 1, f"nilai page tidak valid {bad!r} -> halaman 1")
        print("-> paginasi: LOLOS\n")

        print("=== TEST 7: konsistensi tampilan & input nakal ===")
        h = await v.siapkan_halaman(db, "CS yang tidak ada", "", "1")
        assert_eq((h["cs"], h["total"]), ("", 138), "nomor CS tidak dikenal diabaikan (bukan hasil kosong menyesatkan)")
        h = await v.siapkan_halaman(db, "Rani 2 628222", "DB QURBAN", "1")
        assert_eq((h["label"], h["total"]), ("", 3), "label yang tidak ada di CS itu diabaikan")
        h = await v.siapkan_halaman(db, "Rani 1 628111", "DB 100% 'aman' _x_", "1")
        assert_eq(h["total"], 2, "label berisi %, kutip, underscore cocok persis (tidak jadi wildcard)")
        h = await v.siapkan_halaman(db, "x' OR '1'='1", "", "1")
        assert_eq((h["cs"], h["total"]), ("", 138), "SQL injection di cs: diabaikan, bukan membocorkan/merusak")
        h = await v.siapkan_halaman(db, "Rani 1 628111", "x'; DROP TABLE db_donatur;--", "1")
        assert_eq(h["total"], 10, "SQL injection di label: diabaikan")
        async with db.execute("SELECT COUNT(*) FROM db_donatur") as cur:
            assert_eq((await cur.fetchone())[0], 138, "tabel utuh setelah semua percobaan injection")
        h = await v.siapkan_halaman(db, "Rani 1 628111", "DB QURBAN", "1")
        assert_eq("cs=Rani+1+628111" in h["qs_base"] and "label=DB+QURBAN" in h["qs_base"], True,
                  "qs_base untuk link paginasi ter-encode benar")
        assert_eq(v.fmt_id(1257), "1.257", "format ribuan gaya Indonesia")
        print("-> konsistensi & input nakal: LOLOS\n")

    os.remove(TEST_DB)
    print("=== SEMUA TEST LOLOS ===")


if __name__ == "__main__":
    asyncio.run(main())
