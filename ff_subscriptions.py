#!/usr/bin/env python3
"""
Subscriptions (Firefly "bills") from a table (default: export/subscriptions.csv,
see samples/subscriptions.csv).

    python3 ff_subscriptions.py            # dry run: what would change
    python3 ff_subscriptions.py --execute
    python3 ff_subscriptions.py --table samples/subscriptions.csv

Per CSV row (sorted by group, then monthly -> yearly, active first, name):
  - the subscription itself (created or updated by name),
  - rule = yes: a rule "Subscription: <name>" in rule group "Managed: Subscriptions" (runs
    last, after the counterparty rules have set the clean names) that links new
    bookings; no: no rule (ended subscriptions); external: no rule and no linking
    here (a rule of your own elsewhere links them),
  - links all existing bookings that match (withdrawal to that counterparty,
    amount within match_amount, description containing description_contains).

Matching: counterparty = exact expense account name, several separated by "|"
(the rule uses the first one); match_amount "a-b"
(inclusive, empty = any); description_contains case-insensitive (empty = any,
"!text" = must not contain); exclude = booking ids (group ids, "|"-separated)
that look alike but belong elsewhere. currency = the subscription's currency (empty = EUR;
e.g. USD for bookings on a USD account).
Groups appear on the subscriptions page in the order of SUBSCRIPTION_GROUPS
(comma-separated, environment or .env), else in the order the table first names them.
Rules in group "Managed: Subscriptions" that the CSV no longer wants are deleted;
subscriptions not in the CSV are left alone.
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ff_postprocess import api, get_all, load_dotenv  # noqa: E402

CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "export", "subscriptions.csv")
RULE_GROUP = "Managed: Subscriptions"
RULE_GROUP_NOTE = ("Generated from the subscriptions table by 'ff_subscriptions.py'. "
                   "Changes made here are overwritten and extra rules deleted: edit the CSV instead.")
RULE_PREFIX = "Subscription: "
GROUPS = []   # order of the groups on the subscriptions page (filled in main)
FREQ = ["weekly", "monthly", "quarterly", "half-year", "yearly"]


def amount_range(row):
    if not row["match_amount"]:
        return None
    lo, hi = row["match_amount"].split("-")
    return float(lo), float(hi)


def matches(row, s):
    if s["type"] != "withdrawal":
        return False
    rng = amount_range(row)
    if rng and not rng[0] <= float(s["amount"]) <= rng[1]:
        return False
    want = row["description_contains"].lower()
    if not want:
        return True
    if want.startswith("!"):
        return want[1:] not in (s["description"] or "").lower()
    return want in (s["description"] or "").lower()


def bill_body(row, order):
    return {"name": row["name"], "object_group_title": row["group"], "order": order, "amount_min": row["amount_min"], "amount_max": row["amount_max"],
            "date": row["date"], "repeat_freq": row["repeat_freq"], "skip": int(row["skip"] or 0),
            "currency_code": row["currency"] or "EUR", "active": row["active"] == "true",
            "notes": row["notes"] or None}


def rule_body(row, group_id):
    triggers = [("transaction_type", "withdrawal", False),
                ("destination_account_is", row["counterparty"].split("|")[0], False)]
    rng = amount_range(row)
    if rng:  # amount_more / amount_less are strict
        triggers += [("amount_more", f"{rng[0] - 0.01:.2f}", False),
                     ("amount_less", f"{rng[1] + 0.01:.2f}", False)]
    want = row["description_contains"]
    if want:  # "!text" = must not contain
        triggers.append(("description_contains", want.lstrip("!"), want.startswith("!")))
    return {"title": RULE_PREFIX + row["name"], "rule_group_id": group_id,
            "trigger": "store-journal", "strict": True, "active": True, "stop_processing": False,
            "triggers": [{"type": t, "value": v, "prohibited": no, "active": True,
                          "stop_processing": False} for t, v, no in triggers],
            "actions": [{"type": "link_to_bill", "value": row["name"], "active": True,
                         "stop_processing": False}]}


def same_rule(existing, wanted):
    a = existing["attributes"]
    key = lambda items: [(i["type"], str(i["value"]), bool(i.get("prohibited"))) for i in items]
    return (key(a["triggers"]) == key(wanted["triggers"]) and key(a["actions"]) == key(wanted["actions"])
            and a["active"] and all(i["active"] for i in a["triggers"] + a["actions"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--execute", action="store_true", help="write to Firefly (else dry run)")
    ap.add_argument("--table", default=CSV, help=f"default: {CSV}")
    args = ap.parse_args()
    load_dotenv()
    if not os.path.exists(args.table):
        sys.exit(f"no such table: {args.table} (start from samples/subscriptions.csv)")
    rows = list(csv.DictReader(open(args.table, encoding="utf-8")))
    GROUPS[:] = ([g.strip() for g in os.environ.get("SUBSCRIPTION_GROUPS", "").split(",") if g.strip()]
                 or list(dict.fromkeys(row["group"] for row in rows)))
    mode = "EXECUTE" if args.execute else "DRY RUN"

    # subscriptions
    unknown = {row["group"] for row in rows} - set(GROUPS)
    if unknown:
        sys.exit(f"unknown group(s) {unknown}: add them to SUBSCRIPTION_GROUPS")
    rows.sort(key=lambda r: (GROUPS.index(r["group"]), FREQ.index(r["repeat_freq"]),
                             r["active"] != "true", r["name"].lower()))
    bills = {b["attributes"]["name"]: b for b in get_all("bills")}
    for order, row in enumerate(rows, 1):
        body, b = bill_body(row, order), bills.get(row["name"])
        if not b:
            print(f"{mode}: create subscription {row['name']}")
            if args.execute:
                bills[row["name"]] = api("POST", "bills", body)["data"]
            continue
        a = b["attributes"]
        diff = {k: v for k, v in body.items() if k not in ("currency_code", "object_group_title") and (
            float(a[k]) != float(v) if k in ("amount_min", "amount_max")
            else (a[k] or "")[:10] != v if k == "date" else a[k] != v)}
        if a["object_group_title"] != row["group"]:
            diff["object_group_title"] = row["group"]
        if diff:
            print(f"{mode}: update subscription {row['name']}: {diff}")
            if args.execute:
                api("PUT", f"bills/{b['id']}", body)

    # group order
    for g in get_all("object-groups"):
        title = g["attributes"]["title"]
        if title in GROUPS and g["attributes"]["order"] != GROUPS.index(title) + 1:
            print(f"{mode}: group {title} -> position {GROUPS.index(title) + 1}")
            if args.execute:
                api("PUT", f"object-groups/{g['id']}", {"title": title, "order": GROUPS.index(title) + 1})

    # rules
    groups = {g["attributes"]["title"]: g for g in get_all("rule-groups")}
    group = groups.get(RULE_GROUP)
    if not group:
        print(f"{mode}: create rule group {RULE_GROUP}")
        if args.execute:
            order = max([g["attributes"].get("order") or 0 for g in groups.values()] + [0]) + 1
            group = api("POST", "rule-groups", {"title": RULE_GROUP, "order": order,
                                                      "description": RULE_GROUP_NOTE})["data"]
    existing = {r["attributes"]["title"]: r for r in get_all(f"rule-groups/{group['id']}/rules")} if group else {}
    for row in rows:
        if row["rule"] != "yes":
            continue
        wanted = rule_body(row, group["id"] if group else None)
        r = existing.get(wanted["title"])
        if r and same_rule(r, wanted):
            continue
        print(f"{mode}: {'update' if r else 'create'} rule {wanted['title']}")
        if args.execute:
            if r:
                api("PUT", f"rules/{r['id']}", wanted)
            else:
                api("POST", "rules", wanted)

    wanted_titles = {RULE_PREFIX + row["name"] for row in rows if row["rule"] == "yes"}
    for title, r in existing.items():
        if title not in wanted_titles:   # own group only: rules of dropped/ended rows
            print(f"{mode}: delete rule {title}")
            if args.execute:
                api("DELETE", f"rules/{r['id']}")

    # link existing bookings
    accounts = {}
    for a in get_all("accounts?type=expense"):
        accounts.setdefault(a["attributes"]["name"], a["id"])
    total = 0
    for row in rows:
        if row["rule"] == "external":
            continue                     # linked elsewhere (rent: its own rule)
        accs = []
        for name in row["counterparty"].split("|"):
            if name not in accounts:
                print(f"  !! no expense account named {name!r} ({row['name']})")
            else:
                accs.append(accounts[name])
        bill = bills.get(row["name"])
        bill_id = bill["id"] if bill else "(new)"   # dry run: not created yet
        todo, groups = [], {}
        for acc in accs:
            groups.update((g["id"], g) for g in get_all(f"accounts/{acc}/transactions?type=withdrawal"))
        exclude = set(row["exclude"].split("|")) - {""}
        for g in groups.values():
            if g["id"] in exclude:
                continue
            splits = g["attributes"]["transactions"]
            hit = [s for s in splits if s["destination_id"] in accs and matches(row, s)
                   and (s["bill_id"] or None) != bill_id]
            if hit:
                todo.append((g, splits, {s["transaction_journal_id"] for s in hit}))
        if not todo:
            continue
        dates = sorted(s["date"][:10] for _, sp, ids in todo for s in sp if s["transaction_journal_id"] in ids)
        print(f"{mode}: link {len(dates)} booking(s) to {row['name']} ({dates[0]} .. {dates[-1]})")
        total += len(dates)
        if not args.execute:
            continue
        for g, splits, ids in todo:
            body = {"transactions": [{"transaction_journal_id": s["transaction_journal_id"],
                                      **({"bill_id": bill_id} if s["transaction_journal_id"] in ids else {})}
                                     for s in splits]}
            if len(splits) > 1:
                body["group_title"] = g["attributes"]["group_title"]
            api("PUT", f"transactions/{g['id']}", body)
    print(f"{mode}: {len(rows)} subscriptions, {total} booking(s) to link")


if __name__ == "__main__":
    main()
