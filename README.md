# Firefly III tools: rules from tables, post-processing after the import

Scripts around a self-hosted [Firefly III](https://www.firefly-iii.org/) that gets its
bookings from the [Data Importer](https://docs.firefly-iii.org/how-to/data-importer/about/)
(Enable Banking for DKB, PayPal and Wise; CSV for American Express and Miles & More).

The goal: every booking lands on **one account per merchant**, with a **category**, a
**readable description including the place**, the **foreign amount** and the **purchase
date**, without daily manual work. Anything the automation can't decide is tagged and mailed.

| Script | What it does |
|---|---|
| `ff_rules.py` | Writes the counterparty rules ("the bank calls it X → merchant account, category") from a CSV table into one managed rule group; deletes the empty raw accounts those rules leave behind |
| `ff_subscriptions.py` | Creates Firefly subscriptions ("bills") from a CSV table, one link rule each, and links the bookings already there |
| `ff_postprocess.py` | Runs after each import and fills in what rules can't set: description with place, foreign amount, purchase date, the bank's counterparty name; mails problems and a low-balance forecast |
| `import_amex.sh`, `import_mm.sh` | Upload an American Express / Miles & More CSV export to the Data Importer with `import_config_AMEX.json` / `import_config_MM.json`, then post-process |

Standard library only: Python 3.9+ and nothing to install. Every script is a dry run
unless you add `--execute`.

The texts the scripts parse and the account names they expect (`DKB Giro`, `AMEX`, …) are
those of a German DKB / Wise / PayPal / AMEX setup; they are constants at the top of
`ff_postprocess.py`. The two table-driven scripts work with any bank.

## How it runs

```
twice a day (systemd timer on the server)

  1. Data Importer (Enable Banking)   one run per bank                     → new bookings
  2. Firefly rules (on store)         counterparty, category, budget, subscriptions,
                                      tags _postprocess + _new
  3. ff_postprocess.py --execute --jobs /jobs
                                      description + place, foreign amount, purchase date,
                                      bank counterparty name, card refunds, shared IBANs,
                                      empty leftover accounts deleted;
                                      problems → tag _fixme + mail
  4. ff_postprocess.py --forecast     mail if the checking account may go below 500 EUR
                                      within 7 days
```

**Your part:** go through the bookings tagged `_new` and remove the tag once a booking is
checked. On a mail, fix the bookings tagged `_fixme`; the next run removes `_fixme` and
`_postprocess` once it finds them in order.

**Mails:** a problem is mailed in the run where it first shows up; the morning run lists all
open ones again. If a flagged booking is fine as it is, remove `_postprocess`. Removing only
`_fixme` isn't enough, because the next run adds it again.

## Setup

```sh
cp .env.example .env                 # FIREFLY_URL, FIREFLY_TOKEN, optional mail settings
mkdir -p export
cp samples/*.csv export/             # your tables; export/ is git-ignored
git config core.hooksPath .githooks  # leak guard, see "Development"
```

For the CSV imports also set `default_account` in the two `import_config_*.json` files
(see "Importer configs").

In Firefly, create one rule by hand. It fills the work queue of `ff_postprocess.py` and
must run after the managed groups (put it in a later rule group):

| Rule | Triggers (any) | Actions |
|---|---|---|
| Post-process: tag new bookings | type is withdrawal / deposit / transfer | add tag `_postprocess`, add tag `_new` |

Rule group order: your own groups first, then **Managed: Counterparties**, then groups that
build on the clean names (budgets, tags), then **Managed: Subscriptions**. The two managed
groups are created by the scripts.

Then:

```sh
python ff_rules.py sync              # dry run: what would change
python ff_rules.py sync --execute
python ff_subscriptions.py           # dry run
python ff_subscriptions.py --execute
python ff_postprocess.py             # dry run on the bookings tagged _postprocess
```

## Counterparty rules (`export/counterparty_rules.csv`)

One row per merchant. `ff_rules.py sync` turns each row into a rule in the group
*Managed: Counterparties*; rules there that the table no longer wants are deleted, other
groups are never touched. [`samples/counterparty_rules.csv`](samples/counterparty_rules.csv)
has one row per feature:

| account | type | category | patterns | condition | tag | regex |
|---|---|---|---|---|---|---|
| Amazon | expense | Shopping | `starts:amazon \| starts:www amazon \| contains:amzn` | | | |
| Amazon | revenue | Shopping | `contains:amazon \| contains:amzn` | | | |
| Uber | expense | `=(description ~ " " ~ destination_account_name) matches "/eats/i" ? "Eating out" : "Transport"` | `starts:ubr \| starts:uber` | | | |
| Harbor Ferry | expense | `=amount > -35 ? "Drinks" : "Transport"` | `starts:harbor ferry` | | | |
| REI | expense | Shopping | `is:REI \| starts:rei` | | | `^rei(\s*#?\d\|$)` |
| City Parking | expense | Transport | `starts:city of` | `amount_less:20` | | |
| Island Hardware | expense | Household & DIY | `starts:island hardware` | | Holiday home | |
| Payment Fees | expense | Fees & Taxes | `description_starts:ACCRUAL_CHECKOUT` | | | |
| Jane Doe | expense | | `is:Jane Doe` | | | |

- **account**: the clean name the bookings land on. **type**: `expense` matches the
  destination of withdrawals, `revenue` the source of deposits (salary, refunds).
- **patterns**: `is:` / `starts:` / `contains:` on the counterparty name the bank sent,
  case-insensitive, any one matches. `description_starts:` / `description_contains:` look at
  the description instead.
- **category**: plain name, empty for none (private persons), or a Firefly expression
  starting with `=`. In expressions `amount` is negative for withdrawals.
- **condition**: one more trigger that must also hold (`amount_less:20`).
- **tag**: a tag to add. **regex**: the row only applies if the counterparty name matches;
  triggers have no regex, so this is how `starts:rei` leaves "Reichelt" alone.
- `bookings_2y`, `top_category`, `dropped`, `note` are for your own review and ignored.

The Amazon row becomes this rule (non-strict: any trigger fires):

```
Amazon      destination account starts with "amazon"
            destination account starts with "www amazon"
            destination account contains "amzn"
         →  set destination account "Amazon", set category "Shopping"
```

A row gets strict rules (all triggers must hold, one rule per pattern, booking type added)
when it has a condition, a description pattern, or a pattern that also matches one of your
own asset accounts. Four fixed rules come first: keep the bank's counterparty name of DKB
card payments in the notes, take the merchant from Wise descriptions (payments and refunds),
and strip payment processor prefixes (`SQ *`, `TST*`, `Zettle *`, …).

`python ff_rules.py cleanup [--days N]` lists the empty expense/revenue accounts created in
the last N days (default 14) and empty categories; `--execute` deletes them.

## Subscriptions (`export/subscriptions.csv`)

One row per contract. `ff_subscriptions.py` creates or updates the subscription, writes a
link rule into the group *Managed: Subscriptions* and links all matching bookings already in
Firefly. [`samples/subscriptions.csv`](samples/subscriptions.csv):

| name | group | counterparty | match_amount | description_contains | min–max | repeat | active | rule |
|---|---|---|---|---|---|---|---|---|
| Rent | Home | Jane Landlord | 900-1100 | | 1000 | monthly | true | external |
| Internet | Home | Telekom | | `festnetz` | 34–52 | monthly | true | yes |
| Mobile | Home | Telekom\|Telekom Prepaid | | `!festnetz` | 15 | monthly | true | yes |
| Old electricity supplier | Home | Other Power | | | 61–86 | monthly | false | no |
| Audible | Streaming | Audible | | | 9.95 | monthly | true | yes |
| Amazon Prime | Streaming | Amazon | 89.9-89.9 | | 89.9 | yearly | true | yes |
| Gym (currency `USD`) | Other | Fitness Club | 45-59 | | 45–59 | monthly | true | yes |

- **counterparty**: exact expense account name; several separated by `|`.
- **match_amount** `a-b` (inclusive): which bookings of that counterparty belong to the
  subscription. Empty = all of them, so a price change still links. Needed when the
  merchant also sells other things (Amazon Prime among all Amazon purchases).
- **description_contains**: case-insensitive; `!text` = must not contain.
- **amount_min / amount_max / repeat_freq / skip / date**: what Firefly shows and expects
  (`date` = a day the subscription is due). Firefly rejects `amount_min` 0.
- **active** `false` + **rule** `no`: an ended contract. It stays as an inactive
  subscription with its history linked.
- **rule** `external`: no rule and no linking here; a rule of your own does it.
- **exclude**: booking group ids (`|`-separated) that look alike but belong elsewhere.
- **currency**: empty = EUR.

Groups appear in the order of `SUBSCRIPTION_GROUPS` (`.env`), inside a group the most
frequent first, active before ended. Linking is one request per booking: a first run over
years of history takes a while.

## Post-processing (`ff_postprocess.py`)

Works off the tag `_postprocess`. A processed booking loses the tag and is never touched
again, so manual edits in Firefly are safe. A booking with a problem keeps it, gets
`_fixme` and is listed in the mail.

| What | Example |
|---|---|
| Description = merchant + place | `VISA Debitkartenumsatz …` → `Starbucks Hanover`; the bank text goes to the notes as `Original: …` |
| Foreign amount and currency | from the DKB / Wise / AMEX text |
| Foreign PayPal payments (`--jobs`) | Firefly books 125.50 USD as 125.50 EUR (currency of the asset account); the number becomes the foreign amount, the EUR amount comes from the card charge with the same foreign amount. Until that is imported: `_fixme` |
| Purchase date | `date` = day of the card purchase, `book_date` = bank booking date |
| Bank counterparty name (`--jobs`) | the name the bank sent, read from the importer's job files, into the notes; mail if it doesn't fit the account Firefly picked |
| Card refunds | get their merchant, the purchase's category and the tag `refund` |
| Wise | both legs of a currency conversion (same reference) become one transfer; a "Topped up account" deposit is dropped when an own account already shows the transfer (same amount, ±4 days, exactly one match). Waits 4 days for the other bank, then mails. Needs the Wise account's IBAN on its asset account, so the importer turns the paying bank's booking into a transfer |
| Shared IBANs | cleared from any merchant account that picked one up (see design decision 4) |
| Leftovers | empty accounts the importer created and a rule emptied are deleted |
| Warnings | uncategorised bookings, an import that stopped arriving |
| `--forecast` | subscriptions due plus open card statements for the next 7 days; mail below 500 EUR |

Account notes steer it per merchant (on expense/revenue accounts):

- `description: Fitness Club {period}` replaces the bank text of every booking with this
  counterparty (`{period}` = billing period from the text).
- `place: no` keeps the place out of the description.

`python ff_postprocess.py --backfill START END` runs it over a date range (dry run first,
check every line).

## Importer configs (the JSON files)

The Data Importer does every import from a **config JSON**: which Firefly account, what
the columns mean, how duplicates are recognised. It writes one itself: do an import once in
its web UI and download the config on the last page. From then on that file repeats the
import without the UI, in one of three ways:

| Way | How |
|---|---|
| Web UI | upload the CSV together with the JSON on the start page; all questions are pre-answered |
| `POST /autoupload` | what `import_amex.sh` / `import_mm.sh` do: `curl -F json=@config.json -F importable=@file.csv "$IMPORTER_URL/autoupload?secret=…"` with the Firefly token as bearer. The importer needs `CAN_POST_FILES=true` and an `AUTO_IMPORT_SECRET` of at least 16 characters |
| Command line | `docker exec firefly-importer php artisan importer:import /import/import_config_dkb.json` for configs without a file (Enable Banking); the config must lie in the importer's `IMPORT_DIR_ALLOWLIST` |

Two configs are in the repo. **Before the first use set `default_account`** to the id of
your own asset account (the number in the URL of the account's page in Firefly); the
import scripts read the id from there too.

| | `import_config_AMEX.json` | `import_config_MM.json` |
|---|---|---|
| For | the Amex web export, as downloaded | the file `import_mm.sh` converts the Deutsche Bank export into |
| `date` | `d/m/Y` | `Y-m-d` |
| `roles` (one per column) | date, opposing name, amount (negated: Amex lists charges as positive), note, description, 4 × ignore, external id, ignore | date, book date, opposing name, description, amount, foreign amount, foreign currency, external id |
| Duplicates | `duplicate_detection_method: cell` on the external id column (`unique_column_index`): the Amex reference | same, on the id `import_mm.sh` computes from the row |
| `rules: true` | your Firefly rules run on every imported booking | same |

Because duplicates are recognised by the external id, overlapping exports and repeated
runs are safe.

The **Enable Banking** configs (`"flow": "eb"`, one per bank) are made the same way: connect
the bank in the importer's UI, pick the accounts, choose a relative date range
(`"date_range": "partial"`, 14 days here) and download the config. They hold the session and
authorisation ids of your bank connection, so they are git-ignored here and belong in a
secret store. When the bank connection expires, renew it in the UI and replace the ids in the file.

## CSV imports: American Express and Miles & More

Both scripts need `IMPORTER_URL` and `IMPORTER_SECRET` next to the Firefly settings
(`.env`). They upload the file, run `ff_postprocess.py` on the new bookings and list what
arrived on the card account.

**American Express** (CSV download of the card activity, with all optional details included)

```sh
./import_amex.sh AMEX_activity.csv
```

The script checks the header first: it needs the full export with the columns
`Weitere Details` and `Betreff`, like [`samples/amex_activity.csv`](samples/amex_activity.csv).
The short three-column export has no reference, so a later full export would duplicate it.
The foreign amount is in `Weitere Details` ("Foreign Spend Amount: 5.50 US Dollars …");
the config stores that text as the note and post-process takes the amount from there.

**Miles & More** (Deutsche Bank's `Kreditkartentransaktionen_….csv` download)

```sh
./import_mm.sh -n Kreditkartentransaktionen_….csv    # convert and show only
./import_mm.sh Kreditkartentransaktionen_….csv
```

That export has a preamble, a `Saldo:` footer, German numbers and no transaction id (see
[the sample](samples/Kreditkartentransaktionen_1234xxxxxxxx5678_20260201_120000.csv)). The
script converts it to `export/mm/<name>.csv` (ISO dates, dot decimals, foreign amount only
for non-EUR rows, external id = hash of the row) and stops if the rows don't add up to the
`Saldo:` line. Try it on the sample with `-n`.

Other banks: export a CSV, do one import in the UI, download the config, and copy
`import_amex.sh` with the header check adapted.

## Container image

`ghcr.io/hensing/firefly-tools` (linux/amd64, linux/arm64) holds the three Python scripts, the two
import scripts with their configs and `samples/`. The first argument is the script. The
nightly run on the server:

```sh
docker run --rm --network firefly_firefly_iii --env-file /etc/firefly-postprocess.env \
  --user 65534:65534 --read-only --security-opt no-new-privileges=true --memory 128m \
  -v "$jobs":/jobs:ro \
  ghcr.io/hensing/firefly-tools:1.0.0@sha256:… ff_postprocess.py --execute --jobs /jobs
```

`$jobs` is a copy of the importer's `storage/import-jobs`. The other scripts work on your
`export/` folder (tables, card exports, logs), mounted at `/app/export`:

```sh
run="docker run --rm --env-file .env --user $(id -u) -v $PWD/export:/app/export ghcr.io/hensing/firefly-tools"
$run ff_rules.py sync
$run ff_subscriptions.py
$run import_amex.sh export/AMEX_activity.csv
$run import_mm.sh -n samples/Kreditkartentransaktionen_1234xxxxxxxx5678_20260201_120000.csv
```

`--env-file` takes values literally: no quotes around them in a file used that way. To use
your own importer config, mount it over `/app/import_config_AMEX.json`.

## Conventions

**Accounts**
- One expense account per merchant, brand name only: "Starbucks", not "Starbucks Store
  11630 HANOVER" or "Starbucks Boston". The place goes into the description: "Starbucks Hanover".
- Refunds come from a **revenue** account with the merchant's name and the purchase's category,
  tagged `refund` (Firefly rejects deposits from expense accounts).
- Private persons never get a category from rules; they're decided per booking.

**Booking fields**
- `date` = when it happened (card purchase date), `book_date` = bank booking date.
- Notes: `Original: <bank text>` when the description was rewritten, and
  `Bank counterparty: <name as the bank sent it>`.

**Tags**

| Tag | Set by | Meaning |
|---|---|---|
| `_postprocess` | rule (on import) | Work queue for post-processing; removed when done |
| `_new` | rule (on import) | Your review marker; only you remove it |
| `_fixme` | post-process | Something needs a manual fix (also in the mail) |
| `refund` | rule / post-process | Deposit with a spending category |

## Design decisions

1. **Firefly-native first.** Fetching and classification are the Data Importer and Firefly
   rules, so everything stays visible and editable in Firefly. Scripts only do what rules
   can't. Rules can't set the foreign amount, can't link old bookings, have no regex in
   triggers (as of Firefly 6.7) and only see stored fields.
2. **Rules come from tables.** You review a table rather than 250 rules. One OR rule per
   merchant; strict (AND) rules only where needed. Expressions in actions do the rest.
3. **Post-processing after the import** for what rules can't do. It only touches bookings
   tagged `_postprocess`. Classification stays with the rules: post-process never moves a
   booking to another merchant, it mails instead, because category and subscription rules
   wouldn't run again after a move. The one exception is card refunds.
4. **Shared IBANs are the biggest trap.** Firefly matches the counterparty by IBAN before
   name. DKB books every card payment with one clearing IBAN (DE96 1203 0000 9005 2909 04),
   and PayPal and card acquirers share IBANs too. A merchant account holding such an IBAN
   gets every later booking with it. So:
   - Two revenue placeholders, *DKB card clearing* and *DKB card clearing 2*, hold the DKB
     IBAN. Firefly then refuses to store it on a new merchant. This is a workaround, not a
     supported setup: Firefly's startup check (`correction:ibans`) treats two revenue
     holders as an error and clears the second one on every container start, and the API
     refuses to set it. The import wrapper therefore sets it again in SQL before each import.
   - Post-process checks on every run that exactly those two hold it. It clears the other
     shared IBANs (`SHARED_IBANS`) from any merchant account and mails what it did.
   - Card refunds match the placeholder, so post-process gives them their merchant.
5. **The bank's own name is kept.** Firefly drops the counterparty name the bank sent when it
   matches by IBAN. The importer's job files still have it, so the wrapper copies them out
   and post-process writes them into the notes.
6. **Explicit lists over heuristics.** Shared IBANs and category mappings are explicit
   tables. Every bulk change is a dry run first, reviewed line by line.
7. **Twice a day.** PSD2 allows about 4 unattended requests per account and day (counted in
   the bank's time zone); two runs leave room for manual ones.
8. **Subscriptions keep their history.** Ended contracts stay as inactive subscriptions.

## Firefly quirks we hit

- `PUT /transactions/{id}` treats the splits sent as the complete list: send **all** splits,
  plus `group_title` for split groups, or splits get deleted.
- `PUT /rules/{id}` stores every trigger/action without `"active": true` as inactive; the rule
  still looks active but does nothing.
- Renaming an account makes Firefly rewrite `*_account_is` rule triggers to the new name.
- Trigger values lose trailing spaces: `starts:rei ` is stored as `starts:rei` and then also
  hits "Reichelt". Use the `regex` column for such cases.
- Removing a foreign amount needs `"foreign_amount": "0"` (null is ignored). A foreign amount on
  a transfer rewrites the destination side in that currency, so post-process skips transfers.
- An IBAN may be on one expense and one revenue account (`UniqueIban`). A new expense account
  can't store an IBAN that two or more revenue accounts hold, which is what the clearing
  block relies on.
- In expressions, `amount` is negative for withdrawals. An expression sees the booking as it
  was before the rule started.
- Rules run on store (import) only. A manual edit doesn't re-run them.
- Reconciled bookings: an update that sends `reconciled: true` together with amount, currency
  or account fields is refused.
- The importer's job files (`storage/import-jobs`) are lost when the container is recreated.
- The importer exits 1 with "Zero transactions!" when a bank has no booking in the date
  window. For a quiet account that is normal.

## Runbooks

**Change or add a merchant rule**
1. Edit `export/counterparty_rules.csv`. Keep LF line endings.
2. Check each pattern against all account names, then simulate with `GET rules/{id}/test`.
3. `python ff_rules.py sync` (dry run), then `--execute`.
4. Don't fire changed rules over the whole history: they overwrite deliberate manual categories.

**Add a subscription**: add a row to `export/subscriptions.csv`, `python ff_subscriptions.py`
(dry run), then `--execute`.

**Rename or merge accounts**: check the account's **IBAN** first (a shared IBAN must go to a
placeholder, not be deleted with the account). Move bookings with all splits, then delete the
emptied account.

**Change post-processing**: edit `ff_postprocess.py`, test with a dry run (or `--backfill
START END`) plus throwaway bookings on a cash account. A push to `main` builds the image;
the server picks up the new digest.

**Restore the DKB clearing block** (mail "placeholder lost the IBAN"):
```sql
UPDATE accounts SET iban = 'DE96120300009005290904' WHERE name = 'DKB card clearing 2' AND deleted_at IS NULL;
SELECT id, name, iban FROM accounts WHERE iban LIKE 'DE961203%' AND deleted_at IS NULL;
```
Then check the DKB card payments since the break, comparing the account against
`Bank counterparty:` in the notes.

## Development

- **Nothing personal in the repo.** Your tables and bank exports live in `export/`, your
  token in `.env`; both are git-ignored. Account numbers don't belong in code or examples.
- **Pre-commit leak guard**: `git config core.hooksPath .githooks` (needs
  [gitleaks](https://github.com/gitleaks/gitleaks)). It refuses files from `export/`,
  `legacy/` and `.env`, then runs gitleaks with its default rules plus an IBAN rule
  (`.gitleaks.toml`; the shared IBANs above are allow-listed).
- **Build**: `.github/workflows/docker.yml` runs on GitHub Actions on every push to `main`
  that touches the scripts or the Dockerfile: syntax check, a conversion test on the
  Miles & More sample, multi-arch build, push to `ghcr.io/hensing/firefly-tools`, Trivy
  scan. No secrets to set up: it uses the workflow's own `GITHUB_TOKEN`. The build is
  reproducible, so an unchanged rebuild keeps its digest.
- **Versions** (semver, git tags `vX.Y.Z`): a major or minor release is a tag you push
  together with its commit, `git tag v1.1.0 && git push --atomic origin main v1.1.0`.
  Every other build on `main` (a fix, a Dependabot update) is the next patch release: the
  workflow pushes the image as `X.Y.(Z+1)` and tags the commit `vX.Y.(Z+1)`; pull before
  your next commit. Image tags: `X.Y.Z`, `X.Y`, `X`, `latest` (the last three follow the
  highest version) and `sha-<commit>`. Pin `X.Y.Z@sha256:…` and let Renovate or Dependabot
  move it.
- **Updates**: Dependabot raises weekly PRs for the base image and the pinned actions; the
  build runs on them (without pushing). Merged, they become a patch release.
