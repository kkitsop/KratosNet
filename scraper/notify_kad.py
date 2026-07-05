#!/usr/bin/env python3
"""
kratosNet — Nightly ΚΑΔ notifier
==================================
Τρέχει στο ΙΔΙΟ GitHub Actions workflow με τον ESPA scraper (μηδέν επιπλέον
υποδομή/κόστος), αμέσως μετά την ανανέωση του data/programs.json:

  1. Διαβάζει τα τρέχοντα προγράμματα από data/programs.json
  2. Τραβάει από τη Supabase όλους τους χρήστες με notify_email = true
  3. Για κάθε χρήστη, βρίσκει προγράμματα που (α) ταιριάζουν στο ΚΑΔ/κλάδο του
     και (β) ΔΕΝ του έχουν ήδη σταλεί (πίνακας notified)
  4. Στέλνει ένα συγκεντρωτικό email ανά χρήστη μέσω Brevo (free tier: 300/ημέρα)
  5. Καταγράφει τα σταλμένα στο notified ώστε να μην ξανασταλούν

Απαιτούμενα secrets στο GitHub repo (Settings → Secrets → Actions):
  SUPABASE_URL          — π.χ. https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY  — το service_role key (ΠΟΤΕ το anon key εδώ· και ΠΟΤΕ
                          το service key στο frontend)
  BREVO_API_KEY         — από app.brevo.com → SMTP & API → API Keys

Όρια free tier (συνειδητές σχεδιαστικές επιλογές):
  * Brevo: 300 emails/ημέρα. Το script στέλνει ΕΝΑ συγκεντρωτικό email/χρήστη/βράδυ
    (όχι ένα ανά πρόγραμμα), άρα υποστηρίζει ~300 opt-in χρήστες πριν χρειαστεί
    αναβάθμιση. Αν ξεπεραστεί το όριο, τα επιπλέον αποτυγχάνουν ήπια και
    ξαναδοκιμάζονται το επόμενο βράδυ (δεν έχουν καταγραφεί ως σταλμένα).
  * Supabase free: pause μετά από 7 ημέρες αδράνειας — αυτό το script τρέχει
    νυχτερινά, άρα το project μένει πάντα ενεργό.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
PROGRAMS_PATH = Path(os.environ.get("PROGRAMS_PATH", "data/programs.json"))
SENDER_EMAIL = os.environ.get("NOTIFY_SENDER_EMAIL", "")  # verified sender στο Brevo
SENDER_NAME = "kratosNet"
APP_URL = os.environ.get("APP_URL", "https://kratosnet.pages.dev")

# Ίδιο keyword map με frontend/scraper — κρατάμε τα τρία σε συμφωνία.
KAD_KEYWORDS = {
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


def http_json(url: str, method: str = "GET", headers: dict | None = None, body: dict | list | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            raw = res.read().decode()
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        print(f"[http {e.code}] {method} {url}: {e.read().decode()[:300]}", file=sys.stderr)
        raise


def sb_headers(extra: dict | None = None) -> dict:
    h = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def guess_tags(text: str) -> set[str]:
    t = text.lower()
    tags = {prefix for prefix, kws in KAD_KEYWORDS.items() if any(kw in t for kw in kws)}
    import re
    m = re.search(r"(\d{2})", text)
    if m:
        tags.add(m.group(1))
    return tags


def program_matches(program: dict, user_tags: set[str], user_region: str) -> bool:
    ptags = set(program.get("kad_tags") or [])
    text = f"{program.get('title','')} {program.get('beneficiaries','')}".lower()
    tag_hit = bool(user_tags & ptags) or any(
        kw in text for tag in user_tags for kw in KAD_KEYWORDS.get(tag, [])
    )
    if not tag_hit:
        return False
    if user_region:
        r = user_region.lower()
        preg = (program.get("region") or "").lower()
        # Αν ο χρήστης έχει δηλώσει περιφέρεια, τη σεβόμαστε — εκτός από πανελλαδικά
        if preg and "όλη η ελλάδα" not in preg and r[:4] not in preg:
            return False
    return program.get("status") in ("Ενεργό", "Αναμένεται")


def send_email(to_email: str, subject: str, html: str) -> bool:
    body = {
        "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html,
    }
    try:
        http_json(
            "https://api.brevo.com/v3/smtp/email",
            method="POST",
            headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
            body=body,
        )
        return True
    except Exception as e:
        print(f"[email-fail] {to_email}: {e}", file=sys.stderr)
        return False


def render_email(programs: list[dict]) -> str:
    rows = "".join(
        f"""<tr>
          <td style="padding:10px 0;border-bottom:1px solid #E4E1D5">
            <div style="font-weight:600;color:#1B2430">{p.get('title','')}</div>
            <div style="font-size:12px;color:#5B6472;margin-top:2px">
              {p.get('operational_programme','')} · {p.get('region','')}
              {('· έως ' + p['submission_end']) if p.get('submission_end') else ''}
            </div>
            <a href="{p.get('url','')}" style="font-size:13px;color:#133A5E;font-weight:600">Δες την πρόσκληση →</a>
          </td>
        </tr>"""
        for p in programs
    )
    return f"""<div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto;background:#FBFAF6;padding:24px;border:1px solid #D9D5C8;border-radius:8px">
      <h2 style="color:#133A5E;margin:0 0 4px">kratosNet</h2>
      <p style="color:#5B6472;font-size:13px;margin:0 0 16px">Νέα προγράμματα που ταιριάζουν στον ΚΑΔ σου</p>
      <table style="width:100%;border-collapse:collapse">{rows}</table>
      <p style="font-size:11px;color:#8A8F98;margin-top:20px">
        Λαμβάνεις αυτό το email επειδή ενεργοποίησες ειδοποιήσεις στο kratosNet.
        Απενεργοποίηση: άνοιξε το <a href="{APP_URL}">{APP_URL}</a> → Λογαριασμός → Ειδοποιήσεις.
        Η καταλληλότητα είναι ενδεικτική — πάντα έλεγχος στο PDF της πρόσκλησης.
      </p>
    </div>"""


def main():
    missing = [k for k, v in {
        "SUPABASE_URL": SUPABASE_URL, "SUPABASE_SERVICE_KEY": SUPABASE_SERVICE_KEY,
        "BREVO_API_KEY": BREVO_API_KEY, "NOTIFY_SENDER_EMAIL": SENDER_EMAIL,
    }.items() if not v]
    if missing:
        print(f"[skip] Λείπουν secrets: {', '.join(missing)} — παράλειψη ειδοποιήσεων "
              f"(δεν είναι σφάλμα αν δεν έχει στηθεί ακόμα το notification σύστημα).", file=sys.stderr)
        return 0

    if not PROGRAMS_PATH.exists():
        print("[skip] Δεν υπάρχει programs.json", file=sys.stderr)
        return 0
    data = json.loads(PROGRAMS_PATH.read_text(encoding="utf-8"))
    programs = data.get("programs", [])
    if not programs:
        print("[skip] Κενά δεδομένα προγραμμάτων", file=sys.stderr)
        return 0

    # Χρήστες με ενεργοποιημένες ειδοποιήσεις + συμπληρωμένο ΚΑΔ
    users = http_json(
        f"{SUPABASE_URL}/rest/v1/profiles?notify_email=eq.true&kad=neq.&select=id,email,kad,region",
        headers=sb_headers(),
    ) or []
    print(f"[ok] {len(users)} χρήστες με ενεργές ειδοποιήσεις", file=sys.stderr)

    sent_count = 0
    for user in users:
        if sent_count >= 290:  # μαξιλάρι κάτω από το όριο 300/ημέρα του Brevo
            print("[warn] Πλησιάζουμε το ημερήσιο όριο Brevo — οι υπόλοιποι αύριο.", file=sys.stderr)
            break
        uid, email = user["id"], user.get("email")
        if not email:
            continue
        user_tags = guess_tags(user.get("kad", ""))
        if not user_tags:
            continue

        already = http_json(
            f"{SUPABASE_URL}/rest/v1/notified?user_id=eq.{uid}&select=program_id",
            headers=sb_headers(),
        ) or []
        already_ids = {row["program_id"] for row in already}

        fresh = [
            p for p in programs
            if (p.get("id") or p.get("title")) not in already_ids
            and program_matches(p, user_tags, user.get("region", ""))
        ]
        if not fresh:
            continue

        ok = send_email(email, f"kratosNet: {len(fresh)} νέο(α) πρόγραμμα(τα) για τον ΚΑΔ σου", render_email(fresh))
        if ok:
            rows = [{"user_id": uid, "program_id": (p.get("id") or p.get("title"))} for p in fresh]
            http_json(
                f"{SUPABASE_URL}/rest/v1/notified",
                method="POST",
                headers=sb_headers({"Prefer": "resolution=ignore-duplicates"}),
                body=rows,
            )
            sent_count += 1
            print(f"[sent] {email}: {len(fresh)} προγράμματα", file=sys.stderr)

    print(f"[done] Στάλθηκαν {sent_count} emails", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
