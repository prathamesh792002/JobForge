"""Fix smart quote characters in tailor.py by replacing with unicode escapes."""
import re

path = "src/services/ai/tailor.py"

with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# Replace the problematic smart-quote replace block with unicode-escaped version
old_block = (
    '        # 3. Strip LaTeX-crashing Unicode characters injected by the LLM\n'
    '        updated_yaml = updated_yaml.replace(“”, \'"\')'
    '.replace(“”, \'"\')\n'
)

# Find the block by looking for the comment line and rewriting the whole section
lines = content.split("\n")
new_lines = []
i = 0
while i < len(lines):
    line = lines[i]
    if "Strip LaTeX-crashing Unicode characters injected by the LLM" in line:
        # Replace this comment + the next N lines with clean unicode-escape versions
        new_lines.append('        # 3. Strip LaTeX-crashing Unicode characters injected by the LLM')
        new_lines.append('        updated_yaml = updated_yaml.replace("\\u201c", \'"\').replace("\\u201d", \'"\')')
        new_lines.append('        updated_yaml = updated_yaml.replace("\\u2018", "\'").replace("\\u2019", "\'")')
        new_lines.append('        updated_yaml = updated_yaml.replace("\\u2014", "-").replace("\\u2013", "-")')
        new_lines.append('        updated_yaml = updated_yaml.replace("\\u2022", "").replace("\\u00b7", "")')
        new_lines.append('        updated_yaml = updated_yaml.replace("\\u2026", "...")')
        new_lines.append('        updated_yaml = updated_yaml.replace("\\u2192", "->").replace("\\u2190", "<-")')
        new_lines.append('        updated_yaml = updated_yaml.replace("\\u2191", "^").replace("\\u2193", "v")')
        new_lines.append('        # Final fallback: encode to ASCII, replacing anything still non-ASCII')
        new_lines.append('        updated_yaml = updated_yaml.encode("ascii", errors="replace").decode("ascii")')
        # Skip all old lines until we hit the "Final fallback" line or next block
        i += 1
        while i < len(lines) and "Final fallback" not in lines[i] and "encode" not in lines[i]:
            i += 1
        # Skip the fallback line too (we already wrote it)
        if i < len(lines) and ("Final fallback" in lines[i] or "encode" in lines[i]):
            i += 1
            if i < len(lines) and "encode" in lines[i]:
                i += 1
    else:
        new_lines.append(line)
        i += 1

new_content = "\n".join(new_lines)

with open(path, "w", encoding="utf-8") as f:
    f.write(new_content)

print("Done. Verifying syntax...")
import py_compile
try:
    py_compile.compile(path, doraise=True)
    print("Syntax OK.")
except py_compile.PyCompileError as e:
    print(f"Syntax error: {e}")
