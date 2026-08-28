# SEVs Register monitor — working notes

Read this before changing anything. It records what was expensive to learn and
is not obvious from the code.

## What this is

A daily watch on the Australian **Specialist and Enthusiast Vehicles Register**
— the list of model variants importable through a Registered Automotive
Workshop — published on **ROVER**, the Road Vehicle Regulator's portal. New
entries push to a phone via ntfy.sh; the whole register is browsable on a
GitHub Pages dashboard. Sibling project to `ari-hw-monitor`, same shape: free
GitHub Actions cron, `state.json` as the diff baseline, `docs/data.json` as the
dashboard feed, push to `main` **is** the deploy.

Baseline seeded 2026-08-28: **585 in force, 511 expired, 1096 total**, highest
number `SEV-001102`.

## The data source, and why it looks like that

**ROVER is a Power Pages (Dynamics 365) portal, and the register table is not
in the page HTML.** `/PublishedApprovals/SEVApprovals` ships an *encrypted,
signed* view configuration in a base64 `data-view-layouts` attribute; the
browser POSTs that blob back to the URL in `data-get-url`
(`/_services/entity-grid-data.json/<guid>`) to get rows. Three consequences:

- The blob (`Base64SecureConfiguration`) rotates whenever the department
  republishes the portal. **Read it from the page every run; never cache it to
  disk or hardcode it.**
- The POST needs an anti-forgery token that is *not* in the page. It comes from
  `GET /_layout/tokenhtml`, sent back as the `__RequestVerificationToken`
  header. Without it the grid answers 403.
- The register is selected by *view name*, not by a query. `config.json →
  source.views` maps our status to the portal's `ViewName` strings ("Portal
  View - SEV Entries In Force" / "... Expired"). If the department renames a
  view, `scan()` records a warning and `--self-test` exits 1 naming the view —
  it does not silently return half the register.

**One request for the whole register decodes to ~150 MB of JSON.** Every record
carries the complete option-set metadata for `statecode`/`statuscode`, so a
single row is ~265 KB. `pageSize: 1000` works and takes 15 s, but parsing it
peaks over a gigabyte. We page at 100 (~26 MB per response, discarded before
the next) — a full run is ~14 requests and ~30 s. Do not "optimise" this back
into one call.

**Sorting is limited to columns in the view.** There is no created-on field
exposed, so *new* entries cannot be found by date — they are found by diffing
SEV numbers against `state.json`. Numbers are sequential, so
`rvr_approvalnumber DESC` puts the newest first, which is only a convenience
for the log.

**Dates are `d/m/yyyy`.** `1/04/2009` is April 2009. `parse_au_date` refuses to
fall back to a month-first read on purpose — a silent month/day swap would move
half the register's build dates and never look wrong.

**Related columns arrive under a link alias**: `a_cb372b79...rvr_model`, not
`rvr_model`. `FIELD_MAP` lookups strip everything before the last dot, so a
regenerated alias does not break the parse.

**The detail page is the only place criterion, variant and notes exist.** The
grid has no such columns. `/PublishedApprovals/SEVDetails/?id=<guid>` renders
them into `<div id="SEVCriterion" class="question-label">`-style elements (ids
in `DETAIL_FIELDS`). Each page is ~450 KB, so only entries that are about to be
notified get enriched (`enrich.max_per_run`), and `apply_events` carries those
extras forward so a later plain run does not wipe them.

## Model reports (the MRE register)

**A SEVs listing does not mean anyone can import the car.** A Registered
Automotive Workshop needs an in-force *model report* for that variant before it
can comply one. So the register answers "is it allowed", and the MRE register
answers "can it actually be done, and by whom" — which is the question an
importer is really asking. `has_report` on each entry is that second answer;
the workshop named on the dashboard card is the report holder.

**The MRE register rides on the same grid endpoint and the same entity**
(`rvr_approval`) as the SEVs one, just a different page
(`/PublishedApprovals/MREApprovals`) and view (`Portal View - All Approvals:
MRE`). Its own columns carry the status (`In Force`, `Suspended`, …), the
subtype, level of compliance and a pre-formatted build date range. Seeded
2026-08-28: **988 reports, 939 of them in force**.

**The only join between the two registers is the "Based on" table on the model
report's own page.** The MRE grid has no SEV column, the SEV side has no
reverse link at all, and make/model text does not match between them (the SEV
entry is `NISSAN Skyline`, the report is `Skyline GT-R`). So each report costs
one ~450 KB page fetch to place, once, forever. One report can cover several
SEV entries (`MRE-000988` -> `SEV-001075`, `SEV-001076`), and reports whose
subtype is not "Specialist and Enthusiast Vehicles" (used motorcycles, second
stage manufacture) link to nothing and are never fetched.

**The backfill must not look like news.** ~950 links have to be read on a fresh
install, paced by `link_budget_per_run` (or `--backfill`). Two failure modes
sit either side of this and both were designed against:

- announce every first-time link and the first weeks are a wall of "model
  report approved" for reports that have existed for years;
- suppress every first-time link and a *genuinely new* report — which by
  definition is also being linked for the first time — is never announced at
  all.

The `new_to_us` flag is what separates them: a report absent from
`state["reports"]` on a run that is not the first ever is news, and stays news
(sticky through state) until it is linked, even if the budget defers it for
days. Everything else is backfill, and `run()` puts the SEV entries it touched
into `unsettled`, which mutes report events for them that run. Genuinely new
reports also sort to the front of the queue so a long backfill cannot starve
them.

**`has_report` is compared with `is not None`, not truthiness.** Entries
recorded before model reports existed in this project have no `has_report` key,
and reading that as False would announce that all 585 in-force cars just lost
their report. Same shape of bug as `first_seen` below.

**A collapsed MRE scan is not believed either** (`reconcile_reports`): if the
report register comes back empty or shrunk past `max_shrink_ratio`, the
previous map is kept and report events are switched off for the run. Otherwise
one bad response says "no workshop can build any of these any more".

## Traps that already bit

**`first_seen` must be tested by key presence, not truthiness.** Baseline runs
store `first_seen: null` deliberately — we know when *we* first read an entry,
not when the department added it, and stamping the baseline marks all 1096
entries "new" for a month. Written as `previous.get("first_seen") or stamp`,
the *next* run re-dates every null and does exactly that. The offline suite
catches this ("the dashboard counts it as new"); it was caught there first.

**No per-entry `last_seen`.** An updated-every-run timestamp on 1096 entries
turns each daily Actions commit into a full-file diff. The run time lives in
`state["last_run"]` alone; two consecutive live runs now differ by that one
line.

**A shrinking register is assumed to be a portal glitch, not 500 removals.**
`sanity.max_shrink_ratio` (15%) gates disappearance events; over that, nothing
is marked removed and a warning goes to the dashboard instead. A bad day at
`rover.infrastructure.gov.au` must not fire hundreds of pushes and, worse,
poison the baseline so the entries all re-fire as "new" tomorrow.

**ntfy headers are latin-1.** Register notes and model names carry curly quotes
and en dashes (`Mitsuoka Le–Seyde`, `‘Kei car’`); one non-latin-1 character in
a header raises `UnicodeEncodeError`, `send_ntfy` catches it, and the
notification dies silently while every test stays green. All headers go through
`header_safe`; emoji belong in the Tags header, which ntfy renders into the
title anyway.

**ROVER spells makes three different ways.** `NISSAN`, `Nissan` and `nissan`
are all in the register, as are `PORSCHE`/`Porsche` and `MITSUBISHI`. The
dashboard's brand picker groups on the upper-case key and displays
`preferred_spelling()` — most used, ties to Title Case over SHOUTING over lower
— and the page filters case-insensitively. Group on the raw string and one
brand becomes three entries in the picker.

**Events are recorded before they are sent.** `record_events` writes the
activity log the dashboard reads, and a failed push does *not* re-fire next
run — the run summary says how many were lost instead. Anything new that
produces events must go through `record_events`, or it will never appear on the
dashboard.

## Domain notes worth keeping straight

An entry on the register is **not** an import approval. It means that model
variant *may* be imported via a RAW; the owner still applies for a Vehicle
Import Approval. The SEVs pathway is one of several — 25-year-old vehicles,
pre-1989 vehicles and personal imports have their own — so "not on the
register" never means "cannot be imported".

Entries are per *variant*, and the variant condition can be narrow: several
`NISSAN STAGEA` rows exist as separate entries, and `Honda N-ONE e` is limited
to "MUST be JG5 Model Code BEV ONLY". Never collapse entries by make+model.

Entries expire (a few years out) and can be flagged **under review**, which is
the signal that one may be varied or removed — that is why `under_review` is
pushed by default while routine expiries are not.

## Testing

`python -m tests.test_checker` — 85 checks, no network. `tests/mock_rover.py`
serves the portal shapes (both list pages with their base64 layouts, tokenhtml,
the grid POST with real paging/sorting, detail pages) *and* stands in for
ntfy.sh, capturing every push so tests assert on titles, bodies and the Click
header. It rejects a grid POST without the token, which is what keeps the token
path honest.

The suite runs the real `checker.py` as a subprocess against a temp copy of the
project, so scraping, diffing, notification and dashboard writing are exercised
together. Live checks against the real portal: `--self-test` (verifies the
views, the parse and the detail page) and `--dry-run`.
