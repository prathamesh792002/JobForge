import os

with open('src/controllers/bot/handlers.py', 'r', encoding='utf-8') as f:
    content = f.read()

# I will write the split files manually to avoid regex complications.
# I'll just leave this command to clear the output and do it via write_to_file.
