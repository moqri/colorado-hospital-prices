# Colorado Hospital Prices (reported by payers)

Browser app plus a small local server. The server keeps the insurer plan files and a SQLite database on disk, so the app never downloads or parses plans when you open it.

## Run

```
python3 server.py        # then open http://localhost:8765/
```

First run: if `data/mrf.sqlite` does not exist, the server builds it (about a minute with downloads). The page shows "Building the local data…" until it is ready.

## Layout

| Path | Purpose |
|---|---|
| `public/index.html` | The whole front end (one file). Only `public/` is served; Python files, data and the database are not reachable over HTTP. |
| `server.py` | HTTP server and `/api/*`. Security headers, gzip, response cache (cleared after each refresh), `/healthz`. |
| `ingest.py`, `analyze.py`, `hospitals.py` | Quarterly build: download plans, compute compliance and price statistics, match hospitals. |
| `sources.json` | The plan list. |
| `tests/smoke.py` | `python3 tests/smoke.py` checks a running server. |
| `Dockerfile` | Container image; mount a volume at `/app/data`. |

## Running in production

```
HOST=0.0.0.0 PORT=8765 python3 server.py      # or: docker build -t price-monitor . && docker run -p 8765:8765 -v pm-data:/app/data price-monitor
```

Put it behind a reverse proxy (Caddy, nginx) that terminates HTTPS. The server uses only the Python standard library. The first start builds the database in the background (about two minutes with downloads, and the page says so); `/healthz` reports `building`. After that the server rebuilds the data by itself about every 90 days and swaps the new database in atomically, so visitors never see a half-built state. Set `ACCESS_LOG=1` for request logs.

## How the data flows

| Step | File | What it does |
|---|---|---|
| Sources | `sources.json` | One entry per plan: label, insurer, original download URL. |
| Ingest | `ingest.py` | Downloads each plan to `data/raw/`, reads it and writes `data/mrf.sqlite` (providers, services, rates). Also writes `data/plans.js` and `data/manifest.json`. |
| Server | `server.py` | Serves the app, the `/api/*` endpoints and `data/raw/` files. Re-runs the ingest by itself when the data is older than 90 days (about a quarter), checking every 6 hours. |
| App | `index.html` | Find prices uses `/api` (instant). Every tab reads precomputed results from the database through `/api`. Report scores, schema checks and per-service price statistics are calculated once in `ingest.py` (using `analyze.py`), so nothing is parsed in the browser. |

Manual refresh: `python3 ingest.py --refresh` (downloads everything again). A failed download keeps the previous file for that plan.

## Keeping sources current

Insurers publish new files every January and July. Some links change each time (Cigna links are signed and expire, Aetna and Denver Health file names carry dates). When a refresh reports `FAILED`, update that plan's `url` in `sources.json` and run `python3 ingest.py --refresh`.

Anthem's current links are on <https://www.anthem.com/co/machine-readable-file/search/>. UnitedHealthcare publishes its files at <https://transparency-in-coverage.uhc.com> (the list is at `/api/v1/uhc/blobs/`; the Colorado Division of Insurance links return HTTP 403). One small plan is included (City and County of Denver, 0.3 MB, filed under Denver Health). Most UnitedHealthcare Colorado files are gigabytes, so they are not included.

## Endpoints

`/api/status`, `/api/providers?q=`, `/api/provider?tin=`, `/api/services?q=`, `/api/service?code=`, `/api/rates?code=&tin=`, `/api/rank?code=`, `/api/compliance`, `/api/matrix?q=&cls=&fee=&key=&all=`, `/api/items?plan=&q=&net=&cls=`, `/api/item?id=&cls=`, `/api/npi?n=` (public NPI registry lookup, used for provider addresses).

## Hospitals and the map

`hospitals.py` reads the Colorado hospital list from CMS Care Compare (data.cms.gov), finds map coordinates with the US Census geocoder and NPIs from the public NPI registry (cached in `data/hospitals_cache.json`, redone with `--refresh`). Each hospital is matched to the tax IDs in the plans by NPI and by name; only hospitals that match and have prices are shown. `/api/hospitals` feeds the map.

## How hospitals are matched to prices

Insurers file rates under tax IDs (TINs), not hospital names. For each Colorado hospital on CMS Care Compare, `hospitals.py` looks up its NPIs in the public NPI registry (hospital taxonomy codes only, found by name or by street address) and finds the tax IDs whose provider groups list those NPIs, plus tax IDs whose name equals the hospital's. Tax IDs with no dollar prices, laboratories, pathology, radiology and physician groups are dropped, and the rest must look like a hospital or health system. Hospitals that end up with no tax ID, or no prices, are not shown. Health systems often file under one tax ID for several hospitals; the hospital page says so (`shared`). Insurer-filed rates can differ from the rates in a hospital's own price list.

## Search shows priced services only

Search results, hospital search and the procedure list only include services with a dollar price (a negotiated amount above zero) at one or more hospitals, and a hospital's own search only lists services that hospital has a price for. `public/procedures.json` holds the plain-language names (157 entries); entries with no price are left out automatically.

## Example hospital

Clicking the empty search box on the home page offers one example hospital, Denver Health & Hospital Authority (most prices, all four insurers, largest city). Change `EXAMPLE_HOSP` near the top of the script in `public/index.html` to pick another.

## Hospital aliases

`hospital_aliases.json` holds hand-checked additions to the automatic hospital matching (display names such as UCHealth University of Colorado Hospital, and extra tax IDs with the evidence for each). The CMS name stays searchable and is shown as "Listed by CMS as".

## File size limit

No downloaded plan file may be larger than 100 MB (the downloader stops at that size), so the largest Anthem plans (about 210 MB) are not included.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `HOST`, `PORT` | `127.0.0.1`, `8765` | Where the server listens. Use `HOST=0.0.0.0` in a container. |
| `RATE_LIMIT_PER_MIN` | `240` | API requests per minute per client before a 429 reply (`0` = off). |
| `TRUST_PROXY` | off | Set to `1` behind your own reverse proxy so the client address is read from `X-Forwarded-For`. |
| `ACCESS_LOG` | off | Set to `1` to print one line per request. |

The server answers `SIGTERM` by finishing and exiting cleanly. `GET /healthz` returns `{"ok": true, "building": false}` once data is ready.
