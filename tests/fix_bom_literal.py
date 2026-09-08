"""Replace literal BOM U+FEFF character in dashboard.py with the escape sequence."""
path = "src/controllers/bot/dashboard.py"

with open(path, "r", encoding="utf-8") as f:
    text = f.read()

# The literal BOM char U+FEFF inside the string: "﻿" written as actual char
BOM_LITERAL = "\"﻿\""        # the actual 3-char sequence: " + U+FEFF + "
BOM_ESCAPE  = '"' + r'﻿' + '"'  # the escaped form: "﻿"

if BOM_LITERAL in text:
    text = text.replace(BOM_LITERAL, BOM_ESCAPE)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"Replaced literal BOM with escape sequence in {path}")
else:
    print("Literal BOM character not found — already escaped or absent.")

# Verify syntax
import py_compile
try:
    py_compile.compile(path, doraise=True)
    print("Syntax OK.")
except py_compile.PyCompileError as e:
    print(f"Syntax error after edit: {e}")
