# job-alerts

Checks employer job boards every 30 minutes for new internship and co-op postings that match
your keywords and locations, and posts each new match to a Discord channel. It runs for free
on GitHub Actions; no server needed.

You choose the industry and keywords. Ready-made presets cover chip design/FPGA and
materials/nuclear/medical, and a `general` preset works for any field: it searches the
[Simplify internship list](https://github.com/SimplifyJobs), which covers hundreds of companies.
The defaults look for GTA/Ottawa undergrad internships of 12–16 months. All of it is set in
`config.json` and you can change it.

## Set up your own bot (about 10 minutes)

### 1. Create a Discord webhook

In your Discord server, open the channel the alerts should go to:
**Edit Channel → Integrations → Webhooks → New Webhook → Copy Webhook URL**.
Keep the URL private; anyone with it can post to that channel.

### 2. Copy this repo to your GitHub account

Click **Use this template → Create a new repository** (or **Fork**). Make the new repo
**public**: GitHub Actions minutes are unlimited for public repos, while private repos get
2,000 free minutes a month, which a 30-minute schedule uses up.

### 3. Add the webhook as a secret

In your new repo: **Settings → Secrets and variables → Actions → New repository secret**.

- **Name:** `DISCORD_WEBHOOK_URL` (or any name you like; see step 4)
- **Secret:** the webhook URL from step 1

### 4. Pick your industry and keywords in `config.json`

Open `config.json` on GitHub and click the pencil icon to edit it. The `feeds` list at the
bottom has one entry per Discord channel. Replace it with your own, for example:

```json
"feeds": [
  {"name": "mine", "webhook_env": "DISCORD_WEBHOOK_URL", "preset": "general",
   "field_keywords": ["software", "data", "machine learning"]}
]
```

- `webhook_env` is the name of the secret from step 3.
- `preset` picks a ready-made industry setup from the `presets/` folder:

  | Preset | What it searches |
  |---|---|
  | `general` | **Any industry.** The Simplify internship list, which covers hundreds of companies. Your `field_keywords` decide which roles you get. |
  | `chips` | Chip design, FPGA, analog/mixed-signal, layout, verification and firmware roles at ~36 semiconductor and hardware employers, plus the Simplify list. |
  | `materials` | Nuclear, materials, project management, safety and medical roles at 23 energy, manufacturing, medical-device and pharma employers. |

- `field_keywords` are the role words a job title must contain, such as `"fpga"`,
  `"mechanical"`, `"finance"` or `"data"`. They replace the preset's own list. Leave the
  setting out to use the preset's list as-is.
- Add employers the preset doesn't cover with `"extra_companies": [...]` (see
  [Adding a company](#adding-a-company)).
- The location, 12–16-month and undergrad settings live in `defaults` at the top of the file.
  Change them there, or put any of them inside a feed to override it for that feed only.

Want several channels, say one per friend? Add one feed per channel, each with its own
`webhook_env`, its own secret and its own preset or keywords. A feed whose secret doesn't
exist is skipped with a warning.

### 5. Turn it on

Open the **Actions** tab. If GitHub asks, click to enable workflows. Then click
**job-alerts → Run workflow** to run it once by hand.

The first run posts every current match, so expect a burst of messages. After that it
only posts new postings. It then runs by itself at :17 and :47 past each hour. GitHub
starts scheduled runs late when it's busy, sometimes by an hour or more.

Each run commits a small "Update seen jobs" change (the `seen/` folder): that's how it
remembers what it has already posted. Run `git pull` before editing the repo locally.

## How a job gets posted

For each company in a feed, a posting is sent to Discord only if **all** of these hold:

1. It's new: not already in that feed's seen-jobs history.
2. Its title matches `title_keywords` (intern, co-op, student...).
3. Its title matches the feed's `field_keywords`, if the feed has any (ASIC, FPGA...).
4. Its location matches `location_keywords`.
5. It doesn't state a term other than 12 or 16 months. Postings that say 12/16 months, or
   say nothing about length, are kept; ones that only say "4 months" or "8 months" are dropped.
6. With `undergrad_only`, it isn't aimed only at Master's/PhD students.

Keyword matching ignores case, matches whole words, and accepts a trailing "s".

## Config reference

`config.json` has `defaults`, which apply to every feed, and `feeds`. Any default can be
overridden inside a feed.

| Field | Meaning |
|---|---|
| `title_keywords` | The title must contain one of these. |
| `field_keywords` | (Per feed, optional.) The title must also contain one of these. |
| `location_keywords` | The location must contain one of these (plain text, case-insensitive). Add the province to ambiguous names, e.g. `"burlington, on"`. |
| `duration_patterns` | Regexes for the term lengths you want (default: 12 or 16 months). |
| `other_duration_patterns` | Regexes for any stated length; a posting matching only these is dropped. |
| `search_terms` | What to type into boards that need a search query. |
| `undergrad_only` | Drop Master's/PhD-only postings. |
| `graduate_keywords` / `undergrad_keywords` | Words used by the undergrad check. |

Each feed has a `name` and a `webhook_env` (the secret name), plus either a `preset` or its
own `companies` list. Settings are applied in order: `defaults`, then the preset's file
(`presets/<preset>.json`), then the feed itself. `companies` in a feed replaces the preset's
list, and `extra_companies` adds to it. To make your own preset, copy one of the files in
`presets/` and edit it.

A company can add `"field_filter": false` to skip the `field_keywords` check (useful when the
whole company is in your field, like a nuclear utility), or `"title_filter": false` to skip
the `title_keywords` check (for sources that only list internships).

### Adding a company

Open any job on the company's careers site and look at the URL to find which system it uses:

| URL looks like | Config entry |
|---|---|
| `acme.wd5.myworkdayjobs.com/AcmeCareers/...` | `{"name": "Acme", "ats": "workday", "host": "acme.wd5", "tenant": "acme", "site": "AcmeCareers"}` |
| `job-boards.greenhouse.io/acme/...` | `{"name": "Acme", "ats": "greenhouse", "slug": "acme"}` |
| `jobs.lever.co/acme/...` | `{"name": "Acme", "ats": "lever", "slug": "acme"}` |
| `jobs.ashbyhq.com/acme/...` | `{"name": "Acme", "ats": "ashby", "slug": "acme"}` |
| `careers.acme.com/job/City-Title/123456/` (SuccessFactors) | `{"name": "Acme", "ats": "successfactors", "host": "careers.acme.com"}` |
| `xxxx.fa.ocs.oraclecloud.com/.../sites/CX_1/...` | `{"name": "Acme", "ats": "oracle", "host": "xxxx.fa.ocs.oraclecloud.com", "site": "CX_1"}` |
| `acme.bamboohr.com/careers/...` | `{"name": "Acme", "ats": "bamboohr", "slug": "acme"}` |

For Workday, the `tenant` is usually the first part of the host, and the `site` is the path
segment after the host (ignore `en-US/`). Eightfold, UKG, IBM and AMD's career site are also
supported; see existing entries for their fields.

Not every company can be added. Some careers sites block automated requests or have no
usable job API.

## Running it on your computer

```bash
pip install -r requirements.txt
python job_alerts.py --dry-run                  # print what would be posted; changes nothing
python job_alerts.py --dry-run --feed chips --only AMD
```

`--dry-run` never posts or writes anything. A real run (no `--dry-run`) needs each feed's
webhook in an environment variable named after its `webhook_env`.

## Maintenance

- The Simplify list's URL contains the year (`Summer2027-Internships`). Update it each fall.
- If a company's board changes, that company shows a warning in the Actions log and the
  rest keep working.
- GitHub turns off scheduled workflows in a public repo after 60 days without repo activity.
  If that happens, re-enable it from the Actions tab.
