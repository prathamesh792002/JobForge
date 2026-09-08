"""Debug the export BytesIO construction and Telegram send_document behavior."""
import io, csv, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# --- Test 1: BytesIO position after creation ---
output = io.StringIO()
writer = csv.writer(output)
writer.writerow(["#", "Company", "Job Title"])
writer.writerow([1, "TestCorp", "ML Engineer"])

csv_str = "﻿" + output.getvalue()     # BOM via escape (safe)
encoded  = csv_str.encode("utf-8")
bytes_io = io.BytesIO(encoded)

print(f"[TEST 1] Position after BytesIO(...) creation : {bytes_io.tell()}")
print(f"[TEST 1] Total encoded bytes                  : {len(encoded)}")
first5 = bytes_io.read(5)
print(f"[TEST 1] First 5 bytes (BOM = ef bb bf...)    : {first5.hex()}")
print(f"[TEST 1] Position after read(5)               : {bytes_io.tell()}")
bytes_io.seek(0)
all_data = bytes_io.read()
print(f"[TEST 1] After seek(0) — full read bytes      : {len(all_data)}")
print()

# --- Test 2: What python-telegram-bot actually does ---
# PTB reads from the stream without seeking first; confirm position=0 is needed
bytes_io2 = io.BytesIO(encoded)
print(f"[TEST 2] New BytesIO position                 : {bytes_io2.tell()}")
# Simulate what PTB does internally — reads from current position
content_read = bytes_io2.read()
print(f"[TEST 2] PTB-style read from pos=0            : {len(content_read)} bytes  ✓")

# Test what happens if position is NOT at 0 (simulate a bug scenario)
bytes_io3 = io.BytesIO(encoded)
bytes_io3.seek(0, 2)   # seek to end
print(f"[TEST 3] After seek-to-end, position          : {bytes_io3.tell()}")
content_from_end = bytes_io3.read()
print(f"[TEST 3] Read from end                        : {len(content_from_end)} bytes  (0 = EMPTY = bug!)")
print()
print("=== CONCLUSION ===")
print("BytesIO created with data starts at pos=0 — safe without seek(0).")
print("But if anything moves the cursor before send_document, file appears empty in Telegram.")
