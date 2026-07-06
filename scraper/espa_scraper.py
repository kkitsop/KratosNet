#!/usr/bin/env python3
"""
ESPA / Χρηματοδοτικά Προγράμματα Scraper — v2 (Playwright)
=============================================================
Η espa.gr/el/Pages/Proclamations.aspx είναι κλασικό ASP.NET WebForms
(SharePoint) — το pagination ("Σελίδα 1 2 3 ... 29") γίνεται μέσω
JavaScript __doPostBack, ΟΧΙ μέσω query string. requests+BeautifulSoup
βλέπουν πάντα την ίδια πρώτη σελίδα. Γι' αυτό εδώ χρησιμοποιούμε Playwright
(headless Chromium) που εκτελεί το JS κανονικά, όπως θα έκανε browser.

Σέβεται robots.txt: πριν κάνει οτιδήποτε, διαβάζει το
https://www.espa.gr/robots.txt και ελέγχει αν επιτρέπεται η πρόσβαση στο
path /el/Pages/Proclamations.aspx για το δικό μας user-agent. Αν όχι, κάνει
έξοδο χωρίς να αγγίξει τη σελίδα και αφήνει μήνυμα στα logs.

Χρήση:
    playwright install chromium --with-deps   # μία φορά
    python espa_scraper.py --out ../data/programs.json --max-pages 29

Σχεδιασμένο να τρέχει μέσω GitHub Actions (βλ. ../.github/workflows/scrape-espa.yml).
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
USER_AGENT = "GovHubBot/1.0 (+https://github.com/kkitsop/govhub; personal research tool, low-frequency nightly fetch, respects robots.txt)"

KAD_KEYWORD_MAP = {
    "56": ["εστίαση", "καφε", "καφέ", "επισιτισμ", "τροφοδοσ", "μπαρ", "ζαχαροπλαστ"],
    "55": ["τουρισ", "κατάλυμα", "ξενοδοχ", "ενοικιαζόμεν"],
    "47": ["λιανικ", "εμπόρι", "κατάστημα", "e-λιανικό", "e-shop", "πώληση"],
    "46": ["χονδρικ", "διανομ", "εφοδιαστικ"],
    "62": ["λογισμικ", "πληροφορικ", "ψηφιακ", "τεχνολογ", "startup", "καινοτομ", "προγραμματισμ", "εφαρμογ"],
    "63": ["δεδομέν", "hosting", "φιλοξεν", "cloud", "πληροφόρηση"],
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
        return False, f"Δεν κατέστη δυνατή η ανάγνωση robots.txt ({e}) — σταματάμε συντηρητικά."
    allowed = rp.can_fetch(USER_AGENT, url)
    return allowed, ("επιτρέπεται" if allowed else "ΑΠΑΓΟΡΕΥΕΤΑΙ από robots.txt")


def parse_period(text: str):
    m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})\s*-\s*(\d{1,2}/\d{1,2}/\d{4})", text)
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
    """Τρέχει JS μέσα στη φορτωμένη σελίδα για να τραβήξει τα προγράμματα
    του τρέχοντος page-load, βασισμένο στη δομή heading + επόμενα siblings."""
    return page.evaluate(
        """
        () => {
          const results = [];
          const headings = Array.from(document.querySelectorAll('h3, h4'));
          for (const h of headings) {
            const title = h.textContent.trim();
            if (!title || title.length < 8) continue;
            let node = h.nextElementSibling;
            let blockText = '';
            let moreHref = null;
            let hops = 0;
            while (node && hops < 12) {
              blockText += ' ' + node.textContent.trim();
              const a = Array.from(node.querySelectorAll ? node.querySelectorAll('a') : [])
                .find(x => x.textContent.includes('Περισσότερα'));
              if (a) { moreHref = a.href; break; }
              if (['H3','H4'].includes(node.tagName)) break;
              node = node.nextElementSibling;
              hops++;
            }
            results.push({title, blockText, moreHref});
          }
          return results;
        }
        """
    )


def go_to_next_page(page, current_page_num: int) -> bool:
    """Αναζήτηση της πραγματικής pagination συνάρτησης του site."""
    next_num = current_page_num + 1
    select_selector = "select[id$='DropDownListPagesTop']"

    old_title = page.evaluate("""() => {
      const h = document.querySelector('h3, h4');
      return h ? h.textContent.trim() : '';
    }""")

    # DIAGNOSTIC στη σελίδα 1: αναζήτηση RSS/feed endpoints ή XHR requests που κάνει
    # η σελίδα όταν αλλάζει σελιδοποίηση
    if current_page_num == 1:
        # Βρες τα RSS/feed links στη σελίδα
        feeds = page.evaluate("""() => {
          const links = Array.from(document.querySelectorAll('link, a'));
          const feedLinks = links.filter(l => {
            const href = l.href || '';
            const type = l.type || l.getAttribute('type') || '';
            return /rss|atom|feed|\\.xml/i.test(href) || /rss|atom/i.test(type);
          }).map(l => ({href: l.href, type: l.type || l.getAttribute('type') || ''}));
          return feedLinks.slice(0, 10);
        }""")
        print(f"[debug] Feed/RSS links βρέθηκαν: {len(feeds)}", file=sys.stderr)
        for f in feeds:
            print(f"  {f}", file=sys.stderr)

        # Παρακολούθησε τι XHR requests πυροδοτούν οι αλληλεπιδράσεις
        # (το επόμενο push σελίδας θα κάνει AJAX request — θα το πιάσουμε)
        captured_requests = []
        page.on("request", lambda req: captured_requests.append({
            'url': req.url[:200],
            'method': req.method,
            'post_data': (req.post_data or '')[:300] if req.post_data else ''
        }) if 'espa.gr' in req.url and 'aspx' in req.url.lower() else None)

        # Τώρα κάνε τη keyboard προσπάθεια για να πιάσουμε το request
        try:
            page.focus(select_selector)
            page.keyboard.type(str(next_num))
            page.wait_for_timeout(500)
            page.keyboard.press("Tab")
            page.wait_for_timeout(3000)  # δώσε χρόνο να στείλει request
        except Exception:
            pass

        print(f"[debug] Network requests μετά την keyboard action: {len(captured_requests)}", file=sys.stderr)
        for r in captured_requests[:5]:
            print(f"  {r['method']} {r['url']}", file=sys.stderr)
            if r['post_data']:
                print(f"    body: {r['post_data']}", file=sys.stderr)

        # Έλεγξε αν όντως άλλαξε σελίδα από την προηγούμενη ενέργεια
        new_title = page.evaluate("""() => {
          const h = document.querySelector('h3, h4');
          return h ? h.textContent.trim() : '';
        }""")
        if new_title != old_title and len(new_title) > 5:
            print(f"[debug] ΝΑΙ, ο τίτλος άλλαξε! Νέος: {new_title[:60]}", file=sys.stderr)
            return True
        else:
            print(f"[debug] Ο τίτλος δεν άλλαξε.", file=sys.stderr)

    has_option = page.evaluate(
        f"""() => {{
          const sel = document.querySelector("{select_selector}");
          if (!sel) return false;
          return Array.from(sel.options).some(o => o.value === '{next_num}');
        }}"""
    )
    if not has_option:
        return False

    try:
        page.focus(select_selector)
        page.keyboard.type(str(next_num))
        page.wait_for_timeout(300)
        page.keyboard.press("Tab")
    except Exception as e:
        return False

    try:
        page.wait_for_function(
            """(oldTitle) => {
              const h = document.querySelector('h3, h4');
              return h && h.textContent.trim() !== oldTitle && h.textContent.trim().length > 5;
            }""",
            arg=old_title,
            timeout=5000
        )
        return True
    except Exception:
        return False


def find_next_page_href(page, next_num: int) -> str | None:
    """Legacy — δεν χρησιμοποιείται πλέον."""
    return None


def scrape(max_pages: int = 29) -> list[dict]:
    allowed, reason = robots_allows(LISTING_URL)
    if not allowed:
        print(f"[STOP] {LISTING_URL}: {reason}. Δεν προχωράμε — σεβόμαστε το robots.txt.", file=sys.stderr)
        return []
    print(f"[ok] robots.txt: {reason}", file=sys.stderr)

    all_programs: dict[str, dict] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT)
        page.goto(LISTING_URL, wait_until="networkidle", timeout=30000)

        # Το SharePoint φορτώνει το pagination με καθυστέρηση μέσω JavaScript.
        # Περιμένουμε να εμφανιστεί το κείμενο "από N" (δείκτης ολοκλήρωσης).
        try:
            page.wait_for_function(
                "() => /από\\s+\\d+/.test(document.body.textContent || '')",
                timeout=15000
            )
        except Exception:
            print("[warn] Δεν εμφανίστηκε δείκτης 'από N' — συνεχίζουμε πάντως", file=sys.stderr)

        # Περιμένουμε επίσης το DropDownListPagesTop να είναι στο DOM
        try:
            page.wait_for_selector("select[id$='DropDownListPagesTop']", timeout=10000)
            print("[ok] Pagination dropdown έτοιμο", file=sys.stderr)
        except Exception:
            print("[warn] Δεν βρέθηκε pagination dropdown — μόνο σελίδα 1 θα σαρωθεί", file=sys.stderr)
        page_num = 1
        while page_num <= max_pages:
            items = extract_items_from_page(page)
            first_title = items[0]["title"][:60] if items else "(κενή)"
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
                period_match = re.search(r"Περίοδος υποβολής:?\s*([\d/\-\s]+)", block_text)
                start_iso, end_iso = parse_period(period_match.group(1)) if period_match else (None, None)
                op_match = re.search(r"Επιχειρησιακό πρόγραμμα:?\s*([^\n]+?)(?=Περιοχή εφαρμογής|$)", block_text)
                region_match = re.search(r"Περιοχή εφαρμογής:?\s*([^\n]+?)(?=Περίοδος υποβολής|$)", block_text)
                item_id = None
                if more_href:
                    m = re.search(r"item=(\d+)", more_href)
                    item_id = m.group(1) if m else None
                key = item_id or title
                all_programs[key] = {
                    "id": item_id,
                    "title": title,
                    "status": status,
                    "operational_programme": (op_match.group(1).strip() if op_match else "").strip(", "),
                    "region": (region_match.group(1).strip() if region_match else "").strip(", "),
                    "submission_start": start_iso,
                    "submission_end": end_iso,
                    "url": more_href,
                    "kad_tags": guess_kad_tags(f"{title} {block_text}"),
                    "source": "espa.gr",
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }
            print(f"[ok] σελίδα {page_num}: raw={len(items)} unique={len(all_programs)} πρώτος='{first_title}'", file=sys.stderr)

            if not go_to_next_page(page, page_num):
                print("[ok] Τέλος σελιδοποίησης.", file=sys.stderr)
                break
            page_num += 1

        browser.close()

    return list(all_programs.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-pages", type=int, default=29)
    ap.add_argument("--out", type=str, default="../data/programs.json")
    args = ap.parse_args()

    programs = scrape(max_pages=args.max_pages)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not programs:
        print("[warn] Καμία εγγραφή — ΔΕΝ αντικαθιστώ το υπάρχον data/programs.json "
              "για να μη χάσουμε δεδομένα από προηγούμενο επιτυχημένο run.", file=sys.stderr)
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
