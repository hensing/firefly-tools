#!/usr/bin/env python3
"""
Post-process Firefly III bookings after the nightly import.

Division of labour: the Data Importer fetches and deduplicates, Firefly rules
classify (counterparty, category, budget, tags). This script fills in what a
rule cannot set and never touches the classification:

  - foreign amount + currency from the bank text (DKB card payments)
  - purchase date from the bank text (DKB card payments)
  - a readable description instead of bank boilerplate; the original text
    goes to the notes ("Original: ..."), real transfer texts stay untouched
  - with --jobs (the importer's job files): the counterparty name the bank sent
    into the notes, a mail when it doesn't fit the account Firefly picked, and the
    merchant for card refunds that landed on the DKB clearing placeholder
  - Wise: the two legs of a currency conversion become one transfer, and a top-up that
    an own account already shows as a transfer is dropped
  - shared IBANs (DKB card clearing, PayPal, card acquirers) cleared from any
    merchant account that picked one up
  - empty accounts the importer left behind (raw bank name, booking moved by a
    counterparty rule) deleted
  - warnings by mail: bookings it could not handle, uncategorised bookings,
    imports that stopped arriving
  - --forecast: mail if DKB Giro may drop below 500 EUR within the next days
    (subscriptions due from Giro + open credit card statements)

Work queue: the rule "Post-process: tag new bookings" puts the tag
_postprocess on every new booking. A processed booking loses the tag; one
with a problem keeps it, gets the tag _fixme and is listed in the mail (both
tags go once a later run finds it fixed). Processed bookings are
never touched again, so manual edits in Firefly are safe.

Usage (FIREFLY_URL, FIREFLY_TOKEN set; dry run without --execute):
    python ff_postprocess.py                      # bookings tagged _postprocess
    python ff_postprocess.py --execute [--jobs DIR]  # DIR: the importer's import-jobs
    python ff_postprocess.py --backfill 2022-01-01 2022-12-31 [--execute]
    python ff_postprocess.py --forecast [--no-mail]  # read-only, mails a warning

Exit: 0 done, 1 problems (mailed), 2 crash (mailed). Mail (optional, else stdout
only): SMTP_HOST, SMTP_PORT, SMTP_USER,
SMTP_PASSWORD, MAIL_FROM, MAIL_TO. Standard library only (runs in a plain
python container).
"""

import argparse
import json
import os
import re
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from email.message import EmailMessage

TAG = "_postprocess"
FIXME = "_fixme"   # on bookings with a problem, so they can be found in Firefly/Waterfly
CARD_ACCOUNTS = {"AMEX", "Miles & More DB"}   # description = raw merchant text
# Bank booking texts that are a good description already (the counterparty
# name would hide what the booking is): lowercase text -> description.
BOOKING_TEXTS = {"monatlicher kartenpreis": "monatlicher Kartenpreis"}
STALE_DAYS = {"DKB Giro": 7}                  # warn if no booking for N days
# Revenue accounts holding the DKB card clearing IBAN, so Firefly never stores it on a
# merchant (it matches by IBAN before name). Card refunds land there without the merchant.
CLEARING = {"DKB card clearing", "DKB card clearing 2"}
CLEARING_IBAN = "DE96120300009005290904"
# IBANs that many merchants share. A merchant account holding one gets every later
# booking with it, so the script clears them (the DKB one also stays blocked above).
SHARED_IBANS = {
    CLEARING_IBAN,
    "DE88500700100175526303",  # PayPal
    "LU89751000135104200E",    # PayPal Europe
    "DE51300500000001474915",  # card acquirer (bakery, florist, photo shop, ...)
    "DE67500500000959567025",  # card acquirer (cafe, petrol station, ...)
    "DE86300500000001052141",  # card acquirer (cafe, deli, bar)
    "DE89500500000959567017",  # card acquirer (Spain)
}
COUNTERPARTY_GROUP = "Managed: Counterparties"   # rules that map bank names to accounts
EMPTY_DAYS = 14   # empty accounts created within this many days are deleted (importer leftovers)
NO_PLACE = set()   # counterparties whose account notes say "place: no" (filled in main)
# Forecast: card statements close on day C of the month and are debited from Giro
# on day D at the earliest (2025-12..2026-10: AMEX and M&M close ~2nd, debit 6th-10th)
FORECAST_ACCOUNT, FORECAST_DAYS = "DKB Giro", 7
FORECAST_MIN = 500   # EUR: mail when the lowest expected balance drops below this
CARD_PAYMENTS = {"AMEX": (2, 6), "Miles & More DB": (2, 6)}

FOREIGN = [re.compile(p) for p in (
    r"Ursprungsbetrag in Fremdw\S+ ([\d.,]+) ([A-Z]{3})\b",  # DKB since 2024
    r"\bOriginal ([\d.,]+) ([A-Z]{3})\b",                    # DKB until 2023
    r"\bCard transaction of ([\d.,]+) ([A-Z]{3})\b",         # Wise
    r"Foreign Spend Amount: ([\d.,]+) (.+?) Commission",     # AMEX (notes), names
)]
CURRENCY_NAMES = {   # AMEX writes names; an unknown name keeps the tag
    "US Dollars": "USD", "Canadian Dollars": "CAD", "Australian Dollars": "AUD",
    "Singapore Dollars": "SGD", "Namibian Dollars": "NAD", "Pounds Sterling": "GBP",
    "British Pounds": "GBP", "Swiss Francs": "CHF", "Danish Kroner": "DKK",
    "Danish Krone": "DKK", "Icelandic Krona": "ISK", "Iceland Krona": "ISK",
    "South African Rand": "ZAR", "Botswana Pula": "BWP", "Peruvian Sol": "PEN",
    "Thai Baht": "THB", "Polish Zloty": "PLN", "Swedish Krona": "SEK",
    "Norwegian Krone": "NOK", "Japanese Yen": "JPY", "Mexican Pesos": "MXN",
}
PURCHASE = [
    (re.compile(r"Debitkartenumsatz vom (\d\d)\.(\d\d)\.(\d{4})"),
     lambda m: f"{m[3]}-{m[2]}-{m[1]}"),
    (re.compile(r"^(\d{4}-\d\d-\d\d)T(\d\d:\d\d)\s+Debitk\."),
     lambda m: f"{m[1]}T{m[2]}:00"),
]
BOILERPLATE = re.compile(
    r"^(VISA Debitkartenumsatz"                 # DKB card since 2024
    r"|\d{4}-\d\d-\d\dT\d\d:\d\d\s+Debitk\."    # DKB card until 2023
    r"|CARD-\d+ Card transaction"               # Wise card
    r"|\(no description\)$"                     # PayPal, Tagesgeld, ...
    r"|(?=\S*\d)[0-9A-Z]{16,}$)")              # reference/card number only


# ---------------------------------------------------------------- Firefly API

def api(method, path, body=None):
    url = os.environ["FIREFLY_URL"].rstrip("/") + "/api/v1/" + path
    req = urllib.request.Request(url, method=method, headers={
        "Authorization": "Bearer " + os.environ["FIREFLY_TOKEN"],
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/json",
        **({"X-Firefly-Gate": os.environ["FIREFLY_HEADER_SECRET"]}
           if os.environ.get("FIREFLY_HEADER_SECRET") else {}),
    }, data=json.dumps(body).encode() if body is not None else None)
    # GET/PUT are safe to repeat after a dropped connection; POST is not
    attempts = 1 if method == "POST" else 3
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read()
            return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {e.read().decode()[:500]}")
        except OSError as e:  # URLError, timeouts, connection resets
            if attempt == attempts:
                raise
            print(f"  {method} {path}: {e}, retrying", file=sys.stderr)
            time.sleep(5 * attempt)


def get_all(path):
    sep = "&" if "?" in path else "?"
    page, data = 1, []
    while True:
        resp = api("GET", f"{path}{sep}limit=200&page={page}")
        data += resp["data"]
        if page >= resp["meta"]["pagination"]["total_pages"]:
            return data
        page += 1


# ---------------------------------------------------------------- bank names

# Firefly picks the account by IBAN before name and keeps no trace of the name the
# bank sent. The importer's job files (copied out by the wrapper, --jobs) still have
# it: external ID + amount -> the counterparty name as sent.
BANK_NAMES = {}
UNKNOWN_NAMES = {"(unknown destination)", "(unknown source)"}
ACCOUNT_PATTERNS = []   # (side, kind, value, account) from the counterparty rules


def load_jobs(folder):
    for path in sorted(os.listdir(folder)):
        if not path.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, path), encoding="utf-8") as fh:
                job = json.load(fh)
        except (OSError, ValueError):
            continue
        for group in job.get("converted_transactions") or []:
            for t in group.get("transactions") or []:
                name = t.get("destination_name") if t.get("type") == "withdrawal" else t.get("source_name")
                if t.get("external_id") and name and name not in UNKNOWN_NAMES:
                    BANK_NAMES[(t["external_id"], f"{abs(float(t['amount'])):.2f}")] = name


def bank_name(s):
    return BANK_NAMES.get((s["external_id"] or "", f"{abs(float(s['amount'])):.2f}"))


def load_account_patterns():
    """starts/contains/is triggers on the counterparty -> account they set. Rules with an
    expression as the account (Wise, REI) are left out: only Firefly can evaluate those."""
    group = next((g for g in get_all("rule-groups") if g["attributes"]["title"] == COUNTERPARTY_GROUP), None)
    if not group:
        return
    for r in get_all(f"rule-groups/{group['id']}/rules"):
        a = r["attributes"]
        target = next((x for x in a["actions"] if x["type"] in ("set_destination_account", "set_source_account")), None)
        if not a["active"] or not target or target["value"].startswith("="):
            continue
        for t in a["triggers"]:
            m = re.fullmatch(r"(destination|source)_account_(starts|ends|contains|is)", t["type"])
            if m and not t.get("prohibited"):
                ACCOUNT_PATTERNS.append((m[1], m[2], t["value"].casefold(), target["value"]))


def rule_account(name, side):
    """The account the counterparty rules would give a bank name, or None."""
    n = name.casefold()
    for s, kind, value, account in ACCOUNT_PATTERNS:
        if s == side and (n == value if kind == "is" else n.startswith(value) if kind == "starts"
                          else n.endswith(value) if kind == "ends" else value in n):
            return account
    return None


GENERIC_WORDS = {"the", "and", "inc", "llc", "ltd", "gmbh", "co", "kg", "ag", "store", "shop",
                 "bar", "cafe", "restaurant", "market", "service", "online", "www", "com"}


def words(text):
    return {w for w in re.findall(r"[a-z0-9]{3,}", re.sub(r"[^a-z0-9]+", " ", text.casefold()))
            if not w.isdigit() and w not in GENERIC_WORDS}


def fits(name, account, side):
    """Does the name the bank sent belong to the account the booking ended up on?
    'Starbucks Store 11630 HANOVER' -> Starbucks yes, 'BJ.S.FUEL..9321/SALEM' -> Hilton no."""
    if name.casefold() == account.casefold() or rule_account(name, side) == account:
        return True
    squashed = re.sub(r"[^a-z0-9]", "", name.casefold())
    return bool(words(name) & words(account)) or any(w in squashed for w in words(account))


def with_bank_counterparty(notes, name):
    """Notes with the line 'Bank counterparty: <name>' (replaced, else appended)."""
    line = f"Bank counterparty: {name}"
    if re.search(r"^Bank counterparty: .*$", notes or "", re.M):
        return re.sub(r"^Bank counterparty: .*$", lambda _: line, notes, count=1, flags=re.M)
    return (notes.rstrip() + "\n\n" + line) if notes else line


def refund_merchant(name):
    """(account, category) for a card refund that landed on the clearing placeholder:
    the merchant the counterparty rules give the bank name, else an expense account of
    exactly that name; the category is the one most used on its purchases."""
    merchant = rule_account(name, "destination")
    q = urllib.parse.urlencode({"field": "name", "query": merchant or name, "type": "expense"})
    acc = next((a for a in api("GET", f"search/accounts?{q}")["data"]
                if a["attributes"]["name"].casefold() == (merchant or name).casefold()), None)
    if not acc:
        return None, None
    cats = {}
    for g in api("GET", f"accounts/{acc['id']}/transactions?type=withdrawal&limit=50")["data"]:
        for s in g["attributes"]["transactions"]:
            if s["category_name"]:
                cats[s["category_name"]] = cats.get(s["category_name"], 0) + 1
    return acc["attributes"]["name"], max(cats, key=cats.get) if cats else None


# ---------------------------------------------------------------- per booking

def decimal(text):
    """'4.227,31' / '4227,31' / '1,162.80' / '1162.80' -> '4227.31' ..."""
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            return text.replace(".", "").replace(",", ".")
        return text.replace(",", "")
    return text.replace(",", ".")


def counterparty(s):
    return s["source_name"] if s["type"] == "deposit" else s["destination_name"]


def own_account(s):
    return s["destination_name"] if s["type"] == "deposit" else s["source_name"]


def original_text(s):
    """Bank text: the description, or the copy kept in the notes."""
    m = re.match(r"Original: (.*)", s["notes"] or "")
    return m[1] if m else s["description"] or ""


def nice_description(s, overrides):
    """New description, or None to keep the current one."""
    desc = s["description"] or ""
    if (s["notes"] or "").startswith("Original: "):
        # done before; only add a place to a description that is still exactly
        # the counterparty name (anything else was edited by hand)
        name = counterparty(s)
        if s["type"] == "transfer" or desc.removesuffix(" (tip)") != name:
            return None
        new = with_place(name, place(s, name))
        return new if new != name else None
    if s["type"] == "transfer":
        # real transfer texts stay, only empty/reference-only ones go
        if desc == "(no description)" or re.fullmatch(r"(?=\S*\d)[0-9A-Z]{16,}", desc):
            return "Transfer"
        return None
    if desc.strip().lower() in BOOKING_TEXTS:
        return BOOKING_TEXTS[desc.strip().lower()]
    if counterparty(s) in overrides:   # set per merchant: replaces any bank text
        name = from_template(overrides[counterparty(s)], desc)
        return with_place(name, place(s, name))
    name = counterparty(s)
    if BOILERPLATE.match(desc):
        return with_place(name, place(s, name))
    # AMEX / M&M: the description is the merchant string ("AMAZON MKTPL*5E8...").
    # A single word is a booking text instead ("AUSLANDSEINSATZENTGELT").
    if own_account(s) in CARD_ACCOUNTS and re.search(r"\s", desc.strip()):
        return with_place(name, place(s, name))
    return None


# Where a card payment happened. Accounts are the merchant only (one account per
# merchant, not per branch); the place goes into the description instead.
PROCESSOR = re.compile(r"^[A-Za-z]{2,6} ?\*\s*")   # "Sq *", "Tst*", ... (Square, Toast)
PLACE_WORDS = re.compile(r"[A-Za-z][A-Za-z '-]{2,}")   # no dots: "AMAZON.COM" is no place
CITY_CAPS = re.compile(r"\b[A-Z]{3,}(?: [A-Z]{3,})*$")   # trailing "BROOKLINE", "NEW YORK"


def place(s, name):
    """'Portland' for 'Sq *Handlebar Cafe Portland', or None. Only for payments
    abroad (foreign currency): at home the text names the company seat instead
    ("LUFTHANSA ... KOELN")."""
    if counterparty(s) in NO_PLACE:   # e.g. US Customs: the city is just its office
        return None
    text, notes = original_text(s), s["notes"] or ""
    abroad = (s["currency_code"] != "EUR" or s.get("foreign_currency_code")
              or any(rx.search(text + "\n" + notes) for rx in FOREIGN))
    if not abroad:
        return None
    # DKB card: the bank's counterparty name, kept by the managed rule
    # "Keep the bank counterparty of DKB card payments" ("SQ..HANDLEBAR.CAFE/Portland")
    m = re.search(r"^Bank counterparty: .*/([^/\n]+)$", notes, re.M)
    if m:   # online shops have their website there: "WWW.USMOBILE.COM", "GOODRX.COM"
        if re.search(r"(?i)^(www|https?)|\.(com|org|net|io|co|app|de|uk)\b", m[1]):
            return None
        return m[1].replace(".", " ").strip()
    # Wise: "... issued by Sq *Handlebar Cafe Portland" -> the rest after the merchant
    m = re.search(r"issued by (.+)$", text)
    if m:
        raw = PROCESSOR.sub("", m[1]).strip()
        if not raw.lower().startswith(name.lower()):
            # chains spell themselves differently, but put the city after the
            # store number: "Cvs/pharmacy #10852 SALEM", "Trader Joe S #514 WEST SALEM"
            m = re.search(r"#?\d{2,} ([A-Z]{3,}(?: [A-Z]{3,})*)$", raw)
            return m[1] if m else None
        rest = raw[len(name):].strip()
        # Wise cuts long merchant names before the city: "Tatte Bakery Brookli BROOKLINE"
        m = CITY_CAPS.search(rest)
        if m:
            return m[0]
        words = rest.split()
        return rest if 0 < len(words) <= 2 and all(w[0].isupper() for w in words) else None
    # M&M: "USCUSTOMS ESTA APPL PM, 46278 INDIANAPOLIS, IN, USA" (abroad only:
    # at home the city is the company seat, "LUFTHANSA ..., 50672 KOLN, DEU")
    m = re.search(r",\s*\d{4,5}\s+([^,]+)(?:,\s*[A-Z]{2})?,\s*([A-Z]{3})$", text)
    if m:
        return m[1] if m[2] != "DEU" else None
    # AMEX: merchant and city in fixed columns, separated by 2+ spaces
    if own_account(s) == "AMEX":
        m = re.search(r"\S\s{2,}(\S.*)$", text)
        return m[1] if m else None
    return None


def with_place(name, where):
    """'Handlebar Cafe' + 'PORTLAND' -> 'Handlebar Cafe Portland' (if it looks like a place)."""
    where = re.sub(r"\s+", " ", where or "").strip()
    if not where or not PLACE_WORDS.fullmatch(where) or where.lower() in name.lower():
        return name
    # the merchant again instead of a place: "Rapid Translate" + "RAPIDTRANSLAT"
    squashed = where.lower().replace(" ", "")
    if any(len(w) >= 5 and w in squashed for w in re.findall(r"[a-z]+", name.lower())):
        return name
    where = re.sub(r"([a-z])([A-Z])", r"\1 \2", where)        # "NewYork"
    if not where.istitle():                                     # "NEW YORK", "WAshington"
        where = where.title()
    return f"{name} {where}"


def is_tip(s):
    """Wise books a restaurant tip as a second, smaller booking a day later
    with the same card transaction ID (36.38 + 1.00 = 'Card transaction of 37.38')."""
    if not (s["external_id"] and ".CARD-" in s["external_id"]):
        return False
    q = urllib.parse.quote(f'external_id_is:"{s["external_id"]}"')
    others = [o for g in api("GET", f"search/transactions?query={q}")["data"]
              for o in g["attributes"]["transactions"]
              if o["transaction_journal_id"] != s["transaction_journal_id"]]
    return any(float(o["amount"]) > float(s["amount"]) for o in others)


def plan_split(s, overrides):
    """-> (changes dict, list of problems)"""
    changes, problems = {}, []

    # The name the bank sent (job files): into the notes before the place is read
    # from them, and checked against the account Firefly picked.
    raw = bank_name(s) if BANK_NAMES and s["type"] != "transfer" else None
    if raw:
        notes = with_bank_counterparty(s["notes"], raw)
        if notes != (s["notes"] or ""):
            changes["notes"] = notes
            s = {**s, "notes": notes}
        side = "source" if s["type"] == "deposit" else "destination"
        if s["type"] == "deposit" and counterparty(s) in CLEARING:
            merchant, category = refund_merchant(raw)
            if merchant:   # card refund: the merchant as revenue account, the purchases' category
                changes.update(source_name=merchant, description=merchant,
                               tags=sorted(set(s["tags"] or []) | {"refund"}),
                               notes="Original: " + s["description"] + "\n\n" + s["notes"])
                if category and not s["category_name"]:
                    changes["category_name"] = category
                s = {**s, "source_name": merchant, "description": merchant, "notes": changes["notes"]}
            else:
                problems.append(f"card refund from {raw!r}: no rule or expense account for it, "
                                "set the merchant by hand")
        elif not fits(raw, counterparty(s), side):
            problems.append(f"the bank sent {raw!r} but Firefly booked it on {counterparty(s)!r} "
                            "(shared IBAN on that account?)")
    text = original_text(s)

    # Not on transfers: Firefly credits the foreign amount to the destination,
    # so a DKB -> Paypal top-up "of 89 USD" would land as USD in the EUR account.
    if not s["foreign_currency_code"] and s["type"] != "transfer":
        for rx in FOREIGN:
            m = rx.search(text + "\n" + (s["notes"] or ""))
            if not m:
                continue
            code = m[2] if re.fullmatch(r"[A-Z]{3}", m[2]) else CURRENCY_NAMES.get(m[2])
            if not code:
                problems.append(f"unknown currency name {m[2]!r} (add it to CURRENCY_NAMES)")
            elif code != s["currency_code"]:
                changes["foreign_amount"] = decimal(m[1])
                changes["foreign_currency_code"] = code
            break

    # Card payment date as the booking's date (DKB books days later); the
    # bank's date goes to book_date. A set book_date means done (or manual).
    if not s["book_date"]:
        for rx, fmt in PURCHASE:
            m = rx.search(text)
            if m and fmt(m)[:10] != s["date"][:10]:
                changes["date"] = fmt(m)
                changes["book_date"] = s["date"]
                break

    new = nice_description(s, overrides)
    if new and is_tip(s):
        new += " (tip)"
    if new and new != s["description"]:
        changes["description"] = new
        if not (s["notes"] or "").startswith("Original: "):
            changes["notes"] = "Original: " + s["description"] + (
                "\n\n" + s["notes"] if s["notes"] else "")

    if counterparty(s) in CLEARING and not raw:
        problems.append("booked on the DKB card clearing placeholder: set the merchant "
                        "from the bank export (Zahlungsempfaenger)")
    if own_account(s) == "Paypal" and s["currency_code"] != "EUR":
        problems.append(f"PayPal booking in {s['currency_code']} without EUR amount "
                        "(take it from the AMEX -> Paypal transfer)")
    return changes, problems


def process(groups, overrides, execute, drop_tag):
    """-> done ids, failed [(line, new)], uncategorised lines. new = the problem showed up
    in this run (the booking had no _fixme yet)."""
    done, failed, uncategorised = [], [], []
    for g in groups:
        splits = g["attributes"]["transactions"]
        body, problems, changed = [], [], False
        for s in splits:
            ch, pr = plan_split(s, overrides)
            problems += pr
            tags = ch.get("tags", s["tags"] or [])
            if pr and FIXME not in tags:
                ch["tags"] = tags + [FIXME]
            elif drop_tag and TAG in tags and not pr:   # fixed since: both tags go
                ch["tags"] = [t for t in tags if t not in (TAG, FIXME)]
            changed |= bool(ch)
            body.append({"transaction_journal_id": s["transaction_journal_id"], **ch})
            after = {**s, **ch}   # as written: a card refund gets its category in this run
            if after["type"] != "transfer" and not after["category_name"]:
                uncategorised.append(line(g, after))
            for k, v in ch.items():
                if k not in ("tags", "notes"):
                    print(f"  #{g['id']} {s['date'][:10]} {k}: {s.get(k)!r} -> {v!r}")
        if problems:
            new = any(FIXME not in (s["tags"] or []) for s in splits)
            failed.append((line(g, splits[0]) + " -- " + "; ".join(problems), new))
        if not changed:
            continue
        if execute:
            try:
                # all splits in one PUT: Firefly drops splits missing from an update
                api("PUT", f"transactions/{g['id']}",
                    {"apply_rules": False, "fire_webhooks": False, "transactions": body,
                     # mandatory for split bookings
                     **({"group_title": g["attributes"]["group_title"]} if len(splits) > 1 else {})})
            except RuntimeError as e:
                failed.append((line(g, splits[0]) + f" -- update failed: {e}", True))
                continue
        done.append(g["id"])
    return done, failed, uncategorised


# ---------------------------------------------------------------- Wise

# Wise sends no counterparty. A conversion arrives as a withdrawal and a deposit with the
# same reference; a top-up as a deposit, besides the transfer the paying bank shows
# (Wise account = asset account with its IBAN, so the importer makes it a transfer).
CONVERSION = re.compile(r"^((?:CONVERSION_ORDER|BALANCE)-\d+) Converted ([\d.,]+) ([A-Z]{3}) to ([\d.,]+) ([A-Z]{3})")
TOPUP = re.compile(r"^(TRANSFER-\d+) Topped up account$")
PAIR_DAYS = 4   # the two sides of a top-up: instant at Wise, next banking day at the bank


def add_note(s, text):
    return f"{s['notes'].rstrip()}\n\n{text}" if s["notes"] else text


def put_split(g, s, **changes):
    api("PUT", f"transactions/{g['id']}", {
        "apply_rules": False, "fire_webhooks": False,
        "transactions": [{"transaction_journal_id": s["transaction_journal_id"], **changes}]})


def wise_pairs(groups, execute):
    """-> (groups left to process, problem lines as in process()). Only bookings whose
    Wise side is still unknown; a group with several splits is never touched."""
    single = [(g, g["attributes"]["transactions"][0]) for g in groups
              if len(g["attributes"]["transactions"]) == 1]
    open_ = [(g, s) for g, s in single if s["type"] != "transfer" and counterparty(s) in UNKNOWN_NAMES]
    gone, failed = set(), []

    def problem(g, s, text):
        failed.append((line(g, s) + " -- " + text, FIXME not in (s["tags"] or [])))
        gone.add(g["id"])   # keeps its tag for the next run
        if execute and FIXME not in (s["tags"] or []):
            put_split(g, s, tags=(s["tags"] or []) + [FIXME])

    legs = {}
    for g, s in open_:
        m = CONVERSION.match(s["description"] or "")
        if m:
            legs.setdefault(m[1], []).append((g, s, m))
    for ref, found in legs.items():
        outs = [x for x in found if x[1]["type"] == "withdrawal"]
        ins = [x for x in found if x[1]["type"] == "deposit"]
        if len(outs) != 1 or len(ins) != 1:
            for g, s, _ in found:
                problem(g, s, f"conversion {ref}: {len(outs)} withdrawal(s) and {len(ins)} deposit(s), "
                              "expected one each")
            continue
        (g, s, m), (g2, s2, _) = outs[0], ins[0]
        # the bank text names both currencies and the credited amount
        if (m[3], m[5]) != (s["currency_code"], s2["currency_code"]) \
                or abs(float(decimal(m[4])) - float(s2["amount"])) > 0.005:
            problem(g, s, f"conversion {ref}: text and amounts differ")
            problem(g2, s2, f"conversion {ref}: text and amounts differ")
            continue
        print(f"  #{g['id']} conversion {ref}: transfer {s['source_name']} {float(s['amount']):.2f} "
              f"{s['currency_code']} -> {s2['destination_name']} {float(s2['amount']):.2f} "
              f"{s2['currency_code']}, drop #{g2['id']}")
        if execute:
            put_split(g, s, type="transfer", source_id=s["source_id"], destination_id=s2["destination_id"],
                      foreign_amount=s2["amount"], foreign_currency_code=s2["currency_code"],
                      description=f"Converted {m[2]} {m[3]} to {m[4]} {m[5]}",
                      notes=add_note(s, "Original: " + s["description"]),
                      tags=[t for t in s["tags"] or [] if t not in (TAG, FIXME)])
            # first make sure the transfer really is one: Firefly may ignore a field
            n = api("GET", f"transactions/{g['id']}")["data"]["attributes"]["transactions"][0]
            if (n["type"], n["destination_id"], n["foreign_currency_code"]) != (
                    "transfer", s2["destination_id"], s2["currency_code"]) \
                    or abs(float(n["amount"]) - float(s["amount"])) > 0.005 \
                    or abs(float(n["foreign_amount"] or 0) - float(s2["amount"])) > 0.005:
                problem(g2, s2, f"conversion {ref}: #{g['id']} did not become the expected transfer")
                gone.add(g["id"])
                continue
            api("DELETE", f"transactions/{g2['id']}")
        gone |= {g["id"], g2["id"]}

    for g, s in open_:
        m = TOPUP.match(s["description"] or "")
        if not m or s["type"] != "deposit":
            continue
        day = date.fromisoformat(s["date"][:10])
        found = [(h, t) for h in get_all(f"accounts/{s['destination_id']}/transactions?"
                                         f"start={day - timedelta(days=PAIR_DAYS)}"
                                         f"&end={day + timedelta(days=PAIR_DAYS)}")
                 for t in h["attributes"]["transactions"]
                 if len(h["attributes"]["transactions"]) == 1
                 and t["type"] == "transfer"   # this endpoint's type filter lets deposits through
                 and t["destination_id"] == s["destination_id"]
                 and t["currency_code"] == s["currency_code"]
                 and abs(float(t["amount"]) - float(s["amount"])) < 0.005
                 and "Wise: TRANSFER-" not in (t["notes"] or "")]   # already paired
        if len(found) == 1:
            h, t = found[0]
            print(f"  #{g['id']} top-up {m[1]}: is transfer #{h['id']} from {t['source_name']}, drop #{g['id']}")
            if execute:
                notes = add_note(t, f"Wise: {m[1]}")
                put_split(h, t, notes=notes)
                api("DELETE", f"transactions/{g['id']}")
                for x in groups:   # process() below must not work with the old notes
                    if x["id"] == h["id"]:
                        x["attributes"]["transactions"][0]["notes"] = notes
            gone.add(g["id"])
        elif len(found) > 1:
            problem(g, s, "top-up matches several transfers: "
                          + ", ".join(f"#{h['id']}" for h, _ in found))
        elif (date.today() - day).days <= PAIR_DAYS:
            print(f"  #{g['id']} top-up {m[1]}: no transfer from an own account yet, waiting")
            gone.add(g["id"])
        else:
            problem(g, s, "top-up without a transfer from an own account: set where the money came from")
    return [g for g in groups if g["id"] not in gone], failed


def line(g, s):
    return (f"#{g['id']} {s['date'][:10]} {own_account(s)} -> {counterparty(s)} "
            f"{float(s['amount']):.2f} {s['currency_code']}: {s['description'][:60]}")


# ---------------------------------------------------------------- checks / mail

# "{period}" in an override: the billing period of a direct debit
# ("... All-In -2 21.78 EUR 08.10.26-21.10.26", "... 17.08.-30.08.23"), else a date
PERIOD = re.compile(r"\b\d\d\.\d\d\.(?:\d\d)?\s*-\s*\d\d\.\d\d\.\d\d\b")
ONE_DATE = re.compile(r"\b\d\d\.\d\d\.\d\d\b")


def from_template(template, text):
    """'Fitness First {period}' -> 'Fitness First 08.10.26-21.10.26'."""
    m = PERIOD.search(text) or ONE_DATE.search(text)
    return re.sub(r"\s*\{period\}", " " + m[0].replace(" ", "") if m else "", template).strip()


def description_overrides():
    """Expense/revenue account notes may say 'description: Laundry' (or
    'description: Fitness First {period}'): that replaces the bank text of
    every booking with this counterparty (not transfers); original into the notes.
    A line 'place: no' keeps the place out of the description (fills NO_PLACE)."""
    out = {}
    for kind in ("expense", "revenue"):
        for a in get_all(f"accounts?type={kind}"):
            notes = a["attributes"].get("notes") or ""
            m = re.search(r"^description:\s*(.+)$", notes, re.M)
            if m:
                out[a["attributes"]["name"]] = m[1].strip()
            if re.search(r"^place:\s*no\s*$", notes, re.M | re.I):
                NO_PLACE.add(a["attributes"]["name"])
    return out


def stale_imports():
    warn = []
    accounts = {a["attributes"]["name"]: a["id"] for a in get_all("accounts?type=asset")}
    for name, days in STALE_DAYS.items():
        last = api("GET", f"accounts/{accounts[name]}/transactions?limit=1")["data"]
        newest = last[0]["attributes"]["transactions"][0]["date"][:10] if last else "never"
        if newest < (date.today() - timedelta(days=days)).isoformat():
            warn.append(f"{name}: newest booking {newest} (more than {days} days ago)")
    return warn


def shared_ibans(execute):
    """No merchant may hold an IBAN that many merchants share (Firefly matches by IBAN
    before name, so it would get every later booking with it): cleared on expense and
    revenue accounts, reported otherwise. The two DKB placeholders must keep theirs, so
    the clearing IBAN can't land on a new merchant during an import in the first place."""
    warn = []
    for iban in sorted(SHARED_IBANS):
        q = urllib.parse.urlencode({"field": "iban", "query": iban, "type": "all"})
        holders = [a for a in get_all(f"search/accounts?{q}") if a["attributes"]["iban"] == iban]
        if iban == CLEARING_IBAN:
            warn += [f"{n}: placeholder lost the IBAN {iban} (Firefly clears it on every "
                     "container start; the import wrapper restores it, check its log)"
                     for n in sorted(CLEARING - {a["attributes"]["name"] for a in holders})]
        for a in holders:
            name, kind = a["attributes"]["name"], a["attributes"]["type"]
            if iban == CLEARING_IBAN and name in CLEARING:
                continue
            if kind in ("expense", "revenue") and execute:
                api("PUT", f"accounts/{a['id']}", {"iban": ""})
                warn.append(f"{name}: held the shared IBAN {iban}, cleared it; check the bookings "
                            f"on it since {a['attributes']['created_at'][:10]}")
            else:
                warn.append(f"{name} ({kind}): holds the shared IBAN {iban}, "
                            f"{'would clear it' if kind in ('expense', 'revenue') else 'remove it by hand'}")
    return warn


def empty_accounts(execute):
    """The importer creates an account for the raw bank name; when a counterparty rule
    moves the booking to the clean merchant, that account stays behind empty. Deleted if
    created within EMPTY_DAYS, without IBAN, account number or notes, not a placeholder
    and not named by any rule. Logged only (no problem)."""
    keep = set(CLEARING)
    for r in get_all("rules"):
        for x in r["attributes"]["triggers"] + r["attributes"]["actions"]:
            if "account" in x["type"] and not str(x["value"]).startswith("="):
                keep.add(str(x["value"]))
    since = (date.today() - timedelta(days=EMPTY_DAYS)).isoformat()
    out = []
    for kind in ("expense", "revenue"):
        for a in get_all(f"accounts?type={kind}"):
            at = a["attributes"]
            if (at["created_at"][:10] < since or at["name"] in keep or at["name"] in UNKNOWN_NAMES
                    or at["iban"] or at["account_number"] or at["notes"]):
                continue
            if api("GET", f"accounts/{a['id']}/transactions?limit=1")["data"]:
                continue
            if execute:
                api("DELETE", f"accounts/{a['id']}")
            out.append(f"{'deleted' if execute else 'would delete'} empty {kind} account "
                       f"#{a['id']} {at['name']!r} (created {at['created_at'][:10]})")
    return out


def month_day(year, month, day):
    year, month = (year + 1, 1) if month == 13 else (year - 1, 12) if month == 0 else (year, month)
    return date(year, month, day)


def forecast(today=None):
    """Expected debits on FORECAST_ACCOUNT in the next FORECAST_DAYS: subscriptions
    last paid from it (amount_max, not yet paid; overdue ones count as today) and
    card statements not yet debited (balance on the closing day; a statement that
    closes inside the window counts with today's card balance). Income is not
    counted. Returns (balance, [(date, amount, text)], lowest, lowest_date)."""
    today = today or date.today()
    end = today + timedelta(days=FORECAST_DAYS)
    accounts = {a["attributes"]["name"]: a for a in get_all("accounts?type=asset")}
    giro = accounts[FORECAST_ACCOUNT]
    balance = float(giro["attributes"]["current_balance"])
    events = []
    # look back a few days for subscriptions that are due but not debited yet
    for b in get_all(f"bills?start={today - timedelta(days=5)}&end={end}"):
        a = b["attributes"]
        if not a["active"]:
            continue
        due = sorted(d[:10] for d in a["pay_dates"] if d[:10] <= end.isoformat())
        due = due[len(a["paid_dates"]):]   # the earliest ones are the paid ones
        if not due:
            continue
        last = api("GET", f"bills/{b['id']}/transactions?limit=1")["data"]
        if last and last[0]["attributes"]["transactions"][0]["source_name"] != FORECAST_ACCOUNT:
            continue   # paid by card/PayPal: in the card balance instead
        for d in due:
            events.append((max(date.fromisoformat(d), today), -float(a["amount_max"]),
                           f"subscription {a['name']} (up to {float(a['amount_max']):.2f})"))
    for card, (close, debit) in CARD_PAYMENTS.items():
        acc = accounts[card]
        newest = api("GET", f"accounts/{acc['id']}/transactions?limit=1")["data"]
        newest = newest[0]["attributes"]["transactions"][0]["date"][:10] if newest else "never"
        closed = month_day(today.year, today.month, close)
        if closed > today:
            closed = month_day(today.year, today.month - 1, close)
        # end one day later: Firefly rejects start == end (422) on the closing day itself
        paid = any(s["source_name"] == FORECAST_ACCOUNT
                   for g in get_all(f"accounts/{acc['id']}/transactions?type=transfer"
                                    f"&start={closed}&end={today + timedelta(days=1)}")
                   for s in g["attributes"]["transactions"])
        if not paid:
            at = api("GET", f"accounts/{acc['id']}?date={closed}")["data"]["attributes"]
            owed = -float(at["current_balance"])
            if owed > 0.005:
                events.append((max(month_day(closed.year, closed.month, debit), today), -owed,
                               f"{card} statement of {closed:%d.%m.} (newest card booking {newest})"))
        nxt = month_day(closed.year, closed.month + 1, close)
        if month_day(nxt.year, nxt.month, debit) <= end:
            owed = -float(acc["attributes"]["current_balance"])
            if owed > 0.005:
                events.append((month_day(nxt.year, nxt.month, debit), -owed,
                               f"{card} statement of {nxt:%d.%m.}, estimated with today's card balance "
                               f"(newest card booking {newest})"))
    events = sorted(e for e in events if e[0] <= end)
    run, lowest, lowest_date = balance, balance, today
    for d, amount, _ in events:
        run += amount
        if run < lowest:
            lowest, lowest_date = run, d
    return balance, events, lowest, lowest_date


def forecast_report(balance, events, lowest, lowest_date):
    run, lines = balance, [f"{date.today():%d.%m.}  balance {FORECAST_ACCOUNT:>27} {balance:10.2f}"]
    for d, amount, text in events:
        run += amount
        lines.append(f"{d:%d.%m.}  {amount:10.2f}  -> {run:10.2f}  {text}")
    lines.append(f"\nLowest: {lowest:.2f} EUR on {lowest_date:%d.%m.%Y} "
                 f"(next {FORECAST_DAYS} days; incoming money is not counted)")
    return "\n".join(lines)


def send_mail(subject, text):
    if not os.environ.get("SMTP_HOST"):
        print(f"\n(no SMTP_HOST, mail not sent)\n{subject}\n{text}")
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, os.environ["MAIL_FROM"], os.environ["MAIL_TO"]
    msg.set_content(text)
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", 587))) as smtp:
        smtp.starttls()
        if os.environ.get("SMTP_USER"):
            smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(msg)


def load_dotenv():
    """Local runs: take settings from .env next to the script (the container
    gets them via --env-file instead). Real environment variables win."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.removeprefix("export ").split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--execute", action="store_true", help="write to Firefly (else dry run)")
    ap.add_argument("--backfill", nargs=2, metavar=("START", "END"),
                    help="process all bookings in this date range, tagged or not")
    ap.add_argument("--forecast", action="store_true",
                    help=f"only the {FORECAST_ACCOUNT} forecast: mail if it may go below {FORECAST_MIN} EUR")
    ap.add_argument("--no-mail", action="store_true")
    ap.add_argument("--jobs", metavar="DIR",
                    help="the importer's job files: bank names into the notes, checked against "
                         "the account; card refunds on the clearing placeholder get the merchant")
    args = ap.parse_args()
    load_dotenv()
    for v in ("FIREFLY_URL", "FIREFLY_TOKEN"):
        if not os.environ.get(v):
            sys.exit(f"{v} not set")

    if args.forecast:
        result = forecast()
        report = forecast_report(*result)
        print(report)
        if result[2] < FORECAST_MIN and not args.no_mail:
            send_mail(f"Firefly: {FORECAST_ACCOUNT} may go below {FORECAST_MIN} EUR on {result[3]:%d.%m.} "
                      f"({result[2]:.2f} EUR)", report)
        return 0

    if args.backfill:
        start, end = args.backfill
        groups = get_all(f"transactions?type=all&start={start}&end={end}")
    else:
        q = urllib.parse.quote(f"tag_is:{TAG}")
        groups = get_all(f"search/transactions?query={q}")
    print(f"{len(groups)} booking(s) {'in ' + ' .. '.join(args.backfill) if args.backfill else 'tagged ' + TAG}"
          f"{'' if args.execute else ' (dry run)'}")

    if args.jobs:
        load_jobs(args.jobs)
        load_account_patterns()
        print(f"{len(BANK_NAMES)} bank name(s) from the job files, {len(ACCOUNT_PATTERNS)} rule pattern(s)")
    wise_failed = []
    if not args.backfill:
        groups, wise_failed = wise_pairs(groups, args.execute)
    done, failed, uncategorised = process(groups, description_overrides(), args.execute,
                                          drop_tag=True)
    failed = wise_failed + failed
    stale = stale_imports() if not args.backfill else []
    block = shared_ibans(args.execute) if not args.backfill else []
    for e in empty_accounts(args.execute) if not args.backfill else []:
        print(e)
    print(f"{len(done)} {'updated' if args.execute else 'to update'}, {len(failed)} with problems")

    # A problem is mailed when it shows up; the morning run (08:00 Berlin = 06/07 UTC,
    # the container runs in UTC) lists all open ones again.
    morning = time.gmtime().tm_hour < 12
    new = [f for f, is_new in failed if is_new]
    shown = [f for f, _ in failed] if morning or not args.execute or args.no_mail else new
    parts = []
    if shown:
        parts.append(f"Tagged {FIXME} (fix by hand; the next run removes {FIXME} and {TAG}):\n"
                     + "\n".join("  " + f for f in shown))
    if stale:
        parts.append("Import stalled?\n" + "\n".join("  " + s for s in stale))
    if block:
        parts.append("Shared IBANs (bookings may be on the wrong merchant):\n" + "\n".join("  " + b for b in block))
    if uncategorised and not args.backfill:
        parts.append("New bookings without category:\n" + "\n".join("  " + u for u in uncategorised))
    if parts and args.execute and not args.no_mail:
        send_mail(f"Firefly: {len(new) + len(block)} new problem(s), {len(failed)} open, "
                  f"{len(uncategorised)} uncategorised", "\n\n".join(parts))
    elif parts:
        print("\n" + "\n\n".join(parts))
    return 1 if failed or block else 0


if __name__ == "__main__":
    # exit 0 = all done, 1 = problems (mailed), 2 = crash (mailed when possible)
    try:
        sys.exit(main())
    except Exception:
        import traceback
        trace = traceback.format_exc()
        print(trace, file=sys.stderr)
        if ("--execute" in sys.argv or "--forecast" in sys.argv) and "--no-mail" not in sys.argv:
            try:
                send_mail("Firefly post-processing crashed", trace)
            except Exception as e:
                print(f"mail failed too: {e}", file=sys.stderr)
        sys.exit(2)
