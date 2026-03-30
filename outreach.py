#!/usr/bin/env python3
"""
Apollo Outreach Automation for CalHacks Sponsorship
----------------------------------------------------
Reads companies.csv, finds 3 relevant contacts per company via Apollo,
adds them to a list, and prints a summary.

Usage:
    python outreach.py                  # uses companies.csv by default
    python outreach.py my_companies.csv
"""

import csv
import os
import sys
import time
from datetime import datetime
from typing import Optional

import anthropic
import requests
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

APOLLO_API_KEY             = os.getenv("APOLLO_API_KEY")
APOLLO_LIST_NAME           = os.getenv("APOLLO_LIST_NAME")
APOLLO_COMPANIES_LIST_NAME = os.getenv("APOLLO_COMPANIES_LIST_NAME")

BASE_URL             = "https://api.apollo.io/api/v1"
CONTACTS_PER_COMPANY = 3
APOLLO_FETCH_COUNT   = 20  # fetch this many from Apollo, Claude picks the best 3

# ── Helpers ───────────────────────────────────────────────────────────────────

def headers() -> dict:
    return {
        "Content-Type": "application/json",
        "X-Api-Key": APOLLO_API_KEY,
    }


def resolve_label_ids() -> tuple[str, Optional[str]]:
    """
    Fetch all Apollo labels and return (contacts_label_id, companies_label_id).
    Exits if APOLLO_LIST_NAME is not found.
    """
    resp = requests.get(f"{BASE_URL}/labels", headers=headers())
    resp.raise_for_status()
    labels = resp.json()

    contacts_id  = None
    companies_id = None
    for label in labels:
        name = label.get("name", "")
        lid  = label.get("_id") or label.get("id")
        if name.lower() == APOLLO_LIST_NAME.lower():
            contacts_id = lid
        if APOLLO_COMPANIES_LIST_NAME and name.lower() == APOLLO_COMPANIES_LIST_NAME.lower():
            companies_id = lid

    if not contacts_id:
        available = [l.get("name") for l in labels]
        print(f'\nError: contacts list "{APOLLO_LIST_NAME}" not found.')
        print(f"Available lists: {available}")
        sys.exit(1)

    if APOLLO_COMPANIES_LIST_NAME and not companies_id:
        available = [l.get("name") for l in labels]
        print(f'\nError: companies list "{APOLLO_COMPANIES_LIST_NAME}" not found.')
        print(f"Available lists: {available}")
        sys.exit(1)

    return contacts_id, companies_id


def _people_search(payload: dict) -> list[dict]:
    resp = requests.post(f"{BASE_URL}/mixed_people/api_search", json=payload, headers=headers())
    if not resp.ok:
        print(f"  [search {resp.status_code}] {resp.text[:300]}")
        resp.raise_for_status()
    return resp.json().get("people", [])


def search_people(domain: str, company_name: str) -> list[dict]:
    """Fetch up to APOLLO_FETCH_COUNT people from Apollo with no title/seniority filter."""
    base: dict = {"page": 1, "per_page": APOLLO_FETCH_COUNT}
    if domain:
        people = _people_search({**base, "q_organization_domains_list": [domain]})
        if people:
            return people
    return _people_search({**base, "q_organization_name": company_name})


def pick_best_contacts(company_name: str, people: list[dict]) -> list[dict]:
    """
    Use Claude Haiku to select the best 3 people to contact for hackathon sponsorship.
    Returns a subset of the people list.
    """
    if len(people) <= CONTACTS_PER_COMPANY:
        return people

    roster = "\n".join(
        f"{i}. {p.get('first_name', '')} {p.get('last_name') or ''} — {p.get('title', 'Unknown')}"
        for i, p in enumerate(people)
    )

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"""CalHacks is UC Berkeley's flagship hackathon — one of the largest in the world, with 2,000+ student developers. We are reaching out to AI/developer tool companies to ask for hackathon sponsorship (cash or prizes in exchange for brand exposure to top student developers).

We need to find the right person at each company who would actually handle or approve a hackathon sponsorship. The ideal contacts are:
- Developer Relations, DevRel, Developer Advocate — BEST, they directly benefit from student developer adoption
- Partnerships, Ecosystem, Community — great fit
- University Programs, Campus Programs, Student Programs — perfect
- Growth or Marketing (manager/lead level) — good if no devrel exists
- Founder or CEO only if the company is very small (< 20 people) and no one else fits

Avoid: engineers, sales reps, finance, legal, pure executives (CMO/COO/CTO) at larger companies, VP/C-suite at mid-to-large companies.

Company: {company_name}
People:
{roster}

Pick the {CONTACTS_PER_COMPANY} best. Reply with ONLY the numbers (comma-separated), e.g.: 2, 5, 11"""
        }]
    )

    text = response.content[0].text.strip()
    try:
        indices = [int(x.strip()) for x in text.split(",")]
        return [people[i] for i in indices if 0 <= i < len(people)]
    except (ValueError, IndexError):
        # Fallback: return first N if parsing fails
        return people[:CONTACTS_PER_COMPANY]


def reveal_person(person_id: str) -> dict:
    """Call people/match to get email + full name. Returns partial person dict."""
    if not person_id:
        return {}
    resp = requests.post(
        f"{BASE_URL}/people/match",
        json={"id": person_id, "reveal_personal_emails": False},
        headers=headers(),
    )
    if resp.ok:
        return resp.json().get("person", {})
    return {}


def get_or_create_contact(person: dict, email: Optional[str], last_name: Optional[str]) -> Optional[str]:
    """Return the CRM contact ID for this person, creating one if needed."""
    if person.get("contact_id"):
        # Already a contact — patch with email if we just revealed it
        if email:
            requests.patch(
                f"{BASE_URL}/contacts/{person['contact_id']}",
                json={"email": email},
                headers=headers(),
            )
        return person["contact_id"]

    org = person.get("organization") or {}
    payload = {
        "first_name":        person.get("first_name", ""),
        "last_name":         last_name or "",
        "title":             person.get("title", ""),
        "organization_name": org.get("name", ""),
        "website_url":       org.get("website_url", ""),
        "person_id":         person.get("id"),
    }
    if email:
        payload["email"] = email

    resp = requests.post(f"{BASE_URL}/contacts", json=payload, headers=headers())
    resp.raise_for_status()
    return resp.json().get("contact", {}).get("id")


def already_in_list(contact_id: str, label_id: str) -> bool:
    resp = requests.get(f"{BASE_URL}/contacts/{contact_id}", headers=headers())
    if not resp.ok:
        return False
    return label_id in (resp.json().get("contact", {}).get("label_ids") or [])


def add_to_list(contact_id: str, label_id: str) -> bool:
    resp = requests.patch(
        f"{BASE_URL}/contacts/{contact_id}",
        json={"label_ids": [label_id]},
        headers=headers(),
    )
    if not resp.ok:
        print(f"  [label {resp.status_code}] {resp.text[:200]}")
    return resp.ok


def company_already_in_list(domain: str, companies_label_id: str) -> bool:
    """Return True if an account for this domain is already in the companies list."""
    resp = requests.post(
        f"{BASE_URL}/accounts/search",
        json={"q_organization_domains": domain, "page": 1, "per_page": 1},
        headers=headers(),
    )
    if not resp.ok:
        return False
    for account in resp.json().get("accounts", []):
        if companies_label_id in (account.get("label_ids") or []):
            return True
    return False


def add_company_to_list(company_name: str, domain: str, companies_label_id: str) -> bool:
    """Create (or find) an Apollo account for this company and add it to the companies list."""
    resp = requests.post(
        f"{BASE_URL}/accounts",
        json={"name": company_name, "domain": domain, "website_url": f"https://{domain}"},
        headers=headers(),
    )
    if not resp.ok:
        return False
    account_id = resp.json().get("account", {}).get("id")
    if not account_id:
        return False
    patch = requests.patch(
        f"{BASE_URL}/accounts/{account_id}",
        json={"label_ids": [companies_label_id]},
        headers=headers(),
    )
    return patch.ok


# ── Core loop ─────────────────────────────────────────────────────────────────

LOG_FILE = "outreach_log.csv"
LOG_HEADERS = ["date", "company", "domain", "name", "title", "email"]


def append_to_log(company_name: str, domain: str, contacts: list[dict]) -> None:
    """Append newly added contacts to the running outreach log CSV."""
    write_header = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_HEADERS)
        if write_header:
            writer.writeheader()
        date = datetime.today().strftime("%Y-%m-%d")
        for c in contacts:
            writer.writerow({
                "date":    date,
                "company": company_name,
                "domain":  domain,
                "name":    c["name"],
                "title":   c["title"],
                "email":   c["email"],
            })


def process_companies(csv_path: str, label_id: str, companies_label_id: Optional[str]) -> list[dict]:
    console = Console()
    summary_rows: list[dict] = []

    with open(csv_path, newline="", encoding="utf-8") as f:
        companies = list(csv.DictReader(f))

    console.print(f"\n[bold]Found {len(companies)} companies to process.[/]\n")

    for row in companies:
        # Handle both "company_name" and "Company" header styles
        company_name = (row.get("company_name") or row.get("Company") or "").strip()
        website      = (row.get("website") or row.get("Website") or "").strip()

        if not company_name:
            continue

        domain = website.lower()
        for prefix in ("https://", "http://", "www."):
            domain = domain.removeprefix(prefix)
        domain = domain.rstrip("/").split("/")[0]

        console.rule(f"[bold cyan]{company_name}[/]")

        if companies_label_id and company_already_in_list(domain, companies_label_id):
            console.print("  [dim]Already in companies list — skipping.[/]")
            summary_rows.append({"company": company_name, "contacts": [], "error": None})
            continue

        try:
            people = search_people(domain, company_name)
        except requests.HTTPError as e:
            console.print(f"  [red]Search failed:[/] {e}")
            summary_rows.append({"company": company_name, "contacts": [], "error": str(e)})
            continue

        if not people:
            console.print("  [yellow]No matching contacts found — skipping.[/]")
            summary_rows.append({"company": company_name, "contacts": [], "error": None})
            continue

        # Let Claude pick the best candidates from the full pool
        candidates = pick_best_contacts(company_name, people)
        console.print(f"  [dim]Claude selected {len(candidates)} from {len(people)} people[/]")

        selected: list[dict] = []
        skipped_dupes = 0

        for person in candidates:
            # Reveal email first — skip this person if Apollo doesn't have one
            revealed  = reveal_person(person.get("id"))
            email     = revealed.get("email")
            if not email:
                continue

            last_name = revealed.get("last_name") or person.get("last_name") or ""

            try:
                contact_id = get_or_create_contact(person, email, last_name)
            except requests.HTTPError:
                continue
            if not contact_id:
                continue

            if already_in_list(contact_id, label_id):
                skipped_dupes += 1
                continue

            ok = add_to_list(contact_id, label_id)
            if ok:
                full_name = f"{person.get('first_name', '')} {last_name}".strip()
                selected.append({
                    "name":  full_name,
                    "title": person.get("title", "—"),
                    "email": email or "—",
                })

            time.sleep(0.3)

        if skipped_dupes:
            console.print(f"  [dim]Skipped {skipped_dupes} already in list.[/]")

        status = f"[green]added {len(selected)} to list[/]" if selected else "[dim]nothing new to add[/]"
        console.print(f"  {status}")

        if companies_label_id and selected:
            add_company_to_list(company_name, domain, companies_label_id)

        summary_rows.append({"company": company_name, "contacts": selected, "error": None})

        if selected:
            append_to_log(company_name, domain, selected)

        time.sleep(1.0)

    return summary_rows


# ── Output ────────────────────────────────────────────────────────────────────

def print_summary(summary_rows: list[dict]) -> None:
    console = Console()

    table = Table(title="Outreach Summary", show_lines=True, expand=True)
    table.add_column("Company", style="bold cyan", no_wrap=True, ratio=2)
    table.add_column("Name",    style="white",                  ratio=2)
    table.add_column("Title",   style="dim",                    ratio=3)
    table.add_column("Email",   style="green",                  ratio=3)

    total = 0
    for row in summary_rows:
        company  = row["company"]
        contacts = row["contacts"]
        error    = row["error"]

        if error:
            table.add_row(company, f"[red]Error: {error}[/]", "", "")
        elif not contacts:
            table.add_row(company, "[dim]No contacts found[/]", "", "")
        else:
            for i, c in enumerate(contacts):
                table.add_row(company if i == 0 else "", c["name"], c["title"], c["email"])
                total += 1

    console.print()
    console.print(table)
    console.print(f"\n[bold green]Total contacts added to list:[/] {total}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    missing = [v for v in ["APOLLO_API_KEY", "APOLLO_LIST_NAME"] if not os.getenv(v)]
    if missing:
        print(f"Error: missing env vars: {', '.join(missing)}")
        sys.exit(1)

    csv_path = sys.argv[1] if len(sys.argv) > 1 else "companies.csv"
    if not os.path.exists(csv_path):
        print(f"Error: {csv_path} not found. Expected columns: company_name, website")
        sys.exit(1)

    label_id, companies_label_id = resolve_label_ids()

    results = process_companies(csv_path, label_id, companies_label_id)
    print_summary(results)


if __name__ == "__main__":
    main()
