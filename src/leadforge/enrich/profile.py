"""Account-intel profiling (WE SCORE spec, v0.1.1): tech stack, departments, employee estimate,
industry buying triggers — all tri-state facts where UNKNOWN is never NO.

Every fact carries {"value", "state", "source"} with state in CONFIRMED | ESTIMATED | INFERRED | UNKNOWN.
Nothing here opens a socket except mx_provider() (cached DNS, same resolver as email validation).
LinkedIn URLs are only ever the ones a business publishes on its own site — never fetched.
"""

from __future__ import annotations

import re
from functools import lru_cache

UNKNOWN = {"value": None, "state": "UNKNOWN", "source": ""}


def fact(value, state: str, source: str) -> dict:
    return {"value": value, "state": state, "source": source}


# --- technology detection --------------------------------------------------------------------
# HTML/site fingerprints. Detection => CONFIRMED yes; absence => UNKNOWN (internal systems are
# frequently invisible publicly — the spec's hard rule).
_CRM_FINGERPRINTS = {
    "hubspot": r"js\.hs-scripts\.com|hubspot\.com|hs-analytics",
    "salesforce": r"salesforce\.com|force\.com|pardot\.com",
    "zoho": r"zoho\.com|zohocdn|zohopublic",
    "dynamics": r"dynamics\.com|crm\d*\.dynamics",
}
_ERP_FINGERPRINTS = {
    "odoo": r"\bodoo\b",
    "netsuite": r"\bnetsuite\b",
    "sap": r"\bsap\s*business\s*one\b|sapb1",
    "dynamics365": r"dynamics\s*365|business\s*central",
}
_OTHER_SYSTEMS = {
    "wms": r"\bWMS\b|warehouse management system",
    "tms": r"\bTMS\b|transport(ation)? management system",
    "ehr_emr": r"\bEHR\b|\bEMR\b|electronic (health|medical) record",
    "customer_portal": r"customer portal|client portal|patient portal",
    "project_management": r"\bjira\b|\basana\b|monday\.com|\bclickup\b|\bwrike\b",
}


def detect_tech(html_blob: str, text_blob: str, mx_hosts: list[str] | None) -> dict:
    """-> {"microsoft_365": fact, "crm": fact(+name), "erp": fact(+name), "other": [names]}."""
    out = {"microsoft_365": dict(UNKNOWN), "crm": dict(UNKNOWN), "erp": dict(UNKNOWN), "other": []}
    if mx_hosts:
        joined = " ".join(mx_hosts).lower()
        if "outlook" in joined or "protection.outlook" in joined:
            out["microsoft_365"] = fact("yes", "CONFIRMED", "mx-records")
        elif "google" in joined or "aspmx" in joined:
            out["microsoft_365"] = fact("no", "CONFIRMED", "mx-records (google workspace)")
    for name, pat in _CRM_FINGERPRINTS.items():
        if re.search(pat, html_blob, re.IGNORECASE):
            out["crm"] = fact("yes", "CONFIRMED", f"site-fingerprint:{name}") | {"name": name}
            break
    for name, pat in _ERP_FINGERPRINTS.items():
        if re.search(pat, html_blob + " " + text_blob, re.IGNORECASE):
            out["erp"] = fact("yes", "CONFIRMED", f"site-fingerprint:{name}") | {"name": name}
            break
    for name, pat in _OTHER_SYSTEMS.items():
        if re.search(pat, text_blob, re.IGNORECASE):
            out["other"].append(name)
    return out


# --- organisational complexity ---------------------------------------------------------------
_DEPARTMENTS = ("operations", "logistics", "supply chain", "it", "technology", "engineering",
                "finance", "sales", "marketing", "hr", "human resources", "customer service",
                "production", "quality", "procurement")
_DEPT_RE = re.compile(r"\b(head of|director of|vp of|manager,?|department of|our)\s+(" +
                      "|".join(_DEPARTMENTS) + r")\b|\b(" + "|".join(_DEPARTMENTS) + r")\s+(team|department|division|manager|director)\b",
                      re.IGNORECASE)


def detect_departments(text_blob: str) -> list[str]:
    found: dict[str, None] = {}
    for m in _DEPT_RE.finditer(text_blob):
        dept = (m.group(2) or m.group(3) or "").strip().casefold()
        if dept:
            found.setdefault("it" if dept == "technology" else dept)
    return list(found)


# --- employee estimate ------------------------------------------------------------------------
_EMP_RE = re.compile(r"\b(?:team of|over|more than|employs?|staff of)\s+(\d{2,5})\b"
                     r"|\b(\d{2,5})\+?\s+(?:employees|staff|people|team members)\b", re.IGNORECASE)


def estimate_employees(text_blob: str, source_url: str) -> dict:
    """Self-published headcount is an ESTIMATE, never CONFIRMED. Absent -> UNKNOWN (not zero)."""
    for m in _EMP_RE.finditer(text_blob):
        n = int(m.group(1) or m.group(2))
        if 1 <= n <= 100_000:
            return fact(n, "ESTIMATED", source_url)
    return dict(UNKNOWN)


def employee_range(count: int | None) -> str:
    if count is None:
        return "unknown"
    if count < 20:
        return "<20"
    if count < 50:
        return "20-49"
    if count <= 500:
        return "50-500"
    return ">500"


# --- industry buying triggers -----------------------------------------------------------------
TRIGGER_TABLE: dict[str, dict[str, str]] = {
    "manufacturing": {
        "strong": r"factory expansion|new (production line|factory|plant)|erp (replacement|upgrade|implementation)|industry 4\.0|acquisition",
        "medium": r"production bottleneck|supply.chain disruption|quality (issue|problem)|equipment downtime|compliance requirement",
    },
    "logistics": {
        "strong": r"fleet expansion|(new|expanded) warehouse|tms implementation|wms implementation|cross.border expansion|merger|acquisition",
        "medium": r"route optimi[sz]ation|shipment visibility|real.time tracking|manual dispatch|labou?r shortage",
    },
    "healthcare": {
        "strong": r"(clinic|hospital) expansion|ehr moderni[sz]ation|emr moderni[sz]ation|digital transformation|telehealth",
        "medium": r"patient volume|scheduling inefficien|interoperability|accreditation|staff shortage",
    },
    "professional services": {
        "strong": r"crm implementation|merger|acquisition|rapid (growth|expansion)|new office",
        "medium": r"resource allocation|manual (proposal|reporting)|utili[sz]ation|client portal|workflow standardi[sz]ation",
    },
    "construction": {
        "strong": r"(major|large) (project|contract) (won|awarded)|bim adoption|digital transformation|regional expansion",
        "medium": r"project delay|subcontractor coordination|document management|equipment tracking|cost overrun|safety compliance",
    },
    "saas": {
        "strong": r"funding round|series [a-d]\b|international expansion|soc ?2|iso ?27001|enterprise customers",
        "medium": r"technical debt|legacy (software|system)|api integration|infrastructure scaling|churn",
    },
    # generic fallback for any other industry
    "_generic": {
        "strong": r"expansion|new (office|branch|location)|funding|acquisition|merger|major contract",
        "medium": r"hiring|growing team|new market|digital transformation",
    },
}
_NEAR_DATE_RE = re.compile(r"\b(20\d{2})\b")


def find_triggers(text_blob: str, industry: str, source_url: str, max_triggers: int = 3) -> list[dict]:
    """-> [{category, text, url, strength, date}] — snippet-evidenced, freshness-banded when a year
    is visible near the match, else date=None (freshness UNKNOWN, per spec never downgraded to low)."""
    table = TRIGGER_TABLE.get((industry or "").casefold(), TRIGGER_TABLE["_generic"])
    out: list[dict] = []
    for strength in ("strong", "medium"):
        for m in re.finditer(table[strength], text_blob, re.IGNORECASE):
            snippet = re.sub(r"\s+", " ", text_blob[max(0, m.start() - 80): m.end() + 80]).strip()
            years = _NEAR_DATE_RE.findall(snippet)
            out.append({"category": industry or "generic", "text": snippet[:240], "url": source_url,
                        "strength": strength, "date": f"{max(years)}-01-01" if years else None})
            if len(out) >= max_triggers:
                return out
    return out


def trigger_freshness(date_iso: str | None, today_iso: str) -> str:
    """0-90d VERY_STRONG · 91-180 STRONG · 181-365 MEDIUM · older LOW · undated UNKNOWN."""
    if not date_iso:
        return "UNKNOWN"
    from datetime import date
    try:
        d = date.fromisoformat(date_iso[:10])
        t = date.fromisoformat(today_iso[:10])
    except ValueError:
        return "UNKNOWN"
    days = (t - d).days
    if days <= 90:
        return "VERY_STRONG"
    if days <= 180:
        return "STRONG"
    if days <= 365:
        return "MEDIUM"
    return "LOW"


# --- MX provider lookup (cached; reuses the email-validation resolver) ------------------------
@lru_cache(maxsize=2048)
def mx_hosts_for(domain: str, timeout: float = 5.0) -> tuple[str, ...]:
    if not domain:
        return ()
    try:
        from leadforge.enrich.validate import get_resolver
        answers = get_resolver().resolve(domain, "MX", lifetime=timeout)
        return tuple(str(a.exchange).rstrip(".").lower() for a in answers)
    except Exception:  # noqa: BLE001 — DNS trouble -> unknown, never a crash
        return ()


def build_profile(pages: list, domain: str | None, industry: str | None) -> dict:
    """Assemble the account-intel profile from crawled pages. Pure aggregation; stored in enrich_json."""
    html_blob = "\n".join(p.html for p in pages)
    text_blob = "\n".join(p.text for p in pages)
    home_url = pages[0].url if pages else ""
    emp = estimate_employees(text_blob, home_url)
    return {
        "tech": detect_tech(html_blob, text_blob, list(mx_hosts_for(domain or ""))),
        "departments": detect_departments(text_blob),
        "employee_count": emp,
        "employee_range": employee_range(emp["value"]),
        "revenue": dict(UNKNOWN),  # not publicly derivable politely; populate by hand or a future source
        "triggers": find_triggers(text_blob, industry or "", home_url),
    }
