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


def find_next_page_href(page, next_num: int) -> str | None:
    """Επιστρέφει το href (javascript: postback string) του link για τη
    σελίδα `next_num`.

    Στρατηγική εντοπισμού (βασισμένη στην πραγματική δομή της σελίδας που
    επιβεβαιώθηκε στις 5/7/2026): η σελίδα δείχνει τη σειρά "1 2 3 ... 29"
    ως αριθμητικά links πριν το footer text "από 29". Ψάχνουμε **όλα** τα
    links στη σελίδα των οποίων το κείμενο είναι ακριβώς ο αριθμός που
    θέλουμε (π.χ. "2") ΚΑΙ έχουν href με __doPostBack (SharePoint pager
    convention). Έτσι δεν πιάνουμε άσχετους αριθμούς (ημερομηνίες, ποσά),
    γιατί εκείνα είναι σε άλλα elements ή δεν είναι κλικαρίσιμα.
    """
    return page.evaluate(
        """
        (n) => {
          const target = String(n);
          const candidates = Array.from(document.querySelectorAll('a')).filter(a => {
            const txt = a.textContent.trim();
            const href = a.getAttribute('href') || '';
            return txt === target && href.includes('__doPostBack');
          });
          if (candidates.length === 0) return null;
          // Προτίμα το πρώτο (τυπικά υπάρχει pager πάνω+κάτω αλλά και τα δύο δείχνουν στο ίδιο σημείο)
          return candidates[0].getAttribute('href');
        }
        """,
        next_num,
    )


def go_to_next_page(page, current_page_num: int) -> bool:
    """Πηγαίνει στην επόμενη σελίδα εκτελώντας απευθείας το __doPostBack
    JavaScript. Οι SharePoint postback URLs περιέχουν ειδικούς χαρακτήρες
    (κόμματα, εισαγωγικά) που κάνουν αναξιόπιστο το CSS selector click,
    οπότε παρακάμπτουμε αυτό το πρόβλημα καλώντας απευθείας το JS."""
    next_num = current_page_num + 1
    href = find_next_page_href(page, next_num)
    if not href:
        return False
    # Το href είναι της μορφής "javascript:__doPostBack('...','')"
    js_code = href[len("javascript:"):] if href.startswith("javascript:") else href
    try:
        page.evaluate(js_code)
    except Exception as e:
        print(f"[warn] postback εκτέλεση απέτυχε: {e}", file=sys.stderr)
        return False
    page.wait_for_load_state("networkidle", timeout=15000)
    return True


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
            print("[ok] Pagination footer φορτώθηκε", file=sys.stderr)
        except Exception:
            print("[warn] Δεν εμφανίστηκε 'από N' — συνεχίζουμε πάντως", file=sys.stderr)

        # Scroll στο τέλος για να ενεργοποιηθεί κάθε lazy-loading pagination
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2000)

        # Προσπάθεια για "100 αποτελέσματα ανά σελίδα" ώστε να χρειαστούν
        # μόνο 3 σελίδες αντί για 29. Το κάνουμε με το ίδιο pattern του
        # postback (όχι CSS click) γιατί οι SharePoint dropdowns έχουν
        # πολύπλοκη δομή.
        try:
            hundred_href = page.evaluate(
                """() => {
                  const link = Array.from(document.querySelectorAll('a')).find(a =>
                    a.textContent.trim() === '100' &&
                    (a.getAttribute('href') || '').includes('__doPostBack'));
                  return link ? link.getAttribute('href') : null;
                }"""
            )
            if hundred_href:
                js_code = hundred_href[len("javascript:"):] if hundred_href.startswith("javascript:") else hundred_href
                page.evaluate(js_code)
                page.wait_for_load_state("networkidle", timeout=15000)
                print("[ok] Επιλέχθηκαν 100 αποτελέσματα/σελίδα", file=sys.stderr)
            else:
                print("[warn] Δεν βρέθηκε επιλογή '100 ανά σελίδα' — συνεχίζω με default", file=sys.stderr)
        except Exception as e:
            print(f"[warn] Δεν κατέστη δυνατή η αλλαγή σε 100/σελίδα ({e}) — συνεχίζω με default", file=sys.stderr)

        # === DEBUG ===: εκτεταμένη διερεύνηση δομής pagination
        try:
            debug_info = page.evaluate(
                """() => {
                  // 1. Πάρε όλα τα stoixeia με αριθμητικό κείμενο (a, button, span, div, li)
                  const numeric = Array.from(document.querySelectorAll('a, button, span, li, div'))
                    .filter(el => {
                      const t = (el.textContent || '').trim();
                      // πάρε μόνο "καθαρά" numeric elements (όχι αυτά που περιέχουν παιδιά)
                      return /^\\d{1,3}$/.test(t) && el.children.length === 0;
                    })
                    .slice(0, 40)
                    .map(el => ({
                      tag: el.tagName,
                      text: el.textContent.trim(),
                      onclick: (el.getAttribute('onclick') || '').slice(0, 100),
                      href: (el.getAttribute('href') || '').slice(0, 100),
                      id: el.id || '',
                      className: (el.className || '').toString().slice(0, 80),
                      parentTag: el.parentElement ? el.parentElement.tagName : '',
                      parentClass: el.parentElement ? (el.parentElement.className || '').toString().slice(0, 60) : ''
                    }));
                  // 2. Ψάξε για "Φόρτωση", "Load more", "Επόμενη", "Next" σε οποιοδήποτε element
                  const loadMore = Array.from(document.querySelectorAll('a, button, div'))
                    .filter(el => /φόρτω|load|επόμ|next|περισσ|more|»/i.test(el.textContent || ''))
                    .slice(0, 10)
                    .map(el => ({
                      tag: el.tagName,
                      text: (el.textContent || '').trim().slice(0, 60),
                      onclick: (el.getAttribute('onclick') || '').slice(0, 100),
                      id: el.id || '',
                      className: (el.className || '').toString().slice(0, 80)
                    }));
                  // 3. Πληροφορίες που δείχνουν συνολικό πλήθος
                  const bodyText = document.body.textContent || '';
                  const countMatches = bodyText.match(/από\\s+\\d+|of\\s+\\d+|σύνολο[^\\n]{0,30}\\d+/gi) || [];
                  // 4. Ελεγχος για SharePoint pager container
                  const pagerHtml = document.body.innerHTML.match(/pager[^"]{0,80}/gi) || [];
                  // 5. Πάρε το HTML γύρω από το "από 286" — εκεί πρέπει να είναι το pagination
                  const html = document.body.innerHTML;
                  const idx = html.search(/από\\s+286/);
                  const contextRaw = idx >= 0 ? html.substring(Math.max(0, idx-800), Math.min(html.length, idx+200)) : '';
                  // Καθάρισε whitespace για ευανάγνωστο log
                  const context = contextRaw.replace(/\\s+/g, ' ').slice(0, 1200);
                  return {
                    numeric: numeric,
                    numericCount: numeric.length,
                    loadMore: loadMore,
                    countMatches: countMatches.slice(0, 5),
                    pagerHints: pagerHtml.slice(0, 5),
                    contextAroundCount: context
                  };
                }"""
            )
            print(f"[debug] Numeric elements found: {debug_info['numericCount']}", file=sys.stderr)
            for item in debug_info['numeric'][:20]:
                print(f"  <{item['tag']}> text={item['text']!r} onclick={item['onclick']!r} href={item['href']!r} id={item['id']!r} class={item['className']!r} parent=<{item['parentTag']} class={item['parentClass']!r}>", file=sys.stderr)
            print(f"[debug] Load-more/next candidates: {len(debug_info['loadMore'])}", file=sys.stderr)
            for item in debug_info['loadMore']:
                print(f"  <{item['tag']}> text={item['text']!r} onclick={item['onclick']!r} id={item['id']!r} class={item['className']!r}", file=sys.stderr)
            print(f"[debug] Count text matches: {debug_info['countMatches']}", file=sys.stderr)
            print(f"[debug] Pager hints in HTML: {debug_info['pagerHints']}", file=sys.stderr)
            print(f"[debug] HTML around 'από 286':", file=sys.stderr)
            print(f"  {debug_info['contextAroundCount']}", file=sys.stderr)
        except Exception as e:
            print(f"[debug] error: {e}", file=sys.stderr)

        page_num = 1
        while page_num <= max_pages:
            items = extract_items_from_page(page)
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
            print(f"[ok] σελίδα {page_num}: σύνολο {len(all_programs)} προγράμματα", file=sys.stderr)

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
