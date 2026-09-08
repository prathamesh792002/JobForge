# RenderCV Custom Theme Skill

## Overview
RenderCV uses Jinja2 templates with custom delimiters to generate LaTeX resumes.

## Key Conventions
- **Variable output**: `<<variable>>` (not `{{ }}`)
- **Block tags**: `((* for item in list *))` and `((* endfor *))`
- **Conditionals**: `((* if condition *))` and `((* endif *))`

## Template Files
- `Preamble.j2.tex` — Document class, packages, global settings
- `Header.j2.tex` — Name, contact info layout
- `ExperienceEntry.j2.tex` — Single work experience entry
- `EducationEntry.j2.tex` — Single education entry
- `SectionBeginning.j2.tex` — Section heading
- `SectionEnding.j2.tex` — Section footer/spacing
- `theme.py` — ThemeOptions Pydantic model

## Usage
```bash
rendercv render resume.yaml --theme-dir path/to/my_custom_theme --output-folder-name output
```

## Important
- ONLY use RenderCV for PDF generation. No WeasyPrint, pdfkit, etc.
- Requires TinyTeX with appropriate LaTeX packages installed.
