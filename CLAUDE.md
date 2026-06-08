# CLAUDE.md — NYC Bodega Data Project

Project instructions and context for Claude Code. Read this before working in the repo.

## What this is

A data pipeline that builds a unified record of NYC bodegas by stitching together
public government datasets. The **government data is the base ("spine") layer**:
a complete, regulatorily-grounded store registry plus assortment flags. Catalog and
price layers (delivery apps, distributors) come later and bolt onto the same keys.

Scope is the **5 NYC boroughs only** (counties: `BRONX`, `KINGS`, `NEW YORK`,
`QUEENS`, `RICHMOND`).

## Architecture

- **Monorepo.** One repo, one loader per data source under `loaders/`, shared code in `common/`.
- **Each loader = its own Cloud Run Job**, triggered on a schedule by Cloud Scheduler.
- **Storage = Cloud SQL for PostgreSQL + PostGIS.** One table per source; join across them on `join_key`.
- ETL per loader is Extract (pull SODA/ArcGIS API) → Transform (filter/normalize) → Load (upsert).

```
bodega-data/
  common/              # shared: normalize.py (norm_*), db.py, schema.sql
  loaders/
    food_stores/       # job.py, transform.py, Dockerfile, requirements.txt  [PRIMARY/DONE-ish]
    snap/              # (todo)
    sla/               # (todo)
    tobacco/           # (todo)
    lottery/           # (todo)
    dohmh/             # (todo)
  joins/               # cross-source matching, runs after loaders
```

Loaders are independently deployable; build context is the loader subfolder.
Until `common/` has a second consumer, helpers may live inline in `transform.py`
(don't create `common/` prematurely). When sharing, either vendor a copy of
`common/` per loader, or build from repo root so the context includes it.

## Data sources (dataset IDs)

| Source | Role | Portal | Dataset ID |
|---|---|---|---|
| Retail Food Stores (Ag & Markets) | **spine** | data.ny.gov | `9a8c-vfzj` |
| SNAP-authorized retailers (USDA FNA) | flag: staples/EBT | ArcGIS hub `usda-snap-retailers-usda-fns` | (FeatureServer) |
| Liquor Authority active licenses | flag: alcohol + type | data.ny.gov | `9s3h-dpkz` (decoder PDFs on `hrvs-fxs2`) |
| Tobacco Retail Dealer licenses (DCWP) | flag: cigarettes | data.cityofnewyork.us | `adw8-wvxb` |
| Lottery retailers (Gaming Commission) | flag: lottery | data.ny.gov | `2vvn-pdyi` |
| DOHMH Restaurant Inspections | flag: fresh/prepared food | data.cityofnewyork.us | `43nn-pn8j` |

- NY State datasets (food stores, SLA, lottery) are **statewide** → always filter to the 5 boroughs.
- SODA API pattern: `https://<portal>/resource/<id>.json?$where=...&$limit=50000` (page with `$offset`; max 50k/request). Register a free app token (`X-App-Token` header) to raise rate limits.

## Schema (Cloud SQL, PostGIS)

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE TABLE stores (
  license_number    text PRIMARY KEY,            -- natural key, idempotent refresh
  source            text DEFAULT 'ny_rfs:9a8c-vfzj',
  dba text, entity text,
  house text, street text, city text, county text, zip text,
  geom              geometry(Point, 4326),
  estab_type        text,
  join_key          text,                          -- normalized, cross-source join
  ingested_at       timestamptz DEFAULT now()
);
CREATE INDEX ON stores USING gist (geom);
CREATE INDEX ON stores (join_key);
```

Build `geom` from the source `Georeference` WKT `POINT (lon lat)` via
`ST_SetSRID(ST_MakePoint(lon, lat), 4326)`.

## The bodega filter (the core logic)

"Bodega" is not a field. There is no single column that isolates them, and
establishment type can NOT separate a bodega from a supermarket (both are `AC`).
The filter is layered, in order of trust:

1. **Borough (certain):** `County in {BRONX, KINGS, NEW YORK, QUEENS, RICHMOND}`.
2. **Establishment-type retail gate (structural):** keep if code contains `A`
   (Store) and contains NONE of the warehouse/wholesale/processing letters
   `{D,E,F,G,H,I,L,M,N,O,P,Q,R,S,T,U,V,W,Z}`. (`K`=Vehicle is borderline; keep+flag.)
   This removes wholesale/distribution hybrids (`ACH`, `ACD`, `ACDK`, ...).
3. **Name exclusion (high precision):** drop chains/big-box and non-bodega food
   retailers (pharmacies, bakeries, cafés, butchers, liquor, seafood).

**Keep proper-named survivors.** Many real bodegas have proper-name DBAs
("LA ESQUINA"). Precision comes later: a survivor that matches a SNAP
"Convenience Store" type AND holds an SLA grocery-beer license is unambiguously a
bodega. Hard-filtering at load throws that signal away.

Current result: ~9.7k candidate bodegas across the 5 boroughs.

### Establishment type codes (NYSDAM)
`A`=Store, `B`=Bakery, `C`=Food Manufacturer, `D`=Food Warehouse, `E`=Beverage Plant,
`F`=Feed Mill (non-medicated), `G`=Processing Plant, `H`=Wholesale Manufacturer,
`I`=Refrigerated Warehouse, `J`=Multiple Operations, `K`=Vehicle, `L`=Produce Refrig.
Warehouse, `M`=Salvage Dealer, `N`=Wholesale Produce Packer, `O`=Produce
Grower/Packer/Broker, `P`=C.A. Room, `Q`=Feed Mill (medicated), `R`=Pet Food Mfr,
`S`=Feed Warehouse/Distributor, `T`/`U`=Disposal Plant, `V`=Slaughterhouse,
`W`=Farm Winery-Exempt, `Z`=Farm Product Use Only. The code is a SET — one letter
per licensed operation.

### SLA license types (for the alcohol flag, when SLA loader is built)
NY quirk: grocery/convenience stores can sell **beer/cider only**, not wine or liquor.
- `Grocery Store Beer` → beer (classic bodega).
- `Grocery Beer/Wine Product` → beer + low-ABV "wine product" (NOT regular wine).
- `Liquor Store` → dedicated wine/spirits shop = NOT a bodega (exclude).
Store the license TYPE, not a boolean. Decoder: SLA `leap-license-type-and-class-definitions.xlsx`.

## Join key convention

All sources normalize address to `"<house> <street> <zip5>"`:
- `norm_house`: leading digits only ("1477-1489" → "1477").
- `norm_street`: uppercase; strip punctuation; abbreviate (STREET→ST, AVENUE→AVE,
  EAST→E, WEST→W, ...); **strip ordinal suffixes** ("187TH"→"187") so agencies agree.
- `norm_zip`: first 5 digits.

Entity resolution across agencies is the hard part; the normalizer is shared in
`common/` so all loaders agree.

## Deploy pattern (Cloud Run Jobs)

Each loader deploys from its subfolder; Cloud Build builds the image (no local Docker):

```bash
gcloud run jobs deploy food-stores-etl \
  --source ./loaders/food_stores --region=us-east1 \
  --set-env-vars=INSTANCE=PROJECT:us-east1:INSTANCE,DB_NAME=bodega,DB_USER=postgres,DB_PASS=YOURPASS
gcloud run jobs execute food-stores-etl --region=us-east1
```

- DB connection uses the **Cloud SQL Python Connector** with the **instance connection
  name** (`project:region:instance`) — no IP/firewall management. Service account needs
  `roles/cloudsql.client`.
- Move `DB_PASS` to Secret Manager (`--set-secrets=DB_PASS=db-pass:latest`) when ready.
- Schedule via Cloud Scheduler hitting the Run Admin API (`...jobs/<job>:run`) with an
  OAuth service account that has `roles/run.invoker`.

## Conventions / gotchas

- **Idempotency:** all loads upsert `ON CONFLICT (license_number) DO UPDATE`. Re-running
  a monthly file/pull reconciles instead of duplicating. Keep handlers idempotent.
- **PostGIS extension must exist before first load** or the `geom` insert fails.
- **Dockerfile layer order:** copy `requirements.txt` and `pip install` BEFORE copying
  code, so the deps layer caches across redeploys.
- Refresh cadences differ (food stores periodic; SLA/lottery daily; tobacco semiannual);
  give each loader its own schedule and track `ingested_at`.

## Build status — what exists vs. what to create

**Current state: ONLY `loaders/food_stores/Dockerfile` exists.** Everything below
still needs to be written. Build the `food_stores` loader first, end to end, then
clone the pattern for the other sources.

### Files to create in `loaders/food_stores/`

1. **`requirements.txt`**
   ```
   requests
   pandas
   sqlalchemy
   pg8000
   cloud-sql-python-connector
   ```

2. **`transform.py`** — the bodega filter (see "The bodega filter" section above).
   Takes a DataFrame of raw SODA rows, returns the filtered/normalized frame with
   columns: `license_number, dba, entity, house, street, city, county, zip,
   estab_type, lon, lat, join_key`. SODA field names are
   lowercased/underscored (`dba_name`, `establishment_type`, `georeference`, etc.).
   Includes the `norm_house/norm_street/norm_zip` helpers (inline for now; move to
   `common/` when the SNAP loader needs them too).

3. **`job.py`** — entrypoint (matches `CMD ["python", "job.py"]`). Two functions:
   - `extract()`: GET the RFS SODA API (`9a8c-vfzj`) filtered to the 5 boroughs,
     paging on `$offset` until a short page; return a DataFrame.
   - `main()`: `transform(extract())`, connect via Cloud SQL Connector using env
     vars `INSTANCE / DB_NAME / DB_USER / DB_PASS`, load to a `stage` table, then
     `INSERT ... SELECT ... ON CONFLICT (license_number) DO UPDATE` into `stores`
     (build `geom` with `ST_SetSRID(ST_MakePoint(lon,lat),4326)`).

### One-time setup (before first run)

- In **Cloud SQL Studio** (or `gcloud sql connect`), run the `CREATE EXTENSION postgis`
  and the `CREATE TABLE stores` DDL from the Schema section above.
- Grant the Cloud Run service account `roles/cloudsql.client`.

### Then deploy & run (see Deploy pattern above)

```bash
gcloud run jobs deploy food-stores-etl --source ./loaders/food_stores --region=us-east1 \
  --set-env-vars=INSTANCE=PROJECT:us-east1:INSTANCE,DB_NAME=bodega,DB_USER=postgres,DB_PASS=YOURPASS
gcloud run jobs execute food-stores-etl --region=us-east1
```
Success = `SELECT count(*) FROM stores;` returns ~9.7k.

### Later (not yet)

- `.gcloudignore` at repo root so builds skip CSVs, `__pycache__`, venvs.
- `common/` — only once a second loader needs the shared `norm_*` helpers.

## Status / next steps

- [x] `loaders/food_stores/Dockerfile` written.
- [ ] Create `requirements.txt`, `transform.py`, `job.py` in `loaders/food_stores/`.
- [ ] Create `stores` table + PostGIS extension in Cloud SQL.
- [ ] Deploy `food-stores-etl` Cloud Run Job; get first execution green (~9.7k rows).
- [ ] Add Cloud Scheduler trigger (monthly) once the run is green.
- [ ] SNAP loader (ArcGIS FeatureServer, filter `State='NY'`, carries `Store_Type`).
- [ ] SLA, tobacco, lottery, DOHMH loaders (same job pattern, different endpoints).
- [ ] `joins/` — match flags onto spine via `join_key`; confirm proper-named
      survivors as bodegas when SNAP convenience-store + SLA grocery-beer corroborate.