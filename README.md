# ACCC FOI disclosures

An automatically updated archive of the [ACCC Freedom of Information disclosure log](https://www.accc.gov.au/about-us/freedom-of-information/freedom-of-information-disclosure-log).

A GitHub Actions workflow (`.github/workflows/scrape.yml`) runs every week (Monday morning AEST). You can also run it by hand from the Actions tab. Each run:

1. walks every page of the disclosure log,
2. downloads any documents not already in the archive,
3. writes per-disclosure metadata and a README, and
4. commits the changes.

Documents that ACCC later removes from its site stay in the archive and are marked as removed. ACCC only promises to keep documents up for 12 months.

## Layout

```
disclosures/
  README.md            table of every disclosure, newest first
  index.json           the same as machine-readable JSON
  2026-2027/           grouped by the year in the ACCC reference number
    100482/            the request number
      README.md        title, description, dates, links to documents
      metadata.json    full metadata (source URLs, sizes, first/last seen)
      ACCC FOI Request 100482-2026-2027-Document 1.pdf
      ...
  2018/proactive-A/    "ACCC Proactive Disclosure A/2018"
```

If two log entries share the same reference number, the second goes in a folder with `-2` on the end (for example `2014/1-2`).

## Running locally

```sh
pip install -r scraper/requirements.txt
python scraper/scrape.py                 # full incremental run
python scraper/scrape.py --no-download   # metadata only
python scraper/scrape.py --max-pages 1   # just the newest page
```
