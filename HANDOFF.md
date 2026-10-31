# Handoff: rebuilding Colorado Hospital Prices (reported by payers)

Everything needed to rebuild the site from scratch is the 14 files below plus public data that the code downloads itself. No database or downloaded plan needs to be handed over.

## 1. Files to hand over

| File | What it is |
|---|---|
| `server.py` | Python standard-library web server: serves `public/`, the `/api/*` endpoints, `/healthz`, and rebuilds the data about every 90 days. |
| `ingest.py` | Downloads each plan (100 MB cap), reads it and builds `data/mrf.sqlite`. Plans with fewer than 100 negotiated dollar prices are left out (`MIN_PRICES`). |
| `analyze.py` | Per-plan report numbers (dollar share, percentage rows, duplicates, format checks) and per-service price statistics. |
| `hospitals.py` | Builds the hospital list and matches each hospital to the tax IDs and NPIs in the plans. |
| `sources.json` | The list of plans: name, insurer, download URL. Links change each January and July (see section 3). |
| `hospital_aliases.json` | Hand-checked display names and extra tax IDs, each with the evidence for it. |
| `public/index.html` | The whole front end (one file). |
| `public/procedures.json` | Plain-language names for the common procedures. |
| `public/robots.txt` | Crawler rules. |
| `Dockerfile`, `.dockerignore`, `.gitignore` | Container build and ignore rules. `data/` is never committed. |
| `README.md` | How it runs, how the data flows, settings. |
| `tests/smoke.py`, `tests/test_units.py` | Checks: smoke test against a running server, unit tests. |

Not handed over (rebuilt automatically): `data/mrf.sqlite` (about 736 MB), `data/raw/` (downloaded plans, about 456 MB), `data/*cache*.json`, `data/manifest.json`.

## 2. Public data the code fetches by itself

| Data | Source | Used for |
|---|---|---|
| Colorado hospital list (96 hospitals) | CMS Care Compare, `data.cms.gov/provider-data` dataset `xubh-q36u` | The list of hospitals and their addresses. |
| Map coordinates | US Census geocoder, `geocoding.geo.census.gov` | Hospital pins. |
| NPIs and addresses | NPPES NPI registry API, `npiregistry.cms.hhs.gov/api` | Matching hospitals to tax IDs in the plans. |
| ZIP lookups | `api.zippopotam.us` | Filling missing city or ZIP details. |
| Map tiles and libraries | OpenStreetMap tiles, Leaflet 1.9.4 and markercluster from cdnjs | The map in the browser. |

## 3. Public data to find again: the insurers' price files

The negotiated-rate files are published by each insurer under the federal Transparency in Coverage rule. `sources.json` holds the current links; to rebuild, re-find them:

- **Where to look:** the Colorado Division of Insurance sheet of insurer links (it returns HTTP 403 to automated requests, so open it in a normal browser), and each insurer's own page: Anthem (`anthem.com/co/machine-readable-file/search/`), Aetna (`transparency-proxy.aetna.com`), Cigna (signed links that expire), UnitedHealthcare (`transparency-in-coverage.uhc.com`, list at `/api/v1/uhc/blobs/`), Kaiser, Denver Health, Select Health.
- **Rules followed:** only the currently enforced CMS schema; no file over 100 MB; insurers' own original files only.
- **Known gaps:** very large Anthem, Aetna and UnitedHealthcare files (over 100 MB), and hospital-level rates for Cigna at UCHealth.

## 4. Rebuild steps

1. Put the files above in one folder (keep `public/` and `tests/` as folders).
2. Check each URL in `sources.json`; replace expired links (Cigna, Aetna and Denver Health names carry dates).
3. `python3 server.py` (Python 3.12, no packages to install). The first start downloads the plans and builds the data in about 7 minutes with a clean cache; then open `http://localhost:8765/`.
4. `python3 tests/smoke.py` and `python3 tests/test_units.py` to check it.

Hosting: needs a persistent disk (4 GB or more) mounted at `data/` and 2 GB of RAM for the rebuild. Settings: `HOST=0.0.0.0`, `TRUST_PROXY=1` behind a proxy. The current site runs on Render from the private repo `moqri/colorado-hospital-prices`.

## 5. Rules the site follows (keep them)

Only hospitals with dollar prices are shown; the site says "plans", never "files" or "carrier"; no account, add-your-own-plan or dark-mode features; insurer-filed rates can differ from a hospital's own price list, and the pages say so; do not get around access blocks.
