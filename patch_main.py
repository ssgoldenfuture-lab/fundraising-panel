#!/usr/bin/env python3
"""Patch main.py: tambah open_session setelah bot reply berhasil.

Usage: python patch_main.py [path/ke/main.py]
Kalau path tidak diisi, pakai ./main.py di direktori saat ini.
"""
import re
import sys

path = sys.argv[1] if len(sys.argv) > 1 else 'main.py'
with open(path) as f:
    content = f.read()

# Cari blok reply_message dan tambah open_session setelah itu
old_block = """            result = await loop.run_in_executor(
                None, lambda: wa_bot.reply_message(sender, reply_text, is_group=is_group)
            )
            await _log_wa_broadcast"""

new_block = """            result = await loop.run_in_executor(
                None, lambda: wa_bot.reply_message(sender, reply_text, is_group=is_group)
            )
            if is_group:
                wa_webhook.open_session(sender)  # buka sesi 5 menit utk follow-up tanpa tag
            await _log_wa_broadcast"""

if old_block in content:
    content = content.replace(old_block, new_block)
    with open(path, 'w') as f:
        f.write(content)
    print('PATCHED OK: open_session added')
else:
    # Fallback: cari pola lebih longgar
    idx = content.find('wa_bot.reply_message(sender')
    print(f'Pattern not found. Context around reply_message: {repr(content[max(0,idx-100):idx+200])}')
