#!/usr/bin/env python3
"""
ESPA / Χρηματοδοτικά Προγράμματα Scraper — v4 (Accumulative)
================================================================
Το espa.gr έχει σκόπιμα μπλοκάρει τα RSS feeds του από programmatic access
(εγκεκριμένο endpoint επιστρέφει HTML, το πραγματικό RSS απαγορεύεται από
robots.txt). Το SharePoint pagination επίσης δεν αυτοματοποιείται αξιόπιστα.

**Αντί να παλέψουμε με αυτούς τους περιορισμούς**, χρησιμοποιούμε ΤΗ ΡΟΗ ΤΗΣ
ΙΔΙΑΣ ΤΗΣ ΣΕΛΙΔΑΣ:
  * Κάθε μέρα σαρώνουμε τη ΣΕΛΙΔΑ 1 = 10-11 πιο ΠΡΟΣΦΑΤΑ προγράμματα
  * Νέα προγράμματα εμφανίζονται ΠΑΝΤΑ στη σελίδα 1 → τα πιάνουμε άμεσα
  * Κρατάμε ACCUMULATIVE database — τα υπάρχοντα δεν διαγράφονται
  * Με το χρόνο, η βάση γεμίζει φυσικά

Αυτή η προσέγγιση:
  * Σέβεται πλήρως το robots.txt του site
  * Δεν παλεύει με SharePoint automation
  * Ακόμα κι αν αύριο το site αλλάξει δομή, τα ήδη-συσσωρευμένα προγράμματα
    παραμένουν στο data/programs.json
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


def extract_items_from_page(page) -> list[dict]:
    """Παίρνει τα προγράμματα από την τρέχουσα σελίδα του Proclamations."""
    return page.evaluate(
        """
        () => {
          // Ψάξε containers που περιέχουν τίτλο πρόσκλησης (h3/h4 links)
          const items = [];
          const headers = document.querySelectorAll('h3 a, h4 a');
          const seen = new Set();
          for (const link of headers) {
            const title = link.textContent.trim();
            if (!title || title.length < 10 || seen.has(title)) continue;
            seen.add(title);

            // Πάρε το γονικό container του τίτλου (τυπικά div ή article)
            let container = link.closest('article, div.item, div.proclamation, li, tr');
            if (!container) {
              // Fallback: 3 επίπεδα πίσω
              container = link.parentElement;
              for (let i = 0; i < 3 && container; i++) container = container.parentElement;
            }
            const blockText = container ? container.textContent : '';
            const moreHref = link.href || '';

            items.push({title, blockText, moreHref});
          }
          return items;
        }
        """
    )


def scrape_page_one() -> list[dict]:
    """Σαρώνει τη σελίδα 1 του Proclamations και επιστρέφει τα προγράμματα."""
    if not robots_allows():
        print(f"[STOP] robots.txt: ΑΠΑΓΟΡΕΥΕΤΑΙ", file=sys.stderr)
        return []
    print(f"[ok] robots.txt: επιτρέπεται", file=sys.stderr)

    programs = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT)
        page.goto(LISTING_URL, wait_until="networkidle", timeout=30000)

        # Περιμένουμε να φορτώσει η λίστα
        try:
            page.wait_for_function(
                "() => document.querySelectorAll('h3 a, h4 a').length > 3",
                timeout=15000
            )
        except Exception:
            print("[warn] Δεν φόρτωσε λίστα προγραμμάτων εντός 15s", file=sys.stderr)

        items = extract_items_from_page(page)
        print(f"[ok] Βρέθηκαν {len(items)} προγράμματα στη σελίδα 1", file=sys.stderr)

        for it in items:
            title = it["title"]
            block_text = it["blockText"]
            more_href = it["moreHref"]

            status = "Ενεργό"
            head = block_text[:60]
            if "Αναμένεται" in head:
                status = "Αναμένεται"
            elif "Έχει λήξει" in head:
                status = "Έχει λήξει"

            period_match = re.search(r"Περίοδος υποβολής:?\s*([\d/\-\s–—]+)", block_text)
            start_iso, end_iso = parse_period(period_match.group(1)) if period_match else (None, None)

            op_match = re.search(r"Επιχειρησιακό πρόγραμμα:?\s*([^\n]+?)(?=Περιοχή εφαρμογής|Δικαιούχοι|$)", block_text)
            region_match = re.search(r"Περιοχή εφαρμογής:?\s*([^\n]+?)(?=Περίοδος υποβολής|Δικαιούχοι|$)", block_text)
            beneficiaries_match = re.search(r"Δικαιούχοι:?\s*([^\n]+?)(?=Περίοδος|Επιχειρησιακό|Περιοχή|$)", block_text)

            item_id = None
            if more_href:
                m = re.search(r"item=(\d+)", more_href)
                item_id = m.group(1) if m else None

            programs.append({
                "id": item_id,
                "title": title,
                "status": status,
                "operational_programme": (op_match.group(1).strip() if op_match else "").strip(", "),
                "region": (region_match.group(1).strip() if region_match else "").strip(", "),
                "beneficiaries": (beneficiaries_match.group(1).strip() if beneficiaries_match else "").strip(", "),
                "submission_start": start_iso,
                "submission_end": end_iso,
                "url": more_href,
                "kad_tags": guess_kad_tags(f"{title} {block_text}"),
                "source": "espa.gr",
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            })

        browser.close()
    return programs


def merge_with_existing(new_programs: list[dict], existing_path: Path) -> tuple[list[dict], int, int]:
    """Συγχωνεύει τα νέα προγράμματα με τα υπάρχοντα του data/programs.json.
    Επιστρέφει (merged_list, added, updated)."""
    existing: dict[str, dict] = {}
    if existing_path.exists():
        try:
            data = json.loads(existing_path.read_text(encoding="utf-8"))
            for p in data.get("programs", []):
                key = p.get("id") or p.get("title")
                if key:
                    existing[key] = p
        except Exception as e:
            print(f"[warn] Δεν κατάφερα να διαβάσω υπάρχον programs.json: {e}", file=sys.stderr)

    added = 0
    updated = 0
    for p in new_programs:
        key = p.get("id") or p.get("title")
        if not key:
            continue
        if key in existing:
            # Ενημέρωση: κρατάμε τα δεδομένα του νέου (πιο πρόσφατα), αλλά διατηρούμε
            # το first_seen από το υπάρχον αν υπάρχει
            first_seen = existing[key].get("first_seen") or existing[key].get("fetched_at")
            p["first_seen"] = first_seen
            existing[key] = p
            updated += 1
        else:
            # Νέο πρόγραμμα
            p["first_seen"] = p["fetched_at"]
            existing[key] = p
            added += 1

    return list(existing.values()), added, updated


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=str, default="../data/programs.json")
    ap.add_argument("--max-pages", type=int, default=1, help="LEGACY: αγνοείται στη v4")
    args = ap.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    new_programs = scrape_page_one()

    if not new_programs:
        print("[warn] Καμία εγγραφή — ΔΕΝ αλλάζω το υπάρχον programs.json", file=sys.stderr)
        sys.exit(0)  # δεν αποτυγχάνει το workflow

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
