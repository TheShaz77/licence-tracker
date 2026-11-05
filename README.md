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

It runs free on **GitHub Actions**, writes to your **Google Sheet**, and can email you
a digest whenever something changes.

---

## What it covers today

| Source | Jurisdictions | Licence types | How changes are detected | Refresh |
|---|---|---|---|---|
| **FCA Register API** | UK | Authorised EMI, Small EMI, Authorised PI (API), Small PI (SPI), RAISP | Each firm's status re-checked daily; new FRNs scanned daily | Live |
| **PRA lists** (Bank of England) | UK | Banks (UK-incorporated and branches), building societies | Additions and removals between lists | Monthly |
| **EBA PSD2 register** | All EU/EEA countries (about 30) | PIs, EMIs, AISPs, small/exempt PIs and EMIs | Additions, removals, withdrawal dates, type and service changes | Daily |
| **ECB list of MFIs** | All EU countries | Credit institutions (banks), incl. branches | Additions and removals | Daily |
| **FINMA** | Switzerland | Banks, FinTech licence holders | Additions and removals | Daily |

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

Optional email digest (Gmail example: turn on 2-step verification, then create an
**App password** at <https://myaccount.google.com/apppasswords>):

| Name | Value |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | your Gmail address |
| `SMTP_PASSWORD` | the 16-character app password |
| `EMAIL_TO` | where to send it (comma-separate several addresses) |

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

That's it. The daily job runs at 06:17 UTC every day.

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
