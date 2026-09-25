# Changelog

## v0.3.0 — 2026-09-25
- Database Donatur (Fase 1a): input massal via paste (format `no_hp,apa saja`), satu No HP CS + Divisi berlaku per-batch
- Dedup exact-match otomatis (niru formula COUNTIF manual) — db bentrok masuk antrian review, tidak menimpa data lama otomatis
- Antrian review: pilih pertahankan data lama / pakai data baru / lewati
- Tabel baru: `db_donatur` (gabungan DB MASTER + per-CS lama), `db_duplikat_antrian`
- Catatan: migrasi data ASLI dari Google Sheets ke tabel ini belum jalan (Fase 1b, menyusul terpisah) — tabel masih kosong sampai itu selesai

## v0.2.0 — 2026-09-16
- Tambah 2 kategori FAQ: Broadcast WhatsApp, Database Donatur
- Sidebar /faq: smooth scroll ke section
- Footer /faq: penanda visual lebih jelas

## v0.1.0 — 2026-09-16
- FAQ & Kotak Tanya: submit publik, triase staff, tampilan publik (PR #1)
- Kotak Tanya lengkap: field topik/saran, tahap konfirmasi, Kelola FAQ (PR #2)
- Fix conflict marker git yang bikin app tidak bisa start (PR #3)
- Review FAQ: kategori jadi field bersama, tab status, field catatan/konteks
