#!/usr/bin/env bash
# Imports a Deutsche Bank Miles & More credit card export (Kreditkartentransaktionen_*.csv)
# through the Firefly Data Importer (POST /autoupload) with import_config_MM.json, then lists
# what arrived on the "Miles & More DB" account.
#   ./import_mm.sh Kreditkartentransaktionen_1234xxxxxxxx5678_20260101_120000.csv
#   ./import_mm.sh -n Kreditkartentransaktionen_….csv     convert + show only, no upload
#   (format: samples/Kreditkartentransaktionen_1234xxxxxxxx5678_20260201_120000.csv)
# The DB export has a preamble, a "Saldo:" footer, German numbers and no transaction id, so it is
# first converted to export/mm/<name>.csv (ISO dates, dot decimals, foreign amount only for
# non-EUR, external id "mm-" + hash of the row). Duplicates are skipped by the importer
# (external id), so re-running or overlapping exports are safe.
# Afterwards ff_postprocess.py cleans up the new bookings (instead of waiting for the nightly run).
#
# Needs (in .env or the environment): IMPORTER_URL, IMPORTER_SECRET (= AUTO_IMPORT_SECRET of the importer, which also
# needs CAN_POST_FILES=true), FIREFLY_URL, FIREFLY_TOKEN, optional FIREFLY_HEADER_SECRET.
set -euo pipefail
cd "$(dirname "$0")"
dry=0; [ "${1:-}" = "-n" ] && { dry=1; shift; }
src="${1:?usage: ./import_mm.sh [-n] Kreditkartentransaktionen_….csv}"
config=import_config_MM.json
py="${PYTHON:-python3}"   # standard library only; PYTHON=/path/to/python in the environment to override

[ -f "$src" ] || { echo "no such file: $src"; exit 2; }
mkdir -p export/mm
csv="export/mm/$(basename "$src")"

# convert; fails if the rows don't add up to the "Saldo:" line of the export
"$py" - "$src" "$csv" <<'EOF'
import csv, hashlib, re, sys
from collections import Counter
from decimal import Decimal
src, out = sys.argv[1], sys.argv[2]
try:
    lines = open(src, encoding="utf-8-sig").read().splitlines()
except UnicodeDecodeError:
    lines = open(src, encoding="cp1252").read().splitlines()
num = lambda x: Decimal(x.replace(".", "").replace(",", "."))
iso = lambda d: "{2}-{1:0>2}-{0:0>2}".format(*d.split("."))
rows, seen, total, saldo = [], Counter(), Decimal(0), Decimal(0)
for r in csv.reader(lines, delimiter=";"):
    if r and r[0] == "Saldo:":
        saldo += num(r[6])
    if len(r) != 8 or not re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}", r[0]):
        continue
    beleg, eingang, text, fcur, famount, _rate, amount, cur = (x.strip() for x in r)
    if cur != "EUR":
        sys.exit(f"unexpected booking currency {cur}: {r}")
    key = "|".join([beleg, eingang, text, fcur, famount, amount])
    seen[key] += 1  # identical rows (same day, text, amount) get a running number
    ext = "mm-" + hashlib.sha1(f"{key}|{seen[key]}".encode()).hexdigest()[:20]
    foreign = fcur != "EUR"
    rows.append([iso(beleg), iso(eingang), text.split(", ")[0], text, str(num(amount)),
                 str(num(famount)) if foreign else "", fcur if foreign else "", ext])
    total += num(amount)
if total != saldo:
    sys.exit(f"rows sum to {total}, but the export's Saldo is {saldo}; format changed?")
with open(out, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["date", "booked", "opposing", "description", "amount", "foreign_amount",
                "foreign_currency", "external_id"])
    w.writerows(sorted(rows))
print(f"== {len(rows)} booking(s), sum {total} EUR (= Saldo) -> {out}")
for r in sorted(rows):
    print(f"   {r[0]} {float(r[4]):9.2f} {r[6] and f'({r[5]} {r[6]})':14} {r[3][:60]}")
EOF
[ $dry = 1 ] && exit 0

[ -f .env ] && { set -a; . ./.env; set +a; }   # in the container the variables come with --env-file
mm_account=$("$py" -c 'import json, sys; print(json.load(open(sys.argv[1]))["default_account"])' "$config")
: "${IMPORTER_URL:?IMPORTER_URL missing in .env}" "${IMPORTER_SECRET:?IMPORTER_SECRET missing in .env}" \
  "${FIREFLY_TOKEN:?FIREFLY_TOKEN missing in .env}"
[ ${#IMPORTER_SECRET} -ge 16 ] || { echo "IMPORTER_SECRET is shorter than 16 characters"; exit 2; }

started=$(date -u +%Y-%m-%dT%H:%M:%S)
mkdir -p export/logs
log="export/logs/import_mm_$(date +%Y%m%d-%H%M%S).log"
echo "== importing $csv -> $log"

# URL (with the secret) and headers go to curl on stdin, so they don't show up in `ps`
code=$({
  printf 'url = "%s/autoupload?secret=%s"\n' "${IMPORTER_URL%/}" \
    "$("$py" -c 'import os, urllib.parse; print(urllib.parse.quote(os.environ["IMPORTER_SECRET"], safe=""))')"
  printf 'header = "Authorization: Bearer %s"\n' "$FIREFLY_TOKEN"
  [ -n "${FIREFLY_HEADER_SECRET:-}" ] && printf 'header = "X-Firefly-Gate: %s"\n' "$FIREFLY_HEADER_SECRET"
  true
} | curl --silent --show-error --max-time 900 --config - --request POST \
    --header 'Accept: application/json' \
    --form "json=@${config}" --form "importable=@${csv}" \
    --output "$log" --write-out '%{http_code}') || code="curl-error"

if [ "$code" != "200" ]; then
  echo "FAILED (HTTP $code):"; [ -f "$log" ] && head -c 2000 "$log"; echo
  exit 1
fi

# clean up the new bookings now instead of in the nightly run (foreign amount,
# purchase date, description); no mail locally, problems keep the _postprocess tag
"$py" ff_postprocess.py --execute --no-mail \
  || echo "post-processing reported problems (see above); those bookings keep the _postprocess tag"

# what the import created on the M&M account (the importer's answer has no details)
"$py" - "$started" "$mm_account" <<'EOF'
import sys
from datetime import datetime, timezone
import ff_postprocess as f
started, account = sys.argv[1], sys.argv[2]
# created_at has a timezone offset; compare in UTC
utc = lambda x: datetime.fromisoformat(x).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
new = [(s, c) for t in f.get_all(f"accounts/{account}/transactions?start=2020-01-01&end=2037-12-31")
       for c in [t["attributes"]["created_at"]] if utc(c) >= started
       for s in t["attributes"]["transactions"]]
print(f"{len(new)} new booking(s) on Miles & More DB")
for s, _ in sorted(new, key=lambda x: x[0]["date"]):
    other = s["destination_name"] if s["type"] == "withdrawal" else s["source_name"]
    fx = f" ({float(s['foreign_amount']):.2f} {s['foreign_currency_code']})" if s["foreign_amount"] else ""
    print(f"   {s['date'][:10]} {s['type'][:3]} {float(s['amount']):9.2f} {s['currency_code']}{fx}  "
          f"{other[:32]:32} [{s['category_name'] or '-'}]")
if new:
    print("then: python ff_rules.py cleanup --days 14   (empty raw accounts left by the rules)")
EOF
