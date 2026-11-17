#!/usr/bin/env python3
"""
Counterparty rules for Firefly III from a table, and cleanup of what they leave behind.

    python ff_rules.py sync [--execute] [--table FILE]      # table -> rules
    python ff_rules.py cleanup [--days N] [--execute]       # empty raw accounts/categories

sync writes one rule per row of the table (default: export/counterparty_rules.csv,
see samples/counterparty_rules.csv) into the rule group "Managed: Counterparties":
"the bank calls it X" -> set the merchant account, the category and optionally a
tag. Rules in that group that the table no longer wants are deleted; rules in
other groups are never touched. Dry run without --execute.

Table columns:
  account     the clean account name the bookings should land on ("Starbucks")
  type        expense (withdrawals, matches the destination) or revenue (deposits, source)
  category    category to set; empty = none; "=..." = a Firefly expression
  patterns    "is:X | starts:x | contains:y" on the bank's counterparty name, or
              "description_contains:x" etc. on the description; any one matches
  condition   optional extra trigger that must also hold, e.g. "amount_less:35"
  tag         optional tag to add
  regex       optional: account and category are only set if the counterparty name
              matches this regex (triggers have none; "starts:rei" also hits Reichelt)
  bookings_2y, top_category, dropped, note: for your own review, ignored here

cleanup: the rules move a booking from the raw account the importer created
("Starbucks Store 11630 HANOVER") to the clean one and leave the raw account empty.
It lists (and with --execute deletes) empty expense/revenue accounts created in the
last --days days, plus empty categories.

Needs FIREFLY_URL and FIREFLY_TOKEN (environment or .env). Standard library only.
"""
import argparse
import csv as _csv
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ff_postprocess import api, get_all, load_dotenv  # noqa: E402

RULES_TABLE = Path(__file__).parent / "export" / "counterparty_rules.csv"

RULE_GROUP_COUNTERPARTIES = "Managed: Counterparties"
RULE_GROUP_COUNTERPARTIES_NOTE = ("Generated from the counterparty table by 'ff_rules.py sync'. "
                                  "Changes made here are overwritten and extra rules deleted: edit the CSV instead.")
WISE_RULE = {
    "title": "Wise: counterparty from the description",
    "triggers": [{"type": "transaction_type", "value": "withdrawal"},
                 {"type": "destination_account_is", "value": "(unknown destination)"},
                 {"type": "description_contains", "value": "issued by"}],
    "actions": [{"type": "set_destination_account",
                 "value": '=substr(description, strpos(description, "issued by ") + 10)'}],
}
# Same for Wise card refunds ("Card transaction of -12.79 USD issued by ..."): deposits
# from "(unknown source)"
WISE_REFUND_RULE = {
    "title": "Wise: refund counterparty from the description",
    "triggers": [{"type": "transaction_type", "value": "deposit"},
                 {"type": "source_account_is", "value": "(unknown source)"},
                 {"type": "description_contains", "value": "issued by"}],
    "actions": [{"type": "set_source_account",
                 "value": '=substr(description, strpos(description, "issued by ") + 10)'}],
}


# Card terminals / payment processors in front of the merchant name ("Sq *Handlebar
# Cafe Portland", "Tst* Tatte Bakery"): strip everything up to the "*" before the
# counterparty rules look at the name. Not PayPal: its charges are transfers.
PROCESSOR_PREFIXES = ["sq *", "sq*", "tst *", "tst*", "zettle *", "zettle*", "fh *", "fh*",
                      "dd *", "dd*", "fs *", "fs*"]
PREFIX_RULE = {
    "title": "Strip payment processor prefix (Square, Toast, Zettle, FareHarbor, DoorDash, FastSpring)",
    "triggers": [{"type": "destination_account_starts", "value": p} for p in PROCESSOR_PREFIXES],
    "actions": [{"type": "set_destination_account",
                 # no trim() in Firefly's expressions: skip one space after the "*" by hand
                 "value": '=substr(destination_account_name, strpos(destination_account_name, "*") + 1, 1) == " "'
                          ' ? substr(destination_account_name, strpos(destination_account_name, "*") + 2)'
                          ' : substr(destination_account_name, strpos(destination_account_name, "*") + 1)'}],
}


# DKB card payments carry the merchant and place only in the bank's counterparty
# name ("SQ..HANDLEBAR.CAFE/Portland"), which the rules below replace with the
# clean merchant name. Keep it in the notes first: ff_postprocess takes the place from it.
KEEP_DKB_COUNTERPARTY_RULE = {
    "title": "Keep the bank counterparty of DKB card payments (place for ff_postprocess)",
    "triggers": [{"type": "transaction_type", "value": "withdrawal"},
                 {"type": "description_starts", "value": "VISA Debitkartenumsatz"}],
    "actions": [{"type": "set_notes", "value": '="Bank counterparty: " ~ destination_account_name'}],  # notes are empty on import
}


def rules_wanted(own_accounts=()):
    """The counterparty table -> rule bodies. One OR rule per counterparty
    (all patterns as triggers). Strict per-pattern rules only where needed: an
    amount condition (AND), or a pattern that also matches an own account
    (transfers have an own account on that side)."""
    wanted = [{**KEEP_DKB_COUNTERPARTY_RULE, "strict": True}, {**WISE_RULE, "strict": True},
              {**WISE_REFUND_RULE, "strict": True},
              {**PREFIX_RULE, "strict": False}]
    matches = lambda kind, kw, name: (name.casefold() == kw.casefold() if kind == "is" else
                                      name.casefold().startswith(kw.casefold()) if kind == "starts" else
                                      kw.casefold() in name.casefold())
    for r in _csv.DictReader(RULES_TABLE.open(encoding="utf-8")):
        if not r["patterns"].strip():
            continue
        side = "destination" if r["type"] == "expense" else "source"
        cond = r.get("condition", "").strip()
        pats = [tuple(p.partition(":")[::2]) for p in r["patterns"].split(" | ")]
        covered = lambda k, w: any((k2, w2) != (k, w) and (
            (k2 == "contains" and w2.casefold() in w.casefold() and k in ("starts", "contains", "is")) or
            (k2 == "starts" and w.casefold().startswith(w2.casefold()) and k in ("starts", "is")))
            for k2, w2 in pats)
        pats = [(k, w) for k, w in dict.fromkeys(pats) if not covered(k, w)]
        account, category = r["account"], r["category"]
        rx = (r.get("regex") or "").strip()
        if rx:  # triggers have no regex, actions do: what the trigger also hits keeps its values
            hit = f'{side}_account_name matches "/{rx.replace(chr(92), chr(92) * 2)}/i"'
            account = f'={hit} ? "{account}" : {side}_account_name'
            category = category and f'={hit} ? "{category}" : category_name'
        actions = [{"type": f"set_{side}_account", "value": account}]
        if category:
            actions.append({"type": "set_category", "value": category})
        if r.get("tag", "").strip():
            actions.append({"type": "add_tag", "value": r["tag"].strip()})
        name = f"{r['account']}{' (income)' if r['type'] == 'revenue' else ''}"
        # "description_starts:…" etc. go straight to Firefly, the rest is on the counterparty name
        trig = lambda k, w: {"type": k if k.startswith("description_") else f"{side}_account_{k}", "value": w}
        # description patterns also hit the other booking type (tax refund vs. tax payment),
        # where set_{side}_account would rewrite the own account: strict with the booking type
        risky = [(k, w) for k, w in pats if k.startswith("description_")
                 or any(matches(k, w, a) for a in own_accounts)]
        safe = [(k, w) for k, w in pats if (k, w) not in risky]
        typ = {"type": "transaction_type", "value": "withdrawal" if r["type"] == "expense" else "deposit"}
        if cond:  # AND needed: one strict rule per pattern
            ck, _, cv = cond.partition(":")
            for k, w in pats:
                wanted.append({"title": f"{name} <- {k}:{w} [{cond}]"[:255], "strict": True,
                               "triggers": [typ, trig(k, w), {"type": ck, "value": cv}], "actions": actions})
            continue
        if safe:
            wanted.append({"title": f"{name}"[:255], "strict": False,
                           "triggers": [trig(k, w) for k, w in safe], "actions": actions})
        for k, w in risky:  # would also match an own account: strict with the booking type
            wanted.append({"title": f"{name} <- {k}:{w}"[:255], "strict": True,
                           "triggers": [typ, trig(k, w)], "actions": actions})
    return wanted


def rules_sync(execute):
    own = [a["attributes"]["name"] for a in get_all("accounts?type=asset")]
    wanted = rules_wanted(own)
    groups = {g["attributes"]["title"]: g for g in get_all("rule-groups")}
    group = groups.get(RULE_GROUP_COUNTERPARTIES)
    existing = {r["attributes"]["title"]: r for r in get_all("rules")
                if group and str(r["attributes"]["rule_group_id"]) == str(group["id"])}
    # a PUT without "active" on triggers/actions stores them as inactive: compare it too
    norm = lambda items: [(x["type"], str(x["value"]), x.get("active", True)) for x in items]
    create = [w for w in wanted if w["title"] not in existing]
    update = [w for w in wanted if w["title"] in existing and (
        existing[w["title"]]["attributes"]["strict"] != w["strict"] or
        norm(existing[w["title"]]["attributes"]["triggers"]) != norm(w["triggers"]) or
        norm(existing[w["title"]]["attributes"]["actions"]) != norm(w["actions"]))]
    delete = [t for t in existing if t not in {w["title"] for w in wanted}]
    print(("EXECUTE" if execute else "DRY RUN") + f": group '{RULE_GROUP_COUNTERPARTIES}': "
          f"{len(create)} create, {len(update)} update, {len(delete)} delete, "
          f"{len(wanted) - len(create) - len(update)} unchanged")
    for w in wanted[:6]:
        print(f"   {w['title'][:60]:60} | {' & '.join(t['type'] + '=' + t['value'] for t in w['triggers'] if t['type'] != 'transaction_type')[:60]}"
              f" -> {', '.join(a['value'] for a in w['actions'])[:60]}")
    if not execute:
        return
    if not group:
        order = max([g["attributes"].get("order") or 0 for g in groups.values()] + [0]) + 1
        group = api("POST", "rule-groups", {"title": RULE_GROUP_COUNTERPARTIES, "order": order,
                                            "description": RULE_GROUP_COUNTERPARTIES_NOTE})["data"]
    # deletes first: titles are unique ignoring case ("HANDLEBAR CAFE" -> "Handlebar Cafe")
    for t in delete:
        api("DELETE", f"rules/{existing[t]['id']}")
    for n, w in enumerate(wanted, 1):
        body = {**w, "rule_group_id": group["id"], "trigger": "store-journal", "active": True,
                "stop_processing": False, "order": n,
                "triggers": [{**t, "active": True} for t in w["triggers"]],
                "actions": [{**a, "active": True} for a in w["actions"]]}
        if w in create:
            api("POST", "rules", body)
        elif w in update:
            api("PUT", f"rules/{existing[w['title']]['id']}", body)
    print("done")


def cmd_cleanup(args):
    """After an import: rules move bookings from the raw account (e.g. "Wholefoods Slm 10801
    SALEM") to the canonical one and leave the raw account empty. Lists and deletes empty
    expense/revenue accounts created in the last --days days, plus empty categories."""
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=args.days)).isoformat() if args.days else "0000"
    keep = {"(no name)", "(unknown destination)",  # importer placeholders, the Wise rule matches the latter
            # hold the DKB card clearing IBAN so Firefly never stores it on a merchant
            "DKB card clearing", "DKB card clearing 2"}
    from concurrent.futures import ThreadPoolExecutor
    has_bookings = lambda path: bool(api("GET", f"{path}?start=1980-01-01&end=2037-12-31&limit=1")["data"])
    # the account list carries last_activity (empty = no bookings, checked 2026-10-09 on all
    # 1,800 accounts), so only those few get the exact check, not one request per account
    candidates = [(f"accounts/{a['id']}", f"{kind} account", a["attributes"]["name"])
                  for kind in ("expense", "revenue") for a in get_all(f"accounts?type={kind}")
                  if a["attributes"]["created_at"][:10] >= since and not a["attributes"]["last_activity"]
                  and a["attributes"]["name"] not in keep]
    candidates += [(f"categories/{c['id']}", "category", c["attributes"]["name"]) for c in get_all("categories")]
    with ThreadPoolExecutor(8) as ex:
        used = list(ex.map(lambda c: has_bookings(f"{c[0]}/transactions"), candidates))
    empty = [c for c, u in zip(candidates, used) if not u]
    print(("EXECUTE" if args.execute else "DRY RUN") + f": {len(empty)} empty "
          f"({f'accounts created since {since}' if args.days else 'accounts of any age'})")
    for path, what, name in empty:
        print(f"   {what:16} {name}")
        if args.execute:
            api("DELETE", path)
    print("done")


def main():
    global RULES_TABLE
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sy = sub.add_parser("sync", help="write the rules of the table to Firefly")
    sy.add_argument("--table", type=Path, default=RULES_TABLE, help=f"default: {RULES_TABLE}")
    sy.add_argument("--execute", action="store_true", help="write to Firefly (default: dry run)")
    cl = sub.add_parser("cleanup", help="delete empty raw accounts and categories")
    cl.add_argument("--days", type=int, default=14, help="only accounts created in the last N days (0 = any age)")
    cl.add_argument("--execute", action="store_true", help="write to Firefly (default: dry run)")
    args = p.parse_args()
    load_dotenv()
    missing = [v for v in ("FIREFLY_URL", "FIREFLY_TOKEN") if not os.environ.get(v)]
    if missing:
        sys.exit(f"set {', '.join(missing)}")
    if args.cmd == "sync":
        RULES_TABLE = args.table
        if not RULES_TABLE.exists():
            sys.exit(f"no such table: {RULES_TABLE} (start from samples/counterparty_rules.csv)")
        rules_sync(args.execute)
    else:
        cmd_cleanup(args)


if __name__ == "__main__":
    main()
