#!/usr/bin/env python3
"""
ESPA / Χρηματοδοτικά Προγράμματα Scraper — v5 (h3-based, tested)
====================================================================
Η espa.gr έχει ξεκάθαρη δομή που ΕΠΙΒΕΒΑΙΩΘΗΚΕ με snapshot:
  - Οι πραγματικοί τίτλοι προσκλήσεων είναι σε <h3> tags
  - Οι σελίδες λεπτομερειών είναι στο /el/Pages/ProclamationsFS.aspx?item=NNNN
  - Οι πληροφορίες κάθε πρόσκλησης είναι στο section μεταξύ διαδοχικών <h3>

Λειτουργία:
  - Σαρώνει σελίδα 1 (10 πιο πρόσφατα προγράμματα κάθε μέρα)
  - Συσσωρευτική βάση: τα υπάρχοντα δεν διαγράφονται, τα νέα ενσωματώνονται
  - Καθαρίζει τυχόν παλιές λάθος εγγραφές από buggy προηγούμενα scrapes
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.robotparser as robotparser
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "https://www.espa.gr"
LISTING_URL = f"{BASE}/el/Pages/Proclamations.aspx"
ROBOTS_URL = f"{BASE}/robots.txt"
USER_AGENT = "Mozilla/5.0 (compatible; GovHubBot/1.0; +https://github.com/kkitsop/kratosnet)"

KAD_KEYWORD_MAP = {
    "56": ["εστίαση", "καφε", "καφέ", "επισιτισμ", "τροφοδοσ", "μπαρ", "ζαχαροπλαστ"],
    "55": ["τουρισ", "κατάλυμα", "ξενοδοχ", "ενοικιαζόμεν"],
    "47": ["λιανικ", "εμπόρι", "κατάστημα", "e-λιανικό", "e-shop", "πώληση"],
    "46": ["χονδρικ", "διανομ", "εφοδιαστικ"],
    "62": ["λογισμικ", "πληροφορικ", "ψηφιακ", "τεχνολογ", "startup", "καινοτομ", "προγραμματισμ", "εφαρμογ"],
    "63": ["δεδομέν", "hosting", "φιλοξεν", "cloud"],
    "10": ["μεταποίηση", "τρόφιμα", "βιομηχαν"],
    "01": ["αγροτικ", "γεωργ", "κτηνοτροφ", "καλλιέργει"],
    "41": ["κατασκευ", "οικοδομ", "κτίρι"],
    "43": ["ηλεκτρολογικ", "υδραυλικ", "εξειδικευμέν"],
    "69": ["νομικ", "λογιστικ", "φοροτεχνικ"],
    "71": ["μηχανικ", "αρχιτεκτονικ", "μελέτ"],
    "85": ["εκπαίδευση", "κατάρτιση", "φροντιστήρι"],
    "86": ["υγεία", "ιατρικ", "κλινικ"],
    "90": ["πολιτισμ", "τέχν", "καλλιτεχνικ"],
    "96": ["ομορφι", "κομμωτήρι", "αισθητικ", "ευεξί"],
}

# Τίτλοι που ΔΕΝ είναι πραγματικές προσκλήσεις (menu items, widgets κ.λπ.)
NOT_A_PROGRAM = re.compile(
    r"^(περιοχή μελών|σύνδεση|εγγραφή|τα προγράμματά μου|newsletter|contact|επικοινωνία)",
    re.IGNORECASE
)


def robots_allows() -> bool:
    rp = robotparser.RobotFileParser()
    rp.set_url(ROBOTS_URL)
    try:
        rp.read()
    except Exception as e:
        print(f"[warn] robots.txt μη διαθέσιμο ({e})", file=sys.stderr)
        return True
    return rp.can_fetch(USER_AGENT, LISTING_URL)


def parse_period(text: str) -> tuple[str | None, str | None]:
    m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})\s*[-–—]\s*(\d{1,2}/\d{1,2}/\d{4})", text)
    if not m:
        return None, None
    def to_iso(d):
        day, month, year = d.split("/")
        return f"{year}-{int(month):02d}-{int(day):02d}"
    return to_iso(m.group(1)), to_iso(m.group(2))


def guess_kad_tags(text: str) -> list[str]:
    t = text.lower()
    return [prefix for prefix, kws in KAD_KEYWORD_MAP.items() if any(kw in t for kw in kws)]


def strip_html(html: str) -> str:
    """Αφαιρεί HTML tags και decode-άρει τα βασικά entities."""
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"</p\s*>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    entities = {
        "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
        "&quot;": '"', "&#39;": "'", "&apos;": "'", "\xa0": " ",
    }
    for k, v in entities.items():
        text = text.replace(k, v)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def parse_html(html: str) -> list[dict]:
    """Extract προγραμμάτων από HTML string με βάση τη δομή:
    <h3>τίτλος</h3> ... <a href="ProclamationsFS.aspx?item=NNNN">...</a>

    Επιβεβαιωμένη προσέγγιση από snapshot testing."""

    # Βρες όλα τα <h3> tags με τους indices τους
    h3_matches = list(re.finditer(r"<h3[^>]*>(.*?)</h3>", html, re.DOTALL))

    results = []
    for i, h3 in enumerate(h3_matches):
        title = strip_html(h3.group(1)).strip()
        if not title or len(title) < 10:
            continue
        if NOT_A_PROGRAM.match(title):
            continue

        # Section = HTML μεταξύ αυτού και του επόμενου h3 (ή τέλους)
        start = h3.end()
        end = h3_matches[i + 1].start() if i + 1 < len(h3_matches) else len(html)
        section_html = html[start:end]
        section_text = strip_html(section_html)

        # Extract URL - ψάχνουμε το ProclamationsFS.aspx link
        url_match = re.search(
            r'href="([^"]*ProclamationsFS\.aspx\?item=\d+[^"]*)"',
            section_html
        )
        url = None
        item_id = None
        if url_match:
            url = url_match.group(1).replace("&amp;", "&")
            if url.startswith("/"):
                url = BASE + url
            id_match = re.search(r"item=(\d+)", url)
            if id_match:
                item_id = id_match.group(1)

        # Αν δεν βρήκαμε URL, δεν είναι πρόσκληση
        if not url or not item_id:
            continue

        # Extract metadata
        status = "Ενεργό"
        if section_text.startswith("Έχει λήξει") or "\nΈχει λήξει" in section_text[:100]:
            status = "Έχει λήξει"
        elif section_text.startswith("Αναμένεται") or "\nΑναμένεται" in section_text[:100]:
            status = "Αναμένεται"

        period_match = re.search(r"Περίοδος υποβολής:?\s*([\d/\s\-–—]+)", section_text)
        start_iso, end_iso = parse_period(period_match.group(1)) if period_match else (None, None)

        op_match = re.search(
            r"Επιχειρησιακό πρόγραμμα:?\s*([^\n]+?)(?=\s*Περιοχή εφαρμογής|\s*Δικαιούχοι|\s*Περίοδος|$)",
            section_text
        )
        region_match = re.search(
            r"Περιοχή εφαρμογής:?\s*([^\n]+?)(?=\s*Περίοδος υποβολής|\s*Δικαιούχοι|\s*Επιχειρησιακό|$)",
            section_text
        )
        beneficiaries_match = re.search(
            r"Δικαιούχοι:?\s*([^\n]+?)(?=\s*Περίοδος|\s*Επιχειρησιακό|\s*Περιοχή|$)",
            section_text
        )

        results.append({
            "id": item_id,
            "title": title,
            "status": status,
            "operational_programme": (op_match.group(1).strip() if op_match else "").strip(",: "),
            "region": (region_match.group(1).strip() if region_match else "").strip(",: "),
            "beneficiaries": (beneficiaries_match.group(1).strip() if beneficiaries_match else "").strip(",: "),
            "submission_start": start_iso,
            "submission_end": end_iso,
            "url": url,
            "kad_tags": guess_kad_tags(f"{title} {section_text}"),
            "source": "espa.gr",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        })

    return results


def scrape_page_one() -> list[dict]:
    """Σαρώνει τη σελίδα 1 του Proclamations και επιστρέφει τα προγράμματα."""
    if not robots_allows():
        print(f"[STOP] robots.txt: ΑΠΑΓΟΡΕΥΕΤΑΙ", file=sys.stderr)
        return []
    print(f"[ok] robots.txt: επιτρέπεται", file=sys.stderr)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT)
        page.goto(LISTING_URL, wait_until="networkidle", timeout=30000)

        # Περιμένουμε να φορτώσει το περιεχόμενο των h3 titles
        try:
            page.wait_for_function(
                "() => document.querySelectorAll('h3').length > 5",
                timeout=25000
            )
            print("[ok] Λίστα προγραμμάτων φορτώθηκε", file=sys.stderr)
        except Exception:
            print("[warn] Δεν φόρτωσε λίστα προγραμμάτων εντός 25s — προχωράμε πάντως", file=sys.stderr)

        page.wait_for_timeout(2000)  # extra για rendering
        html = page.content()
        browser.close()

    programs = parse_html(html)
    print(f"[ok] Βρέθηκαν {len(programs)} προγράμματα στη σελίδα 1", file=sys.stderr)
    return programs


def merge_with_existing(new_programs: list[dict], existing_path: Path) -> tuple[list[dict], int, int]:
    """Συγχωνεύει τα νέα προγράμματα με τα υπάρχοντα του data/programs.json.
    Καθαρίζει επίσης εγγραφές που έχουν λάθος τίτλους από παλιές buggy εκτελέσεις."""
    existing: dict[str, dict] = {}
    invalid_removed = 0
    if existing_path.exists():
        try:
            data = json.loads(existing_path.read_text(encoding="utf-8"))
            for p in data.get("programs", []):
                title = p.get("title", "").strip().lower()
                # Λάθος τίτλοι από παλιά bugs
                if ("προσθήκη" in title and "λίστα" in title) or \
                   ("αφαίρεση" in title and "λίστα" in title) or \
                   title.startswith(("περισσότερα", "δείτε ", "read ", "edit ", "επεξεργασία", "share ", "print ")):
                    invalid_removed += 1
                    continue
                key = p.get("id") or p.get("title")
                if key:
                    existing[key] = p
        except Exception as e:
            print(f"[warn] Δεν κατάφερα να διαβάσω υπάρχον programs.json: {e}", file=sys.stderr)

    if invalid_removed > 0:
        print(f"[cleanup] Αφαιρέθηκαν {invalid_removed} λάθος εγγραφές από παλιά scrapes", file=sys.stderr)

    added = 0
    updated = 0
    for p in new_programs:
        key = p.get("id") or p.get("title")
        if not key:
            continue
        if key in existing:
            first_seen = existing[key].get("first_seen") or existing[key].get("fetched_at")
            p["first_seen"] = first_seen
            existing[key] = p
            updated += 1
        else:
            p["first_seen"] = p["fetched_at"]
            existing[key] = p
            added += 1

    return list(existing.values()), added, updated


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=str, default="../data/programs.json")
    ap.add_argument("--max-pages", type=int, default=1, help="LEGACY (αγνοείται στη v5)")
    ap.add_argument("--from-html", type=str, default=None,
                    help="Test mode: parse ένα τοπικό HTML αρχείο (skip Playwright/network)")
    args = ap.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.from_html:
        # Test mode: διάβασε αρχείο, μη κάνεις scraping
        html = Path(args.from_html).read_text(encoding="utf-8")
        new_programs = parse_html(html)
        print(f"[test-mode] Parsed {len(new_programs)} programs from {args.from_html}", file=sys.stderr)
    else:
        new_programs = scrape_page_one()

    if not new_programs:
        print("[warn] Καμία εγγραφή — ΔΕΝ αλλάζω το υπάρχον programs.json", file=sys.stderr)
        sys.exit(0)

    merged, added, updated = merge_with_existing(new_programs, out_path)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(merged),
        "programs": merged,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[done] Συνολικά προγράμματα: {len(merged)} "
          f"(νέα: {added}, ενημερωμένα: {updated})", file=sys.stderr)


if __name__ == "__main__":
    main()
