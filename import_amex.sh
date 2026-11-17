#!/usr/bin/env bash
# Imports an Amex CSV export through the Firefly Data Importer (POST /autoupload) with
# import_config_AMEX.json, then lists what arrived on the AMEX account.
#   ./import_amex.sh                 AMEX_activity.csv
#   ./import_amex.sh export/AMEX_2026.csv
#   (format: samples/amex_activity.csv, the full export with all optional details)
# Duplicates are skipped by the importer (Amex reference = external id), so re-running is safe.
# Afterwards ff_postprocess.py cleans up the new bookings (instead of waiting for the nightly run).
#
# Needs (in .env or the environment): IMPORTER_URL, IMPORTER_SECRET (= AUTO_IMPORT_SECRET of the importer, which also
# needs CAN_POST_FILES=true), FIREFLY_URL, FIREFLY_TOKEN, optional FIREFLY_HEADER_SECRET.
set -euo pipefail
cd "$(dirname "$0")"
csv="${1:-AMEX_activity.csv}"
config=import_config_AMEX.json
py="${PYTHON:-python3}"   # standard library only; PYTHON=/path/to/python in the environment to override

[ -f "$csv" ] || { echo "no such file: $csv"; exit 2; }
# the config maps 11 columns, column 10 "Betreff" = Amex reference = external id. The short
# 3-column export would import without ids, and a later full export would then duplicate it.
# Column 11 "Kategorie" is optional in the Amex export and ignored by the config, so accept both.
header=$(head -1 "$csv" | tr -d '\r\357\273\277')
full="Datum,Beschreibung,Betrag,Weitere Details,Erscheint auf Ihrer Abrechnung als,Adresse,Stadt,PLZ,Land,Betreff"
[ "$header" = "$full" ] || [ "$header" = "$full,Kategorie" ] || {
  echo "unexpected header in $csv (need the full Amex export with 'Weitere Details' / 'Betreff'):"
  echo "   $header"; exit 2; }
[ -f .env ] && { set -a; . ./.env; set +a; }   # in the container the variables come with --env-file
amex_account=$("$py" -c 'import json, sys; print(json.load(open(sys.argv[1]))["default_account"])' "$config")
: "${IMPORTER_URL:?IMPORTER_URL missing in .env}" "${IMPORTER_SECRET:?IMPORTER_SECRET missing in .env}" \
  "${FIREFLY_TOKEN:?FIREFLY_TOKEN missing in .env}"
[ ${#IMPORTER_SECRET} -ge 16 ] || { echo "IMPORTER_SECRET is shorter than 16 characters"; exit 2; }

started=$(date -u +%Y-%m-%dT%H:%M:%S)
mkdir -p export/logs
log="export/logs/import_amex_$(date +%Y%m%d-%H%M%S).log"
echo "== importing $csv ($(($(wc -l < "$csv") - 1)) lines incl. wrapped addresses) -> $log"

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

# what the import created on the AMEX account (the importer's answer has no details)
"$py" - "$started" "$amex_account" <<'EOF'
import sys
from datetime import datetime, timezone
import ff_postprocess as f
started, account = sys.argv[1], sys.argv[2]
# created_at has a timezone offset; compare in UTC
utc = lambda x: datetime.fromisoformat(x).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
new = [(s, c) for t in f.get_all(f"accounts/{account}/transactions?start=2020-01-01&end=2037-12-31")
       for c in [t["attributes"]["created_at"]] if utc(c) >= started
       for s in t["attributes"]["transactions"]]
print(f"{len(new)} new booking(s) on AMEX")
for s, _ in sorted(new, key=lambda x: x[0]["date"]):
    other = s["destination_name"] if s["type"] == "withdrawal" else s["source_name"]
    print(f"   {s['date'][:10]} {s['type'][:3]} {float(s['amount']):9.2f} {s['currency_code']}  "
          f"{other[:32]:32} [{s['category_name'] or '-'}]")
if new:
    print("then: python ff_rules.py cleanup --days 14   (empty raw accounts left by the rules)")
EOF
