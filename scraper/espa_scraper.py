#!/usr/bin/env python3
"""
ESPA / Χρηματοδοτικά Προγράμματα Scraper — v3 (RSS-based)
=============================================================
Αντί για Playwright + SharePoint DOM automation (που η espa.gr δεν επιτρέπει
αξιόπιστα λόγω custom event handling), χρησιμοποιούμε το RSS feed που
ανακαλύφθηκε στη σελίδα:

  https://www.espa.gr/_layouts/Miscellaneous/RSSFeeds.aspx?List=proclamations

Το endpoint υποστηρίζει παραμέτρους Days & Items που ελέγχουν το ιστορικό
και το πλήθος. Δοκιμάζουμε αρκετά μεγάλες τιμές για να πιάσουμε ΟΛΑ τα
τρέχοντα προγράμματα (286 σήμερα).

Πλεονεκτήματα έναντι browser-based scraping:
  * Καθαρό XML → εύκολο parsing
  * Δεν χρειάζεται Playwright/Chromium (workflow τρέχει σε δευτερόλεπτα)
  * Δεν εξαρτάται από UI αλλαγές στη σελίδα
  * Πλήρη δεδομένα (τίτλος, περιγραφή, ημερομηνία, URL)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.robotparser as robotparser
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

BASE = "https://www.espa.gr"
RSS_URL = f"{BASE}/_layouts/Miscellaneous/RSSFeeds.aspx"
ROBOTS_URL = f"{BASE}/robots.txt"
USER_AGENT = "GovHubBot/1.0 (+https://github.com/kkitsop/kratosnet; personal research tool)"

# Ίδιο keyword map με frontend/notifier — τα τρία σε συμφωνία
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


def robots_allows(url: str) -> tuple[bool, str]:
    """Επιστρέφει (allowed, reason)."""
    rp = robotparser.RobotFileParser()
    rp.set_url(ROBOTS_URL)
    try:
        rp.read()
    except Exception as e:
        return True, f"robots.txt μη διαθέσιμο ({e}) — προχωράμε συντηρητικά"
    allowed = rp.can_fetch(USER_AGENT, url)
    return allowed, ("επιτρέπεται" if allowed else "ΑΠΑΓΟΡΕΥΕΤΑΙ από robots.txt")


def parse_period_from_text(text: str) -> tuple[str | None, str | None]:
    """Ψάχνει "8/6/2026 - 30/10/2026" ή "από 8/6/2026 έως 30/10/2026" στο text."""
    m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})\s*[-–—]\s*(\d{1,2}/\d{1,2}/\d{4})", text)
    if not m:
        # Δοκίμασε alternative format
        m = re.search(r"από\s+(\d{1,2}/\d{1,2}/\d{4})\s+έως\s+(\d{1,2}/\d{1,2}/\d{4})", text)
    if not m:
        return None, None

    def to_iso(d: str) -> str:
        day, month, year = d.split("/")
        return f"{year}-{int(month):02d}-{int(day):02d}"

    return to_iso(m.group(1)), to_iso(m.group(2))


def parse_status_from_text(text: str) -> str:
    """Ανίχνευση κατάστασης πρόσκλησης."""
    if "Έχει λήξει" in text or "έχει λήξει" in text:
        return "Έχει λήξει"
    if "Αναμένεται" in text or "αναμένεται" in text:
        return "Αναμένεται"
    return "Ενεργό"


def parse_field(text: str, field_name: str) -> str:
    """Παίρνει την τιμή ενός πεδίου (π.χ. 'Επιχειρησιακό πρόγραμμα') από το text."""
    m = re.search(
        rf"{re.escape(field_name)}\s*:?\s*(.+?)(?=\n|Περιοχή|Επιχειρησιακό|Περίοδος|Δικαιούχοι|$)",
        text
    )
    return m.group(1).strip(" ,:.\n\t") if m else ""


def guess_kad_tags(text: str) -> list[str]:
    """Πρόχειρη ετικετοποίηση κλάδου με βάση λέξεις-κλειδιά."""
    t = text.lower()
    return [prefix for prefix, kws in KAD_KEYWORD_MAP.items() if any(kw in t for kw in kws)]


def strip_html(html: str) -> str:
    """Αφαιρεί HTML tags και decode-άρει HTML entities."""
    # Αντικατέστησε <br> με newlines πριν αφαιρέσεις όλα τα tags
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"</p\s*>", "\n", html, flags=re.IGNORECASE)
    # Αφαίρεσε όλα τα tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Decode common HTML entities
    entities = {
        "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
        "&quot;": '"', "&#39;": "'", "&apos;": "'"
    }
    for k, v in entities.items():
        text = text.replace(k, v)
    # Καθάρισε whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n", "\n", text)
    return text.strip()


def fetch_rss(days: int = 3650, items: int = 500) -> str:
    """Κατεβάζει το RSS feed. Οι μεγάλες τιμές days/items στοχεύουν όλα τα
    τρέχοντα προγράμματα, όχι μόνο των τελευταίων 30 ημερών."""
    url = f"{RSS_URL}?List=proclamations&Language=el&Days={days}&Items={items}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.read().decode("utf-8", errors="replace")


def parse_rss_item(item: ET.Element) -> dict | None:
    """Μετατρέπει ένα RSS <item> σε structured dict με τα πεδία της πρόσκλησης."""
    def get_text(tag: str) -> str:
        el = item.find(tag)
        return (el.text or "").strip() if el is not None and el.text else ""

    title = get_text("title")
    link = get_text("link")
    description_html = get_text("description")
    pub_date = get_text("pubDate")

    if not title or len(title) < 5:
        return None

    # Το description περιέχει HTML με τα μεταδεδομένα (Επιχειρησιακό πρόγραμμα,
    # Περιοχή εφαρμογής, Περίοδος υποβολής, κ.λπ.)
    description = strip_html(description_html)

    # Extract item ID από το URL (π.χ. item=7266)
    item_id_match = re.search(r"item=(\d+)", link)
    item_id = item_id_match.group(1) if item_id_match else None

    # Parse μεταδεδομένα από το description
    status = parse_status_from_text(description)
    start_iso, end_iso = parse_period_from_text(description)
    op_prog = parse_field(description, "Επιχειρησιακό πρόγραμμα")
    region = parse_field(description, "Περιοχή εφαρμογής")
    beneficiaries = parse_field(description, "Δικαιούχοι")

    full_text = f"{title} {description}"

    return {
        "id": item_id,
        "title": title,
        "status": status,
        "operational_programme": op_prog,
        "region": region,
        "submission_start": start_iso,
        "submission_end": end_iso,
        "beneficiaries": beneficiaries,
        "url": link,
        "kad_tags": guess_kad_tags(full_text),
        "source": "espa.gr",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def scrape() -> list[dict]:
    allowed, reason = robots_allows(RSS_URL)
    if not allowed:
        print(f"[STOP] {RSS_URL}: {reason}. Δεν προχωράμε.", file=sys.stderr)
        return []
    print(f"[ok] robots.txt: {reason}", file=sys.stderr)

    # Δοκίμασε πρώτα με μεγάλες τιμές (όλα τα προγράμματα).
    # Αν το endpoint αγνοήσει τις τιμές μας, προσπαθούμε ξανά με μικρότερες.
    for days, items in [(3650, 500), (365, 500), (30, 100)]:
        try:
            print(f"[ok] Άντληση RSS: days={days}, items={items}", file=sys.stderr)
            xml_data = fetch_rss(days=days, items=items)
        except urllib.error.HTTPError as e:
            print(f"[warn] HTTP {e.code} για days={days},items={items}: {e}", file=sys.stderr)
            continue
        except Exception as e:
            print(f"[warn] Σφάλμα άντλησης: {e}", file=sys.stderr)
            continue

        try:
            root = ET.fromstring(xml_data)
        except ET.ParseError as e:
            print(f"[warn] Σφάλμα XML parsing: {e}", file=sys.stderr)
            continue

        items_xml = root.findall(".//item")
        print(f"[ok] Βρέθηκαν {len(items_xml)} entries στο RSS", file=sys.stderr)

        if len(items_xml) == 0:
            continue

        programs: dict[str, dict] = {}
        for item in items_xml:
            parsed = parse_rss_item(item)
            if parsed is None:
                continue
            key = parsed["id"] or parsed["title"]
            programs[key] = parsed

        print(f"[ok] Επεξεργάστηκαν {len(programs)} μοναδικά προγράμματα", file=sys.stderr)
        return list(programs.values())

    print("[error] Όλες οι προσπάθειες άντλησης απέτυχαν", file=sys.stderr)
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=str, default="../data/programs.json")
    # --max-pages: legacy option, αγνοείται στη νέα RSS-based έκδοση
    ap.add_argument("--max-pages", type=int, default=29,
                    help="LEGACY (αγνοείται στη v3 έκδοση με RSS)")
    args = ap.parse_args()

    programs = scrape()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not programs:
        print("[warn] Καμία εγγραφή — ΔΕΝ αντικαθιστώ το υπάρχον data/programs.json.",
              file=sys.stderr)
        sys.exit(0 if not out_path.exists() else 1)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(programs),
        "programs": programs,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[done] Γράφτηκαν {len(programs)} προγράμματα στο {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
