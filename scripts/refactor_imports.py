import os

src_dir = 'src'
for root, _, files in os.walk(src_dir):
    for file in files:
        if file.endswith('.py'):
            filepath = os.path.join(root, file)
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            
            new_content = content.replace('from app.', 'from src.').replace('import app.', 'import src.')
            if 'from app ' in new_content:
                new_content = new_content.replace('from app ', 'from src ')
            
            if new_content != content:
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                print(f'Updated {filepath}')
