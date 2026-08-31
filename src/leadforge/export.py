"""Export (U6.1-6.3): styled XLSX (Leads/Summary/About) + CSV mirror + report.json (docs/03 §5).

openpyxl only (pure-python). CSV is utf-8-sig so Excel on Windows opens it clean. Never prints data rows.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from leadforge import db
from leadforge.models import ICP
from leadforge.util import natural_name, now_iso

COLUMNS = [
    "Score", "Tier", "Business", "Category", "DM Name", "DM Title", "DM Conf", "Phone",
    "Email", "Email Tier", "Website", "Address", "City", "Region", "Postal", "Country",
    "Rating", "Reviews", "Likely Need (Hook)", "Why This Score", "Maps", "Source", "Verified On", "Stale?",
    "Opening Hours", "Company No", "Incorporated", "Company Status", "SIC Codes", "Call Readiness",
]
# account_fit profile (WE SCORE spec) appends the account-intel columns
ACCOUNT_COLUMNS = COLUMNS + [
    "Employees", "Employee Range", "Revenue", "Departments", "Microsoft 365", "CRM", "ERP",
    "Other Systems", "Trigger", "Trigger Strength", "LinkedIn", "Contactability", "Data Confidence",
    "Status",
]
_TIER_FILL = {
    "A": PatternFill("solid", fgColor="C6EFCE"),
    "B": PatternFill("solid", fgColor="FFEB9C"),
    "C": PatternFill("solid", fgColor="FCE4D6"),
    "D": PatternFill("solid", fgColor="E7E6E6"),
    "DQ": PatternFill("solid", fgColor="D9D9D9"),
}
_HEADER_FILL = PatternFill("solid", fgColor="1F3864")
_HEADER_FONT = Font(color="FFFFFF", bold=True)
_REGION_REMINDER = {
    "us": "US/CAN-SPAM: identify yourself, include a physical postal address + working opt-out; honor within 10 business days.",
    "uk": "UK/PECR: corporate subscribers may be emailed B2B; sole traders/individuals need consent. Always give identity + opt-out.",
    "eu": "EU/GDPR: legitimate-interest basis; keep the campaign LIA note; honor objections immediately; store source per contact.",
}


def _display_phone(e164: str | None) -> str:
    """"+44 1483 456363", not "+441483456363": Excel coerces a leading-+ digit run to a number
    (4.41483E+11); the spaced international format survives as text in both XLSX and CSV."""
    if not e164:
        return ""
    try:
        import phonenumbers
        return phonenumbers.format_number(phonenumbers.parse(e164, None),
                                          phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    except Exception:  # noqa: BLE001 — display formatting must never lose the number
        return e164


def _row_for(conn: sqlite3.Connection, s, staleness_days: int = 90) -> dict:
    people = db.people_for(conn, s["business_id"])
    dm = next((p for p in people if p["is_dm"] == 1), None)
    contacts = db.contacts_for(conn, s["business_id"])
    emails = [(c["value"], c["tier"]) for c in contacts if c["kind"] == "email" and c["tier"] != "invalid"]
    order = {"valid": 0, "role": 1, "risky": 2, "catch_all": 3, "unknown": 4}
    emails.sort(key=lambda e: order.get(e[1], 9))
    best_email = emails[0] if emails else ("", "")
    factors = json.loads(s["factors_json"])
    top_why = "; ".join(f["why"] for f in sorted(factors, key=lambda f: -f["points"])[:3])
    hooks = json.loads(s["need_hooks_json"])
    verified = ""
    ev = db.evidence_for(conn, s["business_id"])
    if ev:
        verified = max((e["observed_at"] for e in ev), default="")
    row = {
        "Score": round(s["total"]), "Tier": s["tier"], "Business": s["name"], "Category": s["category"] or "",
        "DM Name": natural_name(dm["name"]) if dm else "", "DM Title": dm["title"] if dm else "",
        "DM Conf": round(dm["dm_confidence"], 2) if dm else "",
        "Phone": _display_phone(s["phone_e164"]) or s["phone_raw"]
                 or _display_phone(next((c["value"] for c in contacts if c["kind"] == "phone"), None)),
        "Email": best_email[0], "Email Tier": best_email[1],
        "Website": s["website"] or "", "Address": s["address_street"] or s["address_full"] or "",
        "City": s["address_city"] or "", "Region": s["address_region"] or "", "Postal": s["address_postal"] or "",
        "Country": s["address_country"] or "", "Rating": s["rating"] if s["rating"] is not None else "",
        "Reviews": s["review_count"] if s["review_count"] is not None else "",
        "Likely Need (Hook)": hooks[0] if hooks else "", "Why This Score": top_why,
        "Maps": s["maps_url"] or "", "Source": s["source"] or "", "Verified On": verified,
        "Stale?": _stale_flag(verified, staleness_days),
    }
    enrich_all = json.loads(s["enrich_json"]) if s["enrich_json"] else {}
    regp = enrich_all.get("registry_profile") or {}
    crawled = bool(enrich_all.get("crawled_at"))
    # Honesty markers for the Summary/report stats, captured BEFORE the placeholder text below
    # fills the cells — "not identified - ask for owner/manager" must never count as a DM.
    # Underscore keys are stripped by the writers (they only emit `columns`).
    row["_has_email"] = bool(row["Email"])
    row["_has_dm"] = bool(row["DM Name"])
    # No unresolved cells: a blank is replaced by WHY it is blank, so callers know what they hold.
    if not row["Email"]:
        row["Email"] = "none published" if crawled else ("no website to crawl" if not row["Website"] else "site not crawled")
        row["Email Tier"] = "-"
    if not row["Website"]:
        row["Website"] = "NONE - no web presence (pitch opportunity)"
    if not row["DM Name"]:
        row["DM Name"] = "not identified - ask for owner/manager"
    row["Opening Hours"] = _format_hours(s["hours_json"])
    row["Company No"] = regp.get("company_number") or ("not matched in registry" if enrich_all.get("registry_checked") else "not looked up")
    row["Incorporated"] = (regp.get("incorporated") or "")[:4] or "-"
    row["Company Status"] = regp.get("company_status") or "-"
    row["SIC Codes"] = ", ".join(regp.get("sic_codes") or []) or "-"
    has_phone = bool(row["Phone"])
    has_dm = row["_has_dm"]
    row["Call Readiness"] = ("READY - named contact" if has_phone and has_dm
                             else "READY - ask switchboard" if has_phone
                             else "NO PHONE - research first")
    meta = {f["factor"]: f for f in factors if f.get("group") == "meta"}
    if "status" in meta:  # account_fit profile -> append WE SCORE account-intel columns
        enrich = json.loads(s["enrich_json"]) if s["enrich_json"] else {}
        prof = enrich.get("profile") or {}
        tech = prof.get("tech") or {}
        trig = (prof.get("triggers") or [{}])[0]
        socials = enrich.get("socials") or {}

        def tri(key):
            f = tech.get(key) or {}
            v = f.get("value")
            return (f.get("name") or "yes") if v == "yes" else ("no" if v == "no" else "unknown")

        row.update({
            "Employees": (prof.get("employee_count") or {}).get("value") or "",
            "Employee Range": prof.get("employee_range", "unknown"),
            "Revenue": (prof.get("revenue") or {}).get("value") or "unknown",
            "Departments": ", ".join(prof.get("departments") or []),
            "Microsoft 365": tri("microsoft_365"), "CRM": tri("crm"), "ERP": tri("erp"),
            "Other Systems": ", ".join(tech.get("other") or []),
            "Trigger": trig.get("text", ""), "Trigger Strength": trig.get("strength", ""),
            "LinkedIn": socials.get("linkedin", ""),
            "Contactability": meta["contactability"]["points"] if "contactability" in meta else "",
            "Data Confidence": meta["data_confidence"]["points"] if "data_confidence" in meta else "",
            "Status": meta["status"]["why"],
        })
    return row


def export_run(conn: sqlite3.Connection, icp: ICP, run_id: str, out_dir: Path, formats: list[str],
               staleness_days: int = 90) -> list[str]:
    rows_raw = db.scores_for_run(conn, run_id)
    rows = [_row_for(conn, s, staleness_days) for s in rows_raw]
    rows.sort(key=lambda r: (-r["Score"], r["Tier"]))
    columns = ACCOUNT_COLUMNS if any("Status" in r for r in rows) else COLUMNS
    run_dir = out_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    artifacts: list[str] = []

    if "xlsx" in formats:
        artifacts.append(str(_write_xlsx(run_dir / f"{icp.campaign}.xlsx", rows, icp, run_id, columns)))
    if "csv" in formats:
        artifacts.append(str(_write_csv(run_dir / f"{icp.campaign}.csv", rows, columns)))
    artifacts.append(str(_write_report(run_dir / "report.json", rows, icp, run_id)))
    return artifacts


def _write_csv(path: Path, rows: list[dict], columns: list[str] = COLUMNS) -> Path:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=columns)
        w.writeheader()
        for r in rows:
            w.writerow({k: (v if v != "" else "-") for k, v in r.items() if k in columns})
    return path


def _write_xlsx(path: Path, rows: list[dict], icp: ICP, run_id: str,
                columns: list[str] = COLUMNS) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"
    ws.append(columns)
    for ci in range(1, len(columns) + 1):
        c = ws.cell(row=1, column=ci)
        c.fill = _HEADER_FILL
        c.font = _HEADER_FONT
        c.alignment = Alignment(vertical="center")
    for r in rows:
        ws.append([(r.get(c, '') if r.get(c, '') != '' else '-') for c in columns])
    # styling: tier fill, hyperlinks, zebra, widths
    tier_col = columns.index("Tier") + 1
    web_col = columns.index("Website") + 1
    maps_col = columns.index("Maps") + 1
    phone_col = columns.index("Phone") + 1
    for ri in range(2, len(rows) + 2):
        ws.cell(row=ri, column=phone_col).number_format = "@"  # text — never scientific notation
        tier = ws.cell(row=ri, column=tier_col).value
        fill = _TIER_FILL.get(tier)
        if fill:
            ws.cell(row=ri, column=tier_col).fill = fill
        for col in (web_col, maps_col):
            cell = ws.cell(row=ri, column=col)
            if cell.value:
                cell.hyperlink = cell.value
                cell.font = Font(color="0563C1", underline="single")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{len(rows) + 1}"
    _autosize(ws)

    _summary_sheet(wb, rows, icp, run_id)
    _about_sheet(wb)
    wb.save(path)
    return path


def _summary_sheet(wb: Workbook, rows: list[dict], icp: ICP, run_id: str) -> None:
    ws = wb.create_sheet("Summary")
    tiers = {t: sum(1 for r in rows if r["Tier"] == t) for t in ("A", "B", "C", "DQ")}
    hook_counts: dict[str, int] = {}
    for r in rows:
        if r["Likely Need (Hook)"]:
            hook_counts[r["Likely Need (Hook)"]] = hook_counts.get(r["Likely Need (Hook)"], 0) + 1
    with_dm, with_email = _real_counts(rows)
    lines = [
        ("LeadForge campaign", icp.campaign),
        ("Offer", icp.offer.what),
        ("Categories", ", ".join(icp.target.categories)),
        ("Geography", ", ".join(icp.target.geography.areas) or "bbox"),
        ("Run", run_id),
        ("Generated", now_iso()),
        ("", ""),
        ("Total leads", len(rows)),
        ("Tier A / B / C / DQ", f"{tiers['A']} / {tiers['B']} / {tiers['C']} / {tiers['DQ']}"),
        ("With decision maker", f"{with_dm} ({_pct(with_dm, len(rows))})"),
        ("With email", f"{with_email} ({_pct(with_email, len(rows))})"),
        ("", ""),
        ("Top need hooks", ""),
    ]
    for hook, n in sorted(hook_counts.items(), key=lambda x: -x[1])[:8]:
        lines.append((f"  {n}×", hook))
    lines += [("", ""), ("Compliance reminder", _REGION_REMINDER.get(icp.compliance.region_profile, ""))]
    for k, v in lines:
        ws.append([k, v])
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 90
    ws["A1"].font = Font(bold=True, size=14)


def _about_sheet(wb: Workbook) -> None:
    ws = wb.create_sheet("About")
    ws.append(["LeadForge — how to read this sheet"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])
    ws.append(["Tier", "A ≥ 75 (hot) · B 55–74 · C < 55 · DQ = hard-disqualified by your ICP rules"])
    ws.append(["Score", "0–100, sum of weighted factors; see 'Why This Score' per row for the top drivers"])
    ws.append(["Email Tier", "valid > role > risky > catch_all > unknown (invalid emails are dropped)"])
    ws.append(["DM Conf", "0–1 confidence the agent assigned when labeling the decision maker from site snippets"])
    ws.append(["Likely Need (Hook)", "auto-suggested outreach angle from detected need signals + your offer"])
    ws.append(["Verified On", "timestamp of the newest evidence for the row; older than your staleness window = re-verify"])
    ws.append(["Source", "which engine discovered the business (gosom = Google Maps)"])
    ws.append([])
    ws.append(["Note", "Contact data is public-source and probabilistic. Confirm before high-stakes outreach; honor opt-outs via `leadforge suppress add`."])
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 100


def _real_counts(rows: list[dict]) -> tuple[int, int]:
    """(with_dm, with_email) counting only real data — placeholder cells don't inflate coverage."""
    return (sum(1 for r in rows if r.get("_has_dm")), sum(1 for r in rows if r.get("_has_email")))


def _write_report(path: Path, rows: list[dict], icp: ICP, run_id: str) -> Path:
    with_dm, with_email = _real_counts(rows)
    report = {
        "run": run_id, "campaign": icp.campaign, "generated": now_iso(), "total": len(rows),
        "tiers": {t: sum(1 for r in rows if r["Tier"] == t) for t in ("A", "B", "C", "DQ")},
        "with_dm": with_dm,
        "with_email": with_email,
    }
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return path


def _autosize(ws, cap: int = 48) -> None:
    for col in ws.columns:
        letter = get_column_letter(col[0].column)
        width = max((len(str(c.value)) for c in col if c.value is not None), default=10)
        ws.column_dimensions[letter].width = min(cap, max(10, width + 2))


def _pct(n: int, total: int) -> str:
    return f"{(100 * n / total):.0f}%" if total else "0%"


def summarize_for_digest(conn: sqlite3.Connection, run_id: str) -> dict:
    rows = db.scores_for_run(conn, run_id)
    tiers = {t: 0 for t in ("A", "B", "C", "D", "DQ")}
    for s in rows:
        tiers[s["tier"]] += 1
    out = {"leads": len(rows), "tier_a": tiers["A"], "tier_b": tiers["B"], "tier_c": tiers["C"], "dq": tiers["DQ"]}
    if tiers["D"]:
        out["tier_d"] = tiers["D"]
    return out


def top_hooks(conn: sqlite3.Connection, run_id: str, k: int = 3) -> list[str]:
    counts: dict[str, int] = {}
    for s in db.scores_for_run(conn, run_id):
        hooks = json.loads(s["need_hooks_json"])
        if hooks:
            counts[hooks[0]] = counts.get(hooks[0], 0) + 1
    return [f"{n}× {h}" for h, n in sorted(counts.items(), key=lambda x: -x[1])[:k]]


# used by export test to keep openpyxl import honest
def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _stale_flag(verified_iso: str, staleness_days: int) -> str:
    """'yes' when the newest evidence is older than validation.staleness_days (docs/03 §staleness)."""
    if not verified_iso:
        return ""
    from datetime import datetime, timedelta
    try:
        seen = datetime.fromisoformat(verified_iso.replace("Z", "+00:00"))
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=UTC)
    except ValueError:
        return ""
    return "yes" if datetime.now(tz=UTC) - seen > timedelta(days=staleness_days) else ""


_DAY_ORDER = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _format_hours(hours_json: str | None) -> str:
    """'Mon 9AM-6PM | ... | Sun closed' from the listing's hours dict; '-' when unknown."""
    if not hours_json:
        return "-"
    try:
        hours = json.loads(hours_json)
    except (TypeError, ValueError):
        return "-"
    if not isinstance(hours, dict) or not hours:
        return "-"
    parts = []
    for day in _DAY_ORDER:
        vals = hours.get(day)
        if not vals:
            continue
        txt = ", ".join(vals) if isinstance(vals, list) else str(vals)
        txt = txt.replace(" ", " ").replace("–", "-").replace(" ", " ")
        parts.append(f"{day[:3]} {txt}")
    return " | ".join(parts) if parts else "-"
