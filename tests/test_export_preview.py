"""Preview the CSV export output against real DB records."""
import asyncio, sys, os, io, csv
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime
from src.database.session import async_session_factory
from src.services.repository.application_repo import get_all_applications_for_export

async def main():
    async with async_session_factory() as db:
        apps = await get_all_applications_for_export(db)

    print(f"Total records: {len(apps)}\n")

    if not apps:
        print("No records yet.")
        return

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "#", "Company", "Job Title", "Persona", "Applied On",
        "Status", "HR Email", "ATS Score (Initial)", "ATS Score (Final)",
        "Tailoring Skipped", "Email Opened", "Opened On", "Source",
    ])

    for i, app in enumerate(apps, 1):
        applied_on = app.created_at.strftime("%d-%b-%Y") if app.created_at else ""
        opened_on  = app.opened_at.strftime("%d-%b-%Y")  if app.opened_at  else ""
        writer.writerow([
            i,
            app.company_name    or "",
            app.job_title       or "",
            app.persona_used    or "",
            applied_on,
            (app.status or "").capitalize(),
            app.hr_email        or "",
            f"{app.initial_ats_score}%" if app.initial_ats_score is not None else "N/A",
            f"{app.final_ats_score}%"   if app.final_ats_score   is not None else "N/A",
            "Skipped" if app.tailoring_skipped else "Done",
            "Yes" if app.opened_at else "No",
            opened_on,
            app.source_type     or "",
        ])

    # Print as a readable table
    output.seek(0)
    rows = list(csv.reader(output))
    col_widths = [max(len(r[c]) for r in rows) for c in range(len(rows[0]))]

    for r_idx, row in enumerate(rows):
        print("  " + "  |  ".join(cell.ljust(col_widths[c]) for c, cell in enumerate(row)))
        if r_idx == 0:
            print("  " + "-+-".join("-" * w for w in col_widths))

asyncio.run(main())
