"""
PDF Compiler Service

Compiles a tailored YAML resume into a PDF using RenderCV CLI
as an async subprocess. Uses the custom LaTeX theme from
app/templates/my_custom_theme/.

Pipeline position: C5 — called after tailoring, before HITL approval.
"""

import asyncio
import logging
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Shrink-to-fit density presets ────────────────────────────────
# If a compiled resume still spills onto a 2nd page, the Preamble's geometry /
# leading / spacing is progressively tightened using these presets (in order)
# and re-compiled until it fits on ONE page. Content is never trimmed and the
# font never drops below 10pt, so nothing is lost and ATS parsing is unaffected.
# The baseline in Preamble.j2.tex is roughly "L1"; these escalate past it.
_DENSITY_PRESETS = [
    # margin(in), leading, section(before/after pt), sub itemize, list itemize.
    # Font stays 10pt at every level — the `article` class only accepts 10/11/12pt
    # and sub-10pt would need packages that may be absent from TinyTeX. Genuinely
    # content-heavy resumes that still overflow here are a *content* problem for the
    # tailor to trim, not a layout one.
    dict(font=10, margin=0.42, leading=0.99, sec_b=5, sec_a=3, sub_top=2, sub_item=2, list_top=2, list_item=1),
    dict(font=10, margin=0.40, leading=0.98, sec_b=5, sec_a=2, sub_top=2, sub_item=1, list_top=1, list_item=1),
    dict(font=10, margin=0.40, leading=0.96, sec_b=4, sec_a=2, sub_top=1, sub_item=1, list_top=1, list_item=1),
]


def _apply_density(preamble_text: str, p: dict) -> str:
    """Rewrite the tunable geometry/spacing values in a Preamble.j2.tex string."""
    t = preamble_text
    t = re.sub(r"\\documentclass\[letterpaper,[\d.]+pt\]",
               f"\\\\documentclass[letterpaper,{p['font']}pt]", t)
    t = re.sub(r"margin=[\d.]+in", f"margin={p['margin']}in", t)
    t = re.sub(r"\\linespread\{[\d.]+\}", f"\\\\linespread{{{p['leading']}}}", t)
    t = re.sub(r"(\\titlespacing\*\{\\section\}\{0pt\})\{[\d.]+pt\}\{[\d.]+pt\}",
               f"\\g<1>{{{p['sec_b']}pt}}{{{p['sec_a']}pt}}", t)
    t = re.sub(r"(leftmargin=0in,label=\{\},)topsep=[\d.]+pt,parsep=0pt,itemsep=[\d.]+pt",
               f"\\g<1>topsep={p['sub_top']}pt,parsep=0pt,itemsep={p['sub_item']}pt", t)
    t = re.sub(r"(leftmargin=0\.2in,)topsep=[\d.]+pt,itemsep=[\d.]+pt",
               f"\\g<1>topsep={p['list_top']}pt,itemsep={p['list_item']}pt", t)
    return t


def _count_pdf_pages(pdf_path: str) -> Optional[int]:
    """Return the page count of a PDF, or None if it can't be determined."""
    try:
        import fitz  # PyMuPDF
        with fitz.open(pdf_path) as doc:
            return doc.page_count
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Could not read page count (%s); skipping one-page enforcement.", e)
        return None

# Path to the custom LaTeX theme directory
# src/services/document/compiler.py → parent.parent.parent = src/
THEME_DIR = Path(__file__).parent.parent.parent / "resources" / "templates" / "mycustomtheme"

# Absolute path to rendercv inside the current venv — avoids PATH lookup failures
# sys.executable is e.g. E:\...\venv\Scripts\python.exe  (Windows)
#                     or /path/to/venv/bin/python         (Linux/macOS)
if sys.platform == "win32":
    # Two layouts exist on Windows:
    #   venv  → python.exe is inside Scripts\  → rendercv.exe is a sibling
    #   system Python → python.exe is in root  → rendercv.exe is in Scripts\
    _py_dir = Path(sys.executable).parent
    _next_to_py   = _py_dir / "rendercv.exe"           # venv layout
    _scripts_subdir = _py_dir / "Scripts" / "rendercv.exe"  # system Python layout
    RENDERCV_BIN = str(_next_to_py if _next_to_py.exists() else _scripts_subdir)
else:
    RENDERCV_BIN = str(Path(sys.executable).parent / "rendercv")


async def _render_once(cmd: list, output_dir: Path, yaml_path_obj: Path) -> str:
    """Run RenderCV once and return the path to the produced PDF."""
    # Run synchronously in a thread to avoid NotImplementedError on Windows
    # when Uvicorn forces the WindowsSelectorEventLoop.
    # shell=False so Windows finds the exe via its absolute path (no PATH needed).
    import subprocess
    def _run_rendercv():
        return subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    process = await asyncio.to_thread(_run_rendercv)

    stdout_str = process.stdout if process.stdout else ""
    stderr_str = process.stderr if process.stderr else ""

    if stdout_str:
        logger.debug("RenderCV stdout: %s", stdout_str[:500])
    if stderr_str:
        logger.debug("RenderCV stderr: %s", stderr_str[:500])

    if process.returncode != 0:
        logger.error(
            "RenderCV compilation failed (exit code %d):\nstdout: %s\nstderr: %s",
            process.returncode, stdout_str[:500], stderr_str[:500],
        )
        raise CompilationError(
            f"RenderCV compilation failed (exit code {process.returncode}):\n"
            f"{stderr_str[:300]}"
        )

    pdf_files = list(output_dir.rglob("*.pdf"))
    if not pdf_files:
        # RenderCV sometimes outputs to the parent directory
        pdf_files = list(yaml_path_obj.parent.rglob("*.pdf"))
    if not pdf_files:
        raise CompilationError(
            "RenderCV produced no PDF output. "
            "Check that TinyTeX is installed with required packages."
        )
    return str(pdf_files[0])


async def compile_pdf(
    yaml_path: str,
    use_custom_theme: bool = False,
    enforce_one_page: bool = True,
) -> str:
    """
    Compile a YAML resume to PDF using RenderCV.

    Runs RenderCV as an async subprocess. Optionally uses the custom
    LaTeX theme. If the custom theme fails, falls back to a built-in theme.

    When ``enforce_one_page`` is True and the custom theme is used, a
    shrink-to-fit pass progressively tightens the layout (margins, leading,
    spacing — never the 10pt font, never the content) and recompiles until the
    resume fits on a single page. Requires PyMuPDF to read the page count; if
    unavailable, enforcement is skipped gracefully.

    Args:
        yaml_path: Absolute path to the resume YAML file.
        use_custom_theme: If True, use the custom LaTeX theme.
        enforce_one_page: If True, shrink-to-fit until the resume is one page.

    Returns:
        Absolute path to the generated PDF file.

    Raises:
        CompilationError: If RenderCV fails to produce a PDF.
    """
    yaml_path_obj = Path(yaml_path)
    if not yaml_path_obj.exists():
        raise CompilationError(f"YAML file not found: {yaml_path}")

    output_dir = yaml_path_obj.parent / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    dest_theme_dir: Optional[Path] = None
    if use_custom_theme and THEME_DIR.exists():
        dest_theme_dir = yaml_path_obj.parent / THEME_DIR.name
        if dest_theme_dir.exists():
            shutil.rmtree(dest_theme_dir)
        try:
            shutil.copytree(THEME_DIR, dest_theme_dir)
        except Exception as e:
            logger.warning("Failed to copy custom theme, falling back to default theme: %s", str(e))
            use_custom_theme = False
            dest_theme_dir = None

    # Use the absolute path to rendercv inside the venv.
    cmd = [
        RENDERCV_BIN, "render", str(yaml_path_obj),
        "--dont-generate-markdown",
        "--dont-generate-html",
        "--dont-generate-png",
        "--output-folder-name", str(output_dir),
    ]

    logger.info("Compiling PDF with RenderCV...")
    logger.debug("Command: %s", cmd)

    pdf_path = await _render_once(cmd, output_dir, yaml_path_obj)
    logger.info("PDF compiled successfully: %s", pdf_path)

    # ── Shrink-to-fit: guarantee a single page ───────────────────
    if enforce_one_page and use_custom_theme and dest_theme_dir is not None:
        pages = _count_pdf_pages(pdf_path)
        if pages is not None and pages > 1:
            preamble_file = dest_theme_dir / "Preamble.j2.tex"
            baseline = preamble_file.read_text(encoding="utf-8")
            for i, preset in enumerate(_DENSITY_PRESETS, start=1):
                logger.info(
                    "Resume is %d pages — applying shrink-to-fit level %d/%d...",
                    pages, i, len(_DENSITY_PRESETS),
                )
                preamble_file.write_text(_apply_density(baseline, preset), encoding="utf-8")
                try:
                    pdf_path = await _render_once(cmd, output_dir, yaml_path_obj)
                except CompilationError:
                    # A tightened preset failed to compile — restore & stop escalating.
                    preamble_file.write_text(baseline, encoding="utf-8")
                    logger.warning("Shrink-to-fit level %d failed to compile; keeping prior PDF.", i)
                    break
                pages = _count_pdf_pages(pdf_path)
                if pages is None or pages <= 1:
                    logger.info("Shrink-to-fit succeeded at level %d (%s page).", i, pages)
                    break
            else:
                logger.warning(
                    "Resume still exceeds one page after all shrink-to-fit levels; "
                    "content may be too long to fit at 10pt."
                )

    return pdf_path


def cleanup_temp_dir(path: str) -> None:
    """
    Clean up the jobforge_ temporary directory for a pipeline run.

    Accepts either the YAML path (.../jobforge_*/resume.yaml) or the PDF
    path (.../jobforge_*/output/resume.pdf) — walks up until it finds the
    jobforge_ root directory and removes the whole tree.
    """
    candidate = Path(path) if Path(path).is_dir() else Path(path).parent
    # Walk up until we find the jobforge_ temp root or hit the filesystem root
    while candidate.parent != candidate:
        if "jobforge_" in candidate.name:
            break
        candidate = candidate.parent

    if candidate.exists() and "jobforge_" in candidate.name:
        try:
            shutil.rmtree(str(candidate))
            logger.info("Cleaned up temp directory: %s", candidate)
        except Exception as e:
            logger.warning("Failed to clean up temp dir %s: %s", candidate, str(e))
    else:
        logger.debug("No jobforge_ temp dir found for path: %s", path)


# ── Custom Exception ──────────────────────────────────────────

class CompilationError(Exception):
    """Raised when RenderCV PDF compilation fails."""
    pass
