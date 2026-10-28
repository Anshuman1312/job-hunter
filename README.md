# Job Hunter

A Python job-hunting agent that finds **Python backend / data engineering** roles
in the **2–3.5 years** band, filters out 4+ year roles, and emails one digest per
run with **visible apply links** for every job.

Current configuration (see `config.json`):

* **Roles:** Python backend (Django/FastAPI/Flask) and Data Engineering
  (ETL, analytics, data platform).
* **Experience:** 2–3.5 years. `3+`, `3.5+` and `3–5` pass; `4+`, `4–6`, `5+`
  are rejected.
* **Locations:** India (on-site/hybrid) or remote that is not restricted to the
  US/Canada/UK/EU.
* **Delivery:** one HTML + plain-text email per run. Every card shows the
  **Apply link** as text plus a **Job page** link, and the email ends with an
  **ALL APPLY LINKS** list.
* **Referrals:** jobs at companies where you have a referral are marked
  `REFERRAL: <COMPANY>`, listed first, and counted in the header.

---

## 1. Sources

| Source | How it is fetched |
| --- | --- |
| LinkedIn, Indeed, Glassdoor, Naukri | `python-jobspy` search queries |
| ZipRecruiter | `python-jobspy` (remote Python) |
| Google Jobs | `python-jobspy` — best effort; often returns 0 from this network |
| YC Work at a Startup | public `/jobs/search` JSON + detail payloads (`boards.py`) |
| Wellfound | `__NEXT_DATA__` Apollo cache on role pages |
| Cutshort | `__NEXT_DATA__` job list (50 jobs/page, India) |
| Foundit (free portal) | public `/middleware/jobsearch` JSON — free for candidates |
| Accenture | Workday public jobs API |
| Capgemini | `cg-jobstream` public search API |
| Wipro / HCL | SuccessFactors public RSS feeds |
| Infosys, TCS, Persistent, Siemens, Amdocs, Barclays | company-scoped JobSpy queries (Naukri/Indeed/LinkedIn) |

All sources are normalised into the same `Job` model, so one matcher applies to
everything.

---

## 2. Setup (Windows / PowerShell)

```powershell
cd "C:\Users\Anshuman Pandey\OneDrive\Desktop\job-hunter"

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --timeout 120 --retries 10 -r requirements.txt
playwright install chromium
```

Gmail SMTP uses an **App Password** (not the account password), read from `.env`:

```env
GMAIL_APP_PASSWORD=your-16-character-app-password
```

`.env` is already present and ignored by git. Rotate the password if it leaks.

---

## 3. Running

```powershell
# Full scrape + one digest email
.\.venv\Scripts\python.exe main.py run

# Preview only: writes a .eml to logs/outbox, sends nothing, writes no state
.\.venv\Scripts\python.exe main.py run --dry-run

# Reuse the last scrape cache (fast; no network)
.\.venv\Scripts\python.exe main.py run --no-scrape

# Continuous mode (default every 120 minutes, see watch.interval_minutes)
.\.venv\Scripts\python.exe main.py watch

# Custom feeds only (YC, Wellfound, Cutshort, Foundit, company career sites)
.\.venv\Scripts\python.exe main.py run --boards-only

# Other useful flags
.\.venv\Scripts\python.exe main.py run --limit 20
.\.venv\Scripts\python.exe main.py run --sites linkedin,indeed
.\.venv\Scripts\python.exe main.py stats
.\.venv\Scripts\python.exe main.py test-email
```

Auto-apply is **disabled** (`application.auto_apply_enabled: false`,
`max_daily_auto_applies: 0`). No forms are submitted; you apply from the email
links.

---

## 4. Email digest

Subject: `[Job Hunter] <N> new Python / Data Engineer job(s) - <date>`

Each job card contains:

* title (clickable), company, location, source, experience band, score
* a short description snippet
* **Apply now** button, visible **Apply link**, and a **Job page** link when it
  differs
* a green `REFERRAL: <COMPANY>` badge when the company is in `referrals`

The plain-text part repeats the same information and ends with an
**ALL APPLY LINKS** section so every URL can be copied quickly.

Only jobs that were successfully emailed are written to `seen_jobs`, so a
failed email does not lose jobs. `email.digest_max_jobs` is 500 by default.

---

## 5. Decision pipeline

```text
Scrape all sources → normalise → dedupe by id/URL
        │
        ├─ already in seen_jobs → skip
        │
        ├─ matcher rejects → log reason (4+ years, senior title, location, …)
        │
        └─ matched → sort referrals first → one digest email → mark seen
```

Matcher rules:

* title must look like backend/Python or data engineering
* hard reject: internship, fresher, frontend, full stack, DevOps, ML/AI,
  sales, management and other non-target titles
* hard reject: senior leadership titles (lead/principal/staff/architect/…)
* experience: accept when the stated range overlaps 2–3.5; reject `4+`
* if years are not stated: accepted by policy (`allow_unknown_experience`),
  except senior titles
* location: India, or remote not restricted to a blocked country

---

## 6. Configuration (`config.json`)

| Section | Purpose |
| --- | --- |
| `candidate` | name, email, phone, city, LinkedIn |
| `search` | JobSpy queries and custom-board queries (`yc`, `wellfound`, `cutshort`, `foundit`) |
| `filters` | experience band, role/stack keywords, title rules, remote blocklist, allowed cities |
| `company_sources` | direct careers feeds: `workday`, `successfactors`, `capgemini` |
| `referrals` | referral company names + aliases used for badges and sorting |
| `application` | CV path, browser profile, screening answers, auto-apply toggle |
| `email` | SMTP settings, digest mode, `digest_max_jobs`, dry-run |
| `boards` | timeouts, page limits for YC/Wellfound/Cutshort/Foundit |
| `watch` | interval, jitter, max cycles |
| `database` | SQLite path (`applications.db`) |
| `logging` | level and rotating file |

---

## 7. Database

`applications.db` holds:

* `applications` + `application_events` — legacy auto-apply audit trail
  (unused while auto-apply is off)
* `seen_jobs` — jobs already emailed, with title, company, URL, site and
  timestamps

```powershell
.\.venv\Scripts\python.exe main.py stats
sqlite3 applications.db "SELECT COUNT(*) FROM seen_jobs;"
```

---

## 8. Scheduling

### Windows Task Scheduler

* **Program:** `C:\Users\Anshuman Pandey\OneDrive\Desktop\job-hunter\.venv\Scripts\python.exe`
* **Arguments:** `main.py run`
* **Start in:** `C:\Users\Anshuman Pandey\OneDrive\Desktop\job-hunter`
* **Trigger:** daily, e.g. 9:00 AM
* **Setting:** “Do not start a new instance”

### cron

```cron
0 9 * * * cd /path/to/job-hunter && .venv/bin/python main.py run >> logs/cron.log 2>&1
```

`main.py watch` is an alternative that keeps running and checks every
`watch.interval_minutes`.

---

## 9. Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Offline coverage: YOE boundaries (3.5+ accepted, 4+ rejected), senior/entry
filtering, remote location rules, `seen_jobs` dedupe, digest rendering with
apply links, and attachment handling with a dummy PDF.

---

## 10. Troubleshooting

| Symptom | Fix |
| --- | --- |
| Google Jobs returns 0 | Known limitation from this network; other sources still run |
| LinkedIn 429 | Rate limited; wait or add a proxy in `scraping.proxy` |
| Foundit returns no rows | Their backend occasionally returns `ECONNREFUSED`; retries next run |
| Accenture returns many generic roles | Matcher rejects non-Python titles; tune `company_sources` |
| `SMTPAuthenticationError (535)` | Use a Gmail App Password |
| Duplicate jobs | They are deduped by id and URL; `seen_jobs` also blocks repeats |
| Email clipped by Gmail | Open “View entire message”; every link is still present |

---

## 11. Limitations & responsible use

* Job boards change markup and rate-limit; every source degrades gracefully and
  the run continues.
* The matcher is intentionally strict; false rejections can happen when a
  description buries requirements. Tune `filters` in `config.json`.
* Referral badges only indicate that you have a contact — always confirm the
  referral before applying.
* Never commit `.env`, `applications.db`, `browser_session/` or the CV.
