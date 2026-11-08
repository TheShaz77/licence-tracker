# Licence tracker

A daily job that keeps a master list of licensed **payment institutions, e-money
institutions and banks** in a Google Sheet. Every day it compares each regulator's
register with the previous day and logs:

| Event | Meaning |
|---|---|
| New licence | A firm appeared, or got authorised/registered |
| Licence cancelled / revoked | The register now shows the firm as cancelled, revoked or withdrawn |
| Removed from register | The firm disappeared from a list-style register (usually means withdrawal) |
| Licence type changed | e.g. Small EMI → Authorised EMI, SPI → API, PI → EMI |
| Licence reinstated | An inactive firm became active again |
| Name changed | Registered name changed |
| Permissions changed | EU firms only: the authorised payment services changed |

It runs free on **GitHub Actions**, writes to your **Google Sheet**, and alerts you
by email (through a GitHub issue) whenever something changes.

---

## What it covers today

| Source | Jurisdictions | Licence types | How changes are detected | Refresh |
|---|---|---|---|---|
| **FCA Register API** | UK | EMIs, small EMIs, APIs, SPIs, e-money agents, payment (PSD) agents, cryptoasset firms (MLR registered) | Each firm's status re-checked daily; new FRNs scanned daily | Live |
| **EBA PSD2 register** | All EU/EEA countries (about 30) | PIs, EMIs, AISPs, small/exempt PIs and EMIs | Additions, removals, withdrawal dates, type and service changes | Daily |
| **ESMA MiCA register** | EU/EEA | MiCA CASPs (authorised), financial firms notified for crypto services (Art. 60), e-money token issuers | Additions, removals, end dates | Daily |
| **FINMA** | Switzerland | FinTech licence holders | Additions and removals | Daily |
| **FINMA SRO member search** | Switzerland | Which AML self-regulatory organisation a Swiss firm belongs to (one firm at a time) | Re-checked every 60 days | Daily |
| **FINTRAC, Bank of Canada** | Canada | MSBs; PSPs registered / applied under the RPAA | Additions, removals, status changes | Daily |
| **FinCEN + 8 state lists** | US | MSBs; state money transmitter licences | Additions, removals, status changes | Daily / weekly |
| **SARB, Prudential Authority, FSCA** | South Africa | Third-party payment providers, payment system operators, PCH system operators, banks, crypto asset service providers | Additions and removals | Daily |

Banks (credit institutions) are not tracked, except in South Africa and where a
bank appears on the MiCA register for its crypto services.

Sanctioned countries are excluded through `excluded_countries` in `config.yaml`
(default: RU, BY, IR, KP, CU, SY). None of the current sources license firms in
those countries; the filter matters as you add more jurisdictions.

**There is no single global register.** Each further jurisdiction is a new adapter
in `regtracker/sources/`. Good next candidates, all with downloadable public lists:
Singapore (MAS Financial Institutions Directory), Hong Kong (HKMA stored-value
facility and bank registers), UAE (CBUAE register), Australia (APRA ADI list),
Canada (Bank of Canada RPAA registry), the US (FDIC BankFind API for banks; money
transmitters sit with each state via NMLS and have no bulk feed), Gibraltar, Jersey
and the Isle of Man.

---

## Setup (about 30 minutes, once)

### 1. Get a free FCA API key
1. Go to <https://register.fca.org.uk/Developer/s/> → **Register to be a New User**.
2. Confirm your email, sign in, and copy your **API key** from your profile.
   You will need the email address you signed up with and the key.

### 2. Put the code on GitHub
1. Create a GitHub account if you don't have one, then **New repository**
   (e.g. `licence-tracker`).
2. **Public or private?**
   - **Public**: GitHub Actions minutes are free and unlimited. The repo will
     contain only public register data; your keys stay hidden in Secrets.
   - **Private**: you get 2,000 free minutes a month. Daily runs use roughly
     1,000–1,300 a month (mostly the FCA re-checks), but the one-off FCA seed
     scan (step 6) needs about 4,000 on top, so it would run into paid minutes.
     The simplest route is to keep the repo public for the first week and switch
     it to private once the seed is complete.
3. Upload the contents of this folder (**Add file → Upload files**, drag
   everything in). Check that `.github/workflows/daily.yml` and
   `.github/workflows/fca-seed.yml` made it; some uploaders skip folders that
   start with a dot. If they're missing, create them with **Add file → Create new
   file**, type the path `.github/workflows/daily.yml`, and paste the contents.

### 3. Create the Google Sheet and a robot account that can edit it
1. Create a blank Google Sheet. Copy its **ID** from the URL:
   `https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`
2. Go to <https://console.cloud.google.com/> → create a project (any name).
3. **APIs & Services → Library** → search **Google Sheets API** → **Enable**.
4. **IAM & Admin → Service accounts → Create service account** (any name) →
   **Done**.
5. Open the service account → **Keys → Add key → Create new key → JSON**. A file
   downloads; keep it private.
6. Copy the service account's email (looks like
   `name@project.iam.gserviceaccount.com`) and **share your Google Sheet with it
   as Editor**.

   If this is for work, check with IT whether you should create the Google
   Cloud project under the company's Google account instead of a personal one.

### 4. Add the secrets to GitHub
Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value |
|---|---|
| `FCA_API_EMAIL` | the email you registered with the FCA |
| `FCA_API_KEY` | your FCA API key |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | the **entire contents** of the JSON key file |
| `SHEET_ID` | the sheet ID from step 3.1 |

**Change alerts need no setup.** Each day with changes, the daily run posts them as
an issue in this repo (Issues tab), and GitHub emails you about it at your GitHub
account's address. Check that **Settings → Notifications → Watching → Email** is
ticked on github.com (it is by default). To switch these alerts off, add a
repository *variable*, not a secret, called `ALERTS_VIA_GITHUB` set to `false`, and add
`ALERTS_VIA_GITHUB: ${{ vars.ALERTS_VIA_GITHUB }}` to the `env:` block in `daily.yml`.

Optional extra: a custom email via SMTP (needs a provider that gives you an SMTP
password, e.g. Gmail app passwords or Brevo):

| Name | Value |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | your Gmail address |
| `SMTP_PASSWORD` | the 16-character app password |
| `EMAIL_TO` | where to send it (comma-separate several addresses) |
| `EMAIL_FROM` | optional sender address (required for Brevo) |

### 5. Turn it on
1. Repo → **Actions** tab → enable workflows if GitHub asks.
2. Open **Daily licence check** → **Run workflow**. The first run takes about
   10 minutes and fills the sheet with the UK banks, EU/EEA payment firms and
   banks, and Swiss banks. The first run of each source is a **baseline** and
   raises no alerts; change alerts start from the second day.

### 6. Start the one-off FCA scan
The FCA's free API can only look firms up one at a time, so the UK payment-firm
list has to be built once by checking every firm reference number (about 1.1
million lookups at the FCA's rate limit).

1. **Actions → FCA seed scan (one-off) → Run workflow**.
2. It runs in 5½-hour chunks every 6 hours and picks up where it left off, for
   **about 3 days** in total. The most useful FRN ranges (EMIs, then PIs) are
   scanned first.
3. Check progress any time in the job log ("FCA seed progress: 42% scanned; 1,830
   payment/e-money firms found").
4. When it says **complete**, the next daily run adds the UK payment firms to
   the sheet. Then disable the seed workflow (**Actions → FCA seed scan → ⋯ →
   Disable workflow**).

That's it. The check runs every 2 hours, at 17 minutes past (00:17, 02:17 … UTC).

---

## The Google Sheet

| Tab | What's on it |
|---|---|
| **Licences** | Every tracked firm: jurisdiction, regulator, name, licence type, Active/Inactive, reference number, LEI, city, website (UK), services, passporting, register link, first seen, last changed. Filters are switched on; filter **Status = Active** for the live list. |
| **Changes** | The change log, newest first. |
| **Summary** | Counts by jurisdiction and licence type, plus the last run's status. |

The same data is kept in the repo as `data/licences.csv` and `data/changes.csv`.

**Don't edit the Licences or Summary tabs**: they're rewritten on each run. To
annotate firms, add your own tab and look them up by reference number with
`XLOOKUP`.

---

## Things to know

- **Accuracy.** The sheet mirrors what each register says. The EBA notes that its
  register has no legal standing and that national registers are authoritative.
  For a decision about a specific firm, check the regulator's own register (the
  link column).
- **Timing.** The PRA bank lists change monthly, so a UK bank licence change shows
  up when the next list is published. The FCA, EBA, ECB and FINMA are checked daily.
- **UK firms with old FRNs.** New UK payment firms get new FRNs and are caught by
  the daily scan. A long-established firm (old FRN) that newly adds a payment
  licence is caught by the weekly re-check only if it's already tracked. To watch
  specific firms, list their FRNs (one per line) in `data/fca_watch_frns.txt`.
- **EU withdrawals** usually show as "Removed from register". Some regulators keep
  withdrawn firms with an end date instead; those show as "Licence cancelled /
  revoked".
- **Safety guard.** If a register suddenly lists far fewer firms than the day
  before (broken download, format change), that source is skipped for the day
  rather than reporting hundreds of false removals. The run is marked as failed, so
  GitHub emails you.
- **If a run fails**, GitHub emails the repo owner. Open the run log; the line
  starting `ERROR` says which register failed. The other sources still update.
- **The FCA also sells a bulk "Register Extract"** (from about £4,500 a year for
  weekly files). If your employer already subscribes, it could replace the seed scan.

## Running it on your own computer (optional)

```bash
pip install -r requirements.txt
python -m regtracker doctor            # fetch each register, print a sample, save nothing
python -m regtracker run --no-sheets   # full run, writes data/licences.csv
python -m regtracker status
```

Set the same secrets as environment variables to use the FCA API and Google Sheets.


---

## National registers and the "Source check" tab

For countries listed under `national:` in `config.yaml` (currently the
Netherlands, Lithuania and Slovakia), the tracker reads the regulator's own register
directly instead of the EU (EBA) copy:

- the sheet shows the national data for that country;
- changes in the national register are alerted;
- the EU register is still read, and its changes for those countries are logged
  (not alerted) so the **Source check** tab can show how many hours later the EU
  copy caught up, and which firms one register lists but the other doesn't.

Use the Source check results to decide where else a direct source is worth
adding. A country can be added when its regulator offers a CSV or Excel
download: add an entry under `national:` with the file URL and either a fixed
`licence_type` or `type_patterns`. Each run saves a sample of every national
file to `data/debug/` so the reader can be checked against the real layout.

If a national source fails, that country falls back to the EU copy on the sheet
until it works again.

---

## Canada: MSBs and payment service providers

- **FINTRAC MSB registry** (`fintrac`): every money services business, Canadian
  and foreign, with status Registered / Expired / Ceased / Revoked. FINTRAC
  updates its registry monthly.
- **Bank of Canada RPAA lists** (`boc`): registered payment service providers,
  applicants (with application date) and refusals/revocations. The Bank's pages
  have no download, so the run opens them in a headless browser, reads the data
  the page loads, and opens each firm's detail page to collect its **email and
  website** (up to `details_per_run` new firms per run, cached in
  `data/state/boc_details.json`, so the full set fills in over a day or two).
- Each MSB on the sheet gets a **PSP status (Canada)**: *PSP registered*,
  *PSP applied*, *PSP refused/revoked*, or *No PSP registration or application*.
  There is no shared ID between FINTRAC and the Bank of Canada, so this is matched
  on legal and operating/trade names; filter the Canada tab on this column.
- Each run saves what it saw to `data/debug/boc_sample.txt` and
  `data/debug/fintrac_sample.txt`.

## Contact details (website, email, phone)

Registers rarely publish all three, so the tracker fills the gaps from other
free sources. It only fills blanks: anything the register itself shows is never
overwritten. The **Contact found via** column says where each value came from.

| Step | Where it runs | What it adds |
|---|---|---|
| FCA register, by FRN | every 2-hourly check (cached) | website and phone (and email if the FCA gives one) for UK banks from the PRA list |
| Bank of Lithuania firm pages | every 2-hourly check (cached) | email for Lithuanian firms |
| Wikidata, by LEI | every 2-hourly check (cached) | official website, mostly for banks (ECB / PRA lists) |
| The firm's own website | the **Contact details (firm websites)** workflow, every 6 hours | a general email (info@, contact@… preferred) and phone from the homepage and its contact / imprint pages |

The website job reads at most four pages per firm, obeys each site's
robots.txt, and re-checks a site after 180 days. Its first pass over ~10,000
websites takes a day or two. Results are stored in `data/state/contacts_web.json`
and appear in the sheet on the next 2-hourly check.

Firms with no known website (most EU payment and e-money firms on the EBA
register, Swiss firms) can't be filled this way; that would need a paid search
API.

## United States: MSBs and state money transmitter licences

**Every US money services business** comes from FinCEN's MSB registrant list
(updated weekly by FinCEN; the tracker checks twice a day). One row per firm,
with its MSB activities, whether it is a money transmitter, and the states it
told FinCEN it operates in.

**State money transmitter licences** come from the states that publish their
own licensee list: Florida, California, Massachusetts, Connecticut, Louisiana,
Indiana, Delaware and Alaska. Each is its own source, so a new or lost licence
in any of them raises an alert. The other states only publish licences through
NMLS Consumer Access, whose terms forbid automated extraction and use for
soliciting firms, so the tracker does not use it.

Columns on the **United States (US)** tab:

| Column | Meaning |
|---|---|
| FinCEN MSB (US) | On state-licence rows: whether the firm is also on FinCEN's MSB list |
| States of operation (FinCEN, US) | States the firm declared to FinCEN |
| State MT licences (US published lists) | States whose published list shows the firm licensed; "per firm's website" adds states listed on the firm's own licence page (only for firms whose website is known) |

Firms are matched across lists by name, so a firm registered under a different
name in one place can be missed. No state publishes pending applications, so
there is no "applied" group for the US.

**Registers that block automated downloads** (FinCEN, Massachusetts, Alaska):
download the file in your browser and upload it to `data/manual/` under the
name given in `data/manual/README.md`. The run summary says "waiting" for these
until a file is there.

## South Africa

| Source | What | Where it comes from |
|---|---|---|
| za_tppp | Third-party payment providers, with their sponsoring bank(s) | SARB public register (PDF; the link changes with each edition and is found automatically) |
| za_so, za_pchso | Payment system operators and payment clearing house system operators | SARB public registers |
| za_banks | Locally and foreign controlled banks, branches of foreign banks, mutual banks, with CEO, phone and website | Prudential Authority lists |
| za_casp | Licensed crypto asset service providers, with FSP number | FSCA list |

## Switzerland: SRO members

No Swiss self-regulatory organisation publishes its member list, and FINMA's
SRO member search shows at most 20 results per search, so there is no list to
download. The tracker looks firms up one at a time (40 per day, each re-checked
every 60 days): Swiss firms already in the sheet from other registers get a
**Swiss SRO** value, and names you add to `data/ch_sro_watch.txt` (one per line)
appear as rows on the Switzerland tab once FINMA shows them as SRO members.

## Telegram notifications

Every run that finds new licences, registrations or applications (new EMIs,
PIs, PSP applications and registrations, MSBs, MiCA CASPs, TPPPs, ADLAs...)
sends them to you on Telegram, grouped by country, with a link to each firm's
register entry and to the sheet. Limit it to certain countries, or change which
events are sent, under `telegram:` in config.yaml.

Setup: create a bot with @BotFather (/newbot) and copy its token; add it as the
repository secret TELEGRAM_BOT_TOKEN; send your new bot any message (e.g. "hi")
so the tracker knows where to reach you; make sure daily.yml passes
TELEGRAM_BOT_TOKEN to the run step.
