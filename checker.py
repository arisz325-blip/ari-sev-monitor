#!/usr/bin/env python3
"""
Australian SEVs Register monitor.

Watches the Specialist and Enthusiast Vehicles (SEVs) Register published on
ROVER — the Road Vehicle Regulator's portal — for model variants that become
legal to import into Australia, diffs each run against the last, pushes an
Android notification via ntfy.sh, and writes a JSON feed for the dashboard.

Usage:
    python checker.py                    # normal run
    python checker.py --dry-run          # check + report, send no notifications
    python checker.py --self-test        # verify the portal is still parseable
    python checker.py --notify-test      # send one test notification and exit
    python checker.py --rebuild          # rebuild the baseline, notify nothing

Environment:
    NTFY_TOPIC     ntfy.sh topic to publish to      (required to notify)
    NTFY_SERVER    override ntfy server             (default https://ntfy.sh)
    NTFY_TOKEN     bearer token for protected topic (optional)
    ROVER_BASE     point the scraper at this base   (testing only)
"""

from __future__ import annotations

import argparse
import base64
import html as htmllib
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
DATA_PATH = ROOT / "docs" / "data.json"

STATE_VERSION = 2
MELBOURNE = ZoneInfo("Australia/Melbourne")

# Notification presentation per event type: (title, ntfy tag, priority).
# Emoji go in Tags, never in Title — see header_safe().
EVENT_META: dict[str, tuple[str, str, int]] = {
    "new":            ("New SEV approval",     "racing_car",              4),
    "report_added":   ("Model report approved", "white_check_mark",       4),
    "returned":       ("SEV back in force",    "arrows_counterclockwise", 4),
    "expired":        ("SEV expired",          "hourglass_flowing_sand",  3),
    "removed":        ("SEV entry removed",    "wastebasket",             3),
    "under_review":   ("SEV under review",     "warning",                 3),
    "report_lost":    ("Model report gone",    "warning",                 3),
    "review_cleared": ("Review finished",      "white_check_mark",        2),
    "expiry_changed": ("SEV expiry changed",   "calendar",                2),
    "updated":        ("SEV details updated",  "pencil2",                 2),
}

EVENT_ORDER = ["new", "report_added", "returned", "under_review", "report_lost",
               "removed", "expired", "review_cleared", "expiry_changed", "updated"]

# Grid attribute name -> our field name. Related-entity columns arrive prefixed
# with a link alias ("a_cb37....rvr_model"); the alias is stripped before the
# lookup, so this survives the portal regenerating that alias.
FIELD_MAP = {
    "rvr_approvalnumber":     "sev",
    "rvr_approvalid":         "id",
    "rvr_manufacturer":       "make",
    "rvr_model":              "model",
    "rvr_categorytype":       "category",
    "rvr_modelcode":          "model_code",
    "rvr_builddatefrom":      "build_from",
    "rvr_builddateto":        "build_to",
    "rvr_approvalexpirydate": "expiry",
    "rvr_underreview":        "under_review",
}

# Fields whose change is worth an "updated" event. Expiry has its own event.
TRACKED_FIELDS = ["make", "model", "category", "model_code",
                  "build_from", "build_to"]

FIELD_LABEL = {
    "make": "Make", "model": "Model", "category": "Category",
    "model_code": "Model code", "build_from": "Build date from",
    "build_to": "Build date to", "expiry": "Expiry",
}

# The model report register (MRE) rides on the same entity and the same grid
# endpoint, with its own columns.
REPORT_FIELD_MAP = {
    "rvr_approvalnumber":     "mre",
    "rvr_approvalid":         "id",
    "rvr_manufacturer":       "make",
    "rvr_model":              "model",
    "rvr_approvalstatus":     "report_status",
    "rvr_approvalsubtypeid":  "subtype",
    "rvr_categorytype":       "category",
    "rvr_levelofcompliance":  "compliance",
    "rvr_mrebuilddaterange":  "build_range",
}

# What an entry's own page adds that the grid does not have. `variant` is the
# important one: the grid's model code is the *series* ("M35 SERIES", "S20"),
# and this is the chassis code that tells five otherwise identical listings
# apart ("HM35", "PNM35", "GRS204").
DETAIL_CARRY = ("criterion", "variant", "variant_details", "notes", "build_range")

# Detail-page fields, keyed by the element id ROVER gives them.
DETAIL_FIELDS = {
    "SEVApprovalNo":     "sev",
    "SEVMake":           "make",
    "SEVModel":          "model",
    "SEVCategory":       "category",
    "SEVBDR":            "build_range",
    "SEVVariant":        "variant",
    "SEVVariantDetails": "variant_details",
    "SEVCriterion":      "criterion",
    "SEVExpiry":         "expiry_display",
    "SEVModelCodeName":  "model_code",
    "SEVNotes":          "notes",
}

# The model report detail page uses the raw attribute names as element ids.
# Only the holder's business identity is taken — the page also publishes a
# named contact, their phone and their email, which we have no need for.
REPORT_DETAIL_FIELDS = {
    "rvr_publishedapprovalholder": "holder",
    "rvr_publishedwebsite":        "website",
    "rvr_approvalstatus":          "report_status",
}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def melbourne_time(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        return (datetime.fromisoformat(iso.replace("Z", "+00:00"))
                .astimezone(MELBOURNE).strftime("%a %d %b %Y, %I:%M %p"))
    except ValueError:
        return None


def parse_au_date(value: str | None) -> str | None:
    """ROVER prints dates d/m/yyyy. Return ISO, or None if unparseable.

    Never fall back to a month-first read: '1/04/2009' is April, and guessing
    January would silently shift half the register's build dates by months.
    """
    if not value:
        return None
    value = str(value).strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def month_year(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return None
    return f"{d.month:02d}/{d.year}"


def build_range(row: dict) -> str:
    start = month_year(row.get("build_from_iso")) or row.get("build_from") or "?"
    end = month_year(row.get("build_to_iso")) or row.get("build_to")
    return f"{start} - {end}" if end else f"{start} - no end date"


def preferred_spelling(spellings: dict[str, int]) -> str:
    """Pick the nicest of ROVER's spellings of one make.

    The register holds "NISSAN", "Nissan" and "nissan" for the same brand.
    Most-used wins; ties go to Title Case over SHOUTING over lower case, then
    alphabetically so the picker does not reshuffle itself between runs.
    """
    def rank(name: str) -> tuple[int, int, str]:
        if name[:1].isupper() and any(c.islower() for c in name):
            style = 2
        elif name.isupper():
            style = 1
        else:
            style = 0
        return spellings[name], style, name

    return max(spellings, key=rank)


def title_of(row: dict) -> str:
    make = (row.get("make") or "").strip()
    model = (row.get("model") or "").strip()
    return " ".join(p for p in (make, model) if p) or row.get("sev", "SEV entry")


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"version": STATE_VERSION, "entries": {}, "recent_events": []}
    try:
        with STATE_PATH.open(encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log(f"  state.json is unreadable ({exc}) — rebuilding baseline")
        return {"version": STATE_VERSION, "entries": {}, "recent_events": []}
    state.setdefault("entries", {})
    state.setdefault("recent_events", [])
    state["version"] = STATE_VERSION
    return state


def save_state(state: dict) -> None:
    state["last_run"] = now_iso()
    STATE_PATH.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n",
                          encoding="utf-8")


# --------------------------------------------------------------------------
# the ROVER portal
# --------------------------------------------------------------------------

class PortalError(RuntimeError):
    """The portal answered, but not with anything we can read."""


class Rover:
    """Talks to the Power Pages entity grid behind the published registers.

    The register table is not in the page HTML — the page ships an encrypted,
    signed view configuration and the browser POSTs it back to
    /_services/entity-grid-data.json to get rows. That blob rotates whenever
    the department republishes the portal, so it is read fresh from the page
    every run and must never be cached to disk.
    """

    def __init__(self, cfg: dict):
        http = cfg.get("http", {})
        self.base = (os.environ.get("ROVER_BASE") or cfg["source"]["base"]).rstrip("/")
        self.delay = float(http.get("request_delay_seconds", 1.0))
        self.timeout = float(http.get("timeout_seconds", 90))
        self.retries = int(http.get("max_retries", 3))
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": http.get("user_agent", "Mozilla/5.0"),
            "Accept-Language": "en-AU,en;q=0.9",
        })
        self._token: str | None = None
        self._last_request = 0.0

    # -- plumbing ----------------------------------------------------------

    def _sleep(self) -> None:
        gap = self.delay - (time.monotonic() - self._last_request)
        if gap > 0:
            time.sleep(gap)
        self._last_request = time.monotonic()

    def _request(self, method: str, path: str, **kw) -> requests.Response:
        url = path if path.startswith("http") else f"{self.base}{path}"
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._sleep()
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kw)
                resp.raise_for_status()
                resp.encoding = "utf-8"
                return resp
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < self.retries:
                    time.sleep(2 ** attempt)
        raise PortalError(f"{method} {url} failed after {self.retries} attempts: {last}")

    def get_text(self, path: str) -> str:
        return self._request("GET", path).text

    def token(self) -> str:
        """Power Pages hands out its anti-forgery token from its own endpoint.

        It is not in the page HTML — /_layout/tokenhtml returns a fragment
        holding it, and the grid POST is rejected without the matching header.
        """
        if self._token is None:
            frag = self.get_text(f"/_layout/tokenhtml?_={int(time.time() * 1000)}")
            m = re.search(r'value="([^"]+)"', frag)
            if not m:
                raise PortalError("no anti-forgery token in /_layout/tokenhtml")
            self._token = m.group(1)
        return self._token

    # -- the grid ----------------------------------------------------------

    def list_page(self, list_path: str) -> tuple[str, dict[str, dict]]:
        """Return (grid data url, {view name: layout}) for a register page."""
        html = self.get_text(list_path)
        url = re.search(r'data-get-url="([^"]+)"', html)
        layouts = re.search(r"data-view-layouts='([^']+)'", html)
        if not url or not layouts:
            raise PortalError(
                f"{list_path} no longer exposes an entity grid "
                "(data-get-url / data-view-layouts missing)")
        try:
            decoded = json.loads(base64.b64decode(layouts.group(1)).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise PortalError(f"could not decode the view layouts: {exc}") from exc
        return url.group(1), {v["ViewName"]: v for v in decoded}

    def grid_rows(self, grid_url: str, layout: dict, referer: str,
                  page_size: int, max_pages: int,
                  sort: str = "rvr_approvalnumber DESC") -> Iterator[dict]:
        """Yield every raw record of one view, a page at a time.

        Paged rather than pulled in one shot on purpose: the response repeats
        the full option-set metadata for *every* record, so the whole register
        in a single call decodes to ~150 MB of JSON. One page of 100 is ~26 MB
        and is discarded before the next is fetched.
        """
        page = 1
        seen = 0
        while page <= max_pages:
            body = {
                "base64SecureConfiguration": layout["Base64SecureConfiguration"],
                "sortExpression": sort,
                "search": None, "filter": None, "metaFilter": None,
                "page": page, "pageSize": page_size,
                "filterByUser": False, "timezoneOffset": 600,
                "customParameters": [],
            }
            resp = self._request("POST", grid_url, json=body, headers={
                "__RequestVerificationToken": self.token(),
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{self.base}{referer}",
            })
            try:
                data = resp.json()
            except ValueError as exc:
                raise PortalError(f"grid page {page} was not JSON: {exc}") from exc
            records = data.get("Records") or []
            for record in records:
                yield record
            seen += len(records)
            if not data.get("MoreRecords") or not records:
                expected = data.get("ItemCount") or 0
                if expected and seen < expected:
                    log(f"    warning: view reported {expected} rows, read {seen}")
                return
            page += 1
        log(f"    warning: stopped at max_pages={max_pages} with more rows waiting")

    def detail_page(self, detail_path: str, record_id: str) -> str:
        """The HTML of one approval's own page (~450 KB — fetch sparingly)."""
        return self.get_text(f"{detail_path}{record_id}")

    def detail(self, detail_path: str, record_id: str,
               fields: dict[str, str] = DETAIL_FIELDS) -> dict:
        return parse_labels(self.detail_page(detail_path, record_id), fields)


def parse_labels(html: str, fields: dict[str, str]) -> dict:
    """Pull ROVER's label/value pairs out of an approval detail page."""
    out: dict[str, str] = {}
    for element_id, field in fields.items():
        m = re.search(
            r'<div id="%s"[^>]*class="question-label"[^>]*>(.*?)</div>\s*</div>'
            % re.escape(element_id), html, re.S)
        if not m:
            m = re.search(
                r'<div id="%s"[^>]*class="question-label"[^>]*>(.*?)</div>'
                % re.escape(element_id), html, re.S)
        if m:
            text = re.sub(r"<[^>]+>", " ", m.group(1))
            text = re.sub(r"\s+", " ", htmllib.unescape(text)).strip()
            if text:
                out[field] = text
    return out


def parse_related_sevs(html: str) -> list[str]:
    """The SEV entries a model report was written against.

    This link exists in exactly one place: the "Based on" table on the model
    report's own page. The MRE grid does not carry it, the SEV side has no
    reverse link, and make/model text does not match reliably between the two
    registers — so this page fetch is the only honest way to say whether a
    given SEV entry has a model report.
    """
    return sorted({
        m.group(1) for m in re.finditer(
            r'href="/PublishedApprovals/SEVDetails/\?id=[^"]*"[^>]*>\s*'
            r"(SEV-\d+)\s*</a>", html)})


def record_to_row(record: dict, status: str, detail_url: str) -> dict | None:
    """Flatten one grid record into our own shape."""
    values: dict[str, Any] = {}
    for attr in record.get("Attributes", []):
        field = FIELD_MAP.get(str(attr.get("Name", "")).split(".")[-1])
        if field:
            values[field] = attr.get("DisplayValue")

    sev = str(values.get("sev") or "").strip()
    if not sev:
        return None

    row = {
        "sev": sev,
        "id": record.get("Id") or values.get("id"),
        "make": str(values.get("make") or "").strip(),
        "model": str(values.get("model") or "").strip(),
        "category": str(values.get("category") or "").strip(),
        "model_code": str(values.get("model_code") or "").strip(),
        "build_from": values.get("build_from"),
        "build_to": values.get("build_to"),
        "expiry": values.get("expiry"),
        "under_review": str(values.get("under_review") or "").strip().lower() == "yes",
        "status": status,
    }
    row["build_from_iso"] = parse_au_date(row["build_from"])
    row["build_to_iso"] = parse_au_date(row["build_to"])
    row["expiry_iso"] = parse_au_date(row["expiry"])
    row["title"] = title_of(row)
    row["url"] = f"{detail_url}{row['id']}" if row.get("id") else None
    return row


def scan(rover: Rover, cfg: dict) -> tuple[dict[str, dict], list[str]]:
    """Read every view of the register. Returns ({sev number: row}, warnings)."""
    src = cfg["source"]
    warnings: list[str] = []
    grid_url, layouts = rover.list_page(src["list_path"])
    detail_url = f"{rover.base}{src['detail_path']}"

    rows: dict[str, dict] = {}
    for status, view_name in src["views"].items():
        layout = layouts.get(view_name)
        if layout is None:
            warnings.append(
                f"view '{view_name}' is gone from {src['list_path']} "
                f"(portal now offers: {', '.join(sorted(layouts)) or 'nothing'})")
            log(f"  {status}: MISSING view '{view_name}'")
            continue
        count = 0
        for record in rover.grid_rows(grid_url, layout, src["list_path"],
                                      int(src.get("page_size", 100)),
                                      int(src.get("max_pages", 40))):
            row = record_to_row(record, status, detail_url)
            if row is None:
                continue
            # In force wins: an entry listed in two views is still importable.
            if row["sev"] not in rows or status == "in_force":
                rows[row["sev"]] = row
            count += 1
        log(f"  {status}: {count} entries")
    if not rows:
        raise PortalError("the register returned no entries at all")
    return rows, warnings


# --------------------------------------------------------------------------
# model reports
# --------------------------------------------------------------------------

def record_to_report(record: dict, detail_url: str) -> dict | None:
    values: dict[str, Any] = {}
    for attr in record.get("Attributes", []):
        field = REPORT_FIELD_MAP.get(str(attr.get("Name", "")).split(".")[-1])
        if field:
            values[field] = attr.get("DisplayValue")

    mre = str(values.get("mre") or "").strip()
    if not mre:
        return None
    report = {
        "mre": mre,
        "id": record.get("Id") or values.get("id"),
        "make": str(values.get("make") or "").strip(),
        "model": str(values.get("model") or "").strip(),
        "report_status": str(values.get("report_status") or "").strip(),
        "subtype": str(values.get("subtype") or "").strip(),
        "category": str(values.get("category") or "").strip(),
        "compliance": str(values.get("compliance") or "").strip(),
        "build_range": str(values.get("build_range") or "").strip(),
    }
    report["url"] = f"{detail_url}{report['id']}" if report.get("id") else None
    return report


def scan_reports(rover: Rover, cfg: dict) -> tuple[dict[str, dict], list[str]]:
    """Read the model report register. Returns ({MRE number: report}, warnings)."""
    mcfg = cfg.get("model_reports", {})
    if not mcfg.get("enabled", True):
        return {}, []
    warnings: list[str] = []
    grid_url, layouts = rover.list_page(mcfg["list_path"])
    detail_url = f"{rover.base}{mcfg['detail_path']}"

    reports: dict[str, dict] = {}
    for key, view_name in mcfg["views"].items():
        layout = layouts.get(view_name)
        if layout is None:
            warnings.append(
                f"model report view '{view_name}' is gone from {mcfg['list_path']} "
                f"(portal now offers: {', '.join(sorted(layouts)) or 'nothing'})")
            log(f"  reports {key}: MISSING view '{view_name}'")
            continue
        for record in rover.grid_rows(grid_url, layout, mcfg["list_path"],
                                      int(mcfg.get("page_size", 100)),
                                      int(mcfg.get("max_pages", 40))):
            report = record_to_report(record, detail_url)
            if report:
                reports[report["mre"]] = report
        log(f"  reports {key}: {len(reports)} model reports")
    return reports, warnings


def link_reports(rover: Rover, cfg: dict, reports: dict[str, dict],
                 state: dict) -> set[str]:
    """Attach each model report to the SEV entries it was written against.

    The link only exists on the report's own ~450 KB page, so it is fetched
    once per report and then carried in state forever. A fresh install has
    ~950 to work through, paced by `link_budget_per_run` (or done in one go by
    `--backfill`).

    Returns the MRE numbers whose links were merely *discovered* by that
    backfill. Those must not be announced as "a model report appeared today" —
    only reports that showed up in the register after we started watching are
    news, which is what the sticky `new_to_us` flag tracks: a report first seen
    while the budget was exhausted is still news whenever it finally gets
    linked, and reports seen on the very first run never are.
    """
    mcfg = cfg.get("model_reports", {})
    known = state.get("reports", {})
    first_ever = not known
    subtypes = [s.lower() for s in mcfg.get("subtypes", []) if s]
    budget = int(mcfg.get("link_budget_per_run", 60))

    pending: list[dict] = []
    for mre, report in reports.items():
        before = known.get(mre)
        if before and before.get("linked"):
            report["sev"] = before.get("sev", [])
            report["linked"] = True
            for field in ("holder", "website"):
                if before.get(field):
                    report[field] = before[field]
            continue
        if subtypes and report.get("subtype", "").lower() not in subtypes:
            # Used motorcycle and second-stage manufacture reports are not
            # written against a SEV entry at all; nothing to fetch.
            report["sev"] = []
            report["linked"] = True
            continue
        report["new_to_us"] = (not first_ever
                               and (before is None or bool(before.get("new_to_us"))))
        pending.append(report)

    # Genuinely new reports go first: they are the ones worth a notification,
    # and a long backfill queue must not push them past the budget.
    pending.sort(key=lambda r: not r.get("new_to_us"))

    backfilled: set[str] = set()
    for report in pending[:budget]:
        if not report.get("id"):
            continue
        try:
            html = rover.detail_page(mcfg["detail_path"], report["id"])
        except PortalError as exc:
            log(f"    model report fetch failed for {report['mre']}: {exc}")
            continue
        report.update(parse_labels(html, REPORT_DETAIL_FIELDS))
        report["sev"] = parse_related_sevs(html)
        report["linked"] = True
        if not report.pop("new_to_us", False):
            backfilled.add(report["mre"])
    if pending:
        done = min(len(pending), budget)
        tail = "" if len(pending) <= budget else \
            f" — {len(pending) - budget} still to do, rerun or use --backfill"
        log(f"  linked {done} model report(s) to their SEV entries{tail}")
    return backfilled


def reconcile_reports(state: dict, reports: dict[str, dict],
                      cfg: dict) -> tuple[dict[str, dict], bool, list[str]]:
    """Decide whether to believe this run's model report scan.

    Same reasoning as the SEV shrink guard: a bad response must not flip a
    thousand entries to "no model report" and push a wave of alarms. When the
    scan looks wrong we keep the previous map and say so.
    """
    known = state.get("reports", {})
    if not cfg.get("model_reports", {}).get("enabled", True):
        return {}, False, []
    if not known:
        return reports, bool(reports), []
    if not reports:
        return known, False, ["the model report register returned nothing — "
                              "keeping the previous model report data"]
    shrink = 1 - (len(reports) / len(known))
    if shrink > float(cfg.get("sanity", {}).get("max_shrink_ratio", 0.15)):
        return known, False, [
            f"model reports came back {len(reports)}, down from {len(known)} "
            f"({shrink:.0%} smaller) — keeping the previous data and not "
            "reporting any report as lost"]
    return reports, True, []


def attach_reports(rows: dict[str, dict], reports: dict[str, dict]) -> None:
    """Hang each SEV entry's model reports off it, newest report first."""
    by_sev: dict[str, list[dict]] = {}
    for mre in sorted(reports, reverse=True):
        report = reports[mre]
        for sev in report.get("sev") or []:
            by_sev.setdefault(sev, []).append({
                "mre": mre,
                "holder": report.get("holder"),
                "status": report.get("report_status"),
                "compliance": report.get("compliance"),
                "website": report.get("website"),
                "url": report.get("url"),
            })
    for sev, row in rows.items():
        attached = by_sev.get(sev, [])
        row["reports"] = attached
        row["has_report"] = any(r.get("status") == "In Force" for r in attached)


def report_holders(row: dict) -> list[str]:
    return sorted({r["holder"] for r in row.get("reports", [])
                   if r.get("status") == "In Force" and r.get("holder")})


# --------------------------------------------------------------------------
# diffing
# --------------------------------------------------------------------------

def matches_watch(row: dict, watch: dict) -> bool:
    """Does this entry match the watch filters? No filters set = everything."""
    makes = [m.lower() for m in watch.get("makes", []) if m]
    keywords = [k.lower() for k in watch.get("keywords", []) if k]
    categories = [c.lower() for c in watch.get("categories", []) if c]
    if not (makes or keywords or categories):
        return True
    haystack = " ".join(str(row.get(f) or "") for f in
                        ("make", "model", "model_code", "category")).lower()
    if makes and any(m in (row.get("make") or "").lower() for m in makes):
        return True
    if keywords and any(k in haystack for k in keywords):
        return True
    if categories and any(c in (row.get("category") or "").lower() for c in categories):
        return True
    return False


def diff(state: dict, rows: dict[str, dict], cfg: dict,
         report_events: bool = True,
         unsettled: frozenset[str] = frozenset()) -> tuple[list[dict], list[str]]:
    """Compare this run against the stored baseline.

    Returns (events, warnings). Every event carries the row it is about; the
    caller decides which of them become notifications.

    `unsettled` holds SEV numbers whose model report links were filled in by
    the backfill this run — their report state was discovered, not changed, so
    they get no report event.
    """
    known: dict[str, dict] = state.get("entries", {})
    warnings: list[str] = []
    events: list[dict] = []
    sanity = cfg.get("sanity", {})

    # A portal hiccup that returns half the register must not fire hundreds of
    # "removed" pushes. Disappearances are only believed when the register
    # came back roughly the size it was last time.
    trust_disappearances = True
    live_known = {s: e for s, e in known.items() if e.get("status") != "gone"}
    if live_known:
        shrink = 1 - (len(rows) / len(live_known))
        if shrink > float(sanity.get("max_shrink_ratio", 0.15)):
            trust_disappearances = False
            warnings.append(
                f"register returned {len(rows)} entries, down from {len(live_known)} "
                f"({shrink:.0%} smaller) — treating disappearances as a portal "
                "glitch, not as removals")
    if len(rows) < int(sanity.get("min_expected_entries", 200)):
        warnings.append(f"only {len(rows)} entries came back — the register is "
                        "normally far larger; check the portal")

    for sev, row in rows.items():
        before = known.get(sev)

        if before is None:
            events.append({"type": "new", "row": row,
                           "detail": f"{build_range(row)} · "
                                     f"{row['category'] or 'category n/a'}"})
            continue

        was_gone = before.get("status") == "gone"
        if before.get("status") != row["status"]:
            if row["status"] == "in_force":
                events.append({"type": "returned", "row": row,
                               "detail": ("relisted as in force"
                                          if was_gone else "expired entry is back in force")
                                         + f" (expires {row.get('expiry') or 'n/a'})"})
            elif not was_gone:
                events.append({"type": "expired", "row": row,
                               "detail": f"expired {row.get('expiry') or ''}".strip()})

        # `is not None` matters: entries recorded before model reports were
        # tracked have no has_report at all, and reading that as False would
        # announce that every car in the register just lost its report.
        had_report = before.get("has_report")
        if (report_events and had_report is not None
                and had_report != row.get("has_report")
                and sev not in unsettled):
            if row["has_report"]:
                holders = ", ".join(report_holders(row))
                events.append({"type": "report_added", "row": row,
                               "detail": "a model report is now in force"
                                         + (f" — {holders}" if holders else "")
                                         + "; a workshop can build it now"})
            else:
                events.append({"type": "report_lost", "row": row,
                               "detail": "no model report is in force any more — "
                                         "nobody can import it until one is"})

        if bool(before.get("under_review")) != row["under_review"]:
            if row["under_review"]:
                events.append({"type": "under_review", "row": row,
                               "detail": "the entry may be varied or removed"})
            else:
                events.append({"type": "review_cleared", "row": row,
                               "detail": "review flag cleared"})

        same_view = before.get("status") == row["status"]
        if same_view and (before.get("expiry_iso") or before.get("expiry")) != \
                (row.get("expiry_iso") or row.get("expiry")):
            events.append({"type": "expiry_changed", "row": row,
                           "detail": f"expiry {before.get('expiry') or 'n/a'} -> "
                                     f"{row.get('expiry') or 'n/a'}"})

        changes = [f"{FIELD_LABEL.get(f, f)}: {before.get(f) or 'n/a'} -> "
                   f"{row.get(f) or 'n/a'}"
                   for f in TRACKED_FIELDS if (before.get(f) or "") != (row.get(f) or "")]
        if changes:
            events.append({"type": "updated", "row": row,
                           "detail": "; ".join(changes[:3])})

    if trust_disappearances:
        for sev, before in known.items():
            if sev in rows or before.get("status") == "gone":
                continue
            row = dict(before)
            row["status"] = "gone"
            events.append({"type": "removed", "row": row,
                           "detail": "no longer listed on either register view"})

    order = {t: i for i, t in enumerate(EVENT_ORDER)}
    events.sort(key=lambda e: (order.get(e["type"], 99), e["row"].get("sev", "")))
    return events, warnings


def apply_events(state: dict, rows: dict[str, dict], events: list[dict],
                 baseline: bool = False) -> None:
    """Fold this run's rows into the stored baseline.

    Entries recorded by a baseline run get no first_seen: we know when *we*
    first read them, not when the department added them, and stamping the
    baseline would light up the whole register as "new" for a month.

    Nothing per-entry records the current time on an ordinary run either — a
    last_seen field would rewrite all 1000+ entries daily and turn every
    commit into a full-file diff. The run time lives in state["last_run"].
    """
    entries = state.setdefault("entries", {})
    stamp = None if baseline else now_iso()
    for sev, row in rows.items():
        stored = dict(row)
        previous = entries.get(sev, {})
        # Key presence, not truthiness: a baseline entry stores first_seen as
        # null on purpose, and `or stamp` would re-date the whole register on
        # the next run — lighting up all 1000+ entries as new.
        stored["first_seen"] = previous["first_seen"] if "first_seen" in previous \
            else stamp
        # Detail-page extras are read once and then carried, so a run that did
        # not fetch this entry's page does not drop what we already know.
        for field in (*DETAIL_CARRY, "detailed"):
            if row.get(field):
                stored[field] = row[field]
            elif previous.get(field):
                stored[field] = previous[field]
        entries[sev] = stored
    for event in events:
        if event["type"] == "removed":
            entry = entries.get(event["row"].get("sev"))
            if entry is not None:
                entry["status"] = "gone"


# --------------------------------------------------------------------------
# notifications
# --------------------------------------------------------------------------

_HEADER_SUBS = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...", " ": " ",
}


def header_safe(text: str) -> str:
    """Make a string safe to put in an HTTP header.

    Headers are latin-1. Register notes and model names carry curly quotes and
    en dashes, and one non-latin-1 character anywhere raises
    UnicodeEncodeError — which send_ntfy catches, so the notification dies
    silently while every test stays green. Emoji belong in ntfy's Tags header,
    which renders them into the title anyway.
    """
    for bad, good in _HEADER_SUBS.items():
        text = text.replace(bad, good)
    return text.encode("latin-1", "replace").decode("latin-1")


def model_key(row: dict) -> tuple[str, str, str]:
    """What counts as "the same car" for grouping."""
    return ((row.get("make") or "").upper().strip(),
            (row.get("model") or "").upper().strip(),
            (row.get("model_code") or "").upper().strip())


def bundle_events(events: list[dict], cfg: dict) -> list[dict]:
    """Collapse same-kind events on the same model into one notification.

    The department publishes a model's variants in one batch — five Stagea
    entries landed together on 2026-08-23 — and five separate pushes saying
    "NISSAN STAGEA" are five interruptions carrying one piece of news.
    Bundling happens at the notification layer only: the dashboard's activity
    feed still lists every event separately.
    """
    if not cfg.get("notify", {}).get("group_same_model", True):
        return [{"type": e["type"], "rows": [e["row"]], "detail": e["detail"]}
                for e in events]
    bundles: list[dict] = []
    index: dict[tuple, dict] = {}
    for event in events:
        key = (event["type"], *model_key(event["row"]))
        bundle = index.get(key)
        if bundle is None:
            bundle = {"type": event["type"], "rows": [], "detail": event["detail"]}
            index[key] = bundle
            bundles.append(bundle)
        bundle["rows"].append(event["row"])
    return bundles


def variant_line(row: dict) -> str:
    """`HM35 — 300RX, 3.0lt V6 VQ30DD 191 kW`, as far as we know it."""
    variant = (row.get("variant") or "").strip()
    details = (row.get("variant_details") or "").strip()
    if variant.upper() == "ALL":
        variant = ""
    if variant and details:
        return f"{variant} — {details[:90]}"
    return variant or details[:110] or row.get("sev", "")


def notification_body(event: dict) -> str:
    rows = event["rows"]
    row = rows[0]
    if len(rows) > 1:
        bits = [f"{row['title']} — {len(rows)} variants",
                build_range(row)]
    else:
        bits = [f"{row['title']} ({row.get('sev', '')})".strip(), build_range(row)]
    line = " · ".join(p for p in (row.get("category"), row.get("model_code")) if p)
    if line:
        bits.append(line)
    if row.get("criterion"):
        bits.append(f"Criterion: {row['criterion']}")
    if len(rows) > 1:
        for member in rows[:6]:
            bits.append(f"· {variant_line(member)}")
        if len(rows) > 6:
            bits.append(f"· +{len(rows) - 6} more")
        bits.append(", ".join(r.get("sev", "") for r in rows[:8]))
    else:
        if row.get("variant") and row["variant"].upper() != "ALL":
            bits.append(f"Variant: {row['variant']}")
        if row.get("variant_details"):
            bits.append(row["variant_details"][:200])
    # The register listing is only half the answer: without an in-force model
    # report, no workshop can actually import the car yet.
    if any("has_report" in r for r in rows):
        covered = [r for r in rows if r.get("has_report")]
        holders = sorted({h for r in covered for h in report_holders(r)})
        if len(rows) > 1:
            bits.append(f"Model report: {len(covered)} of {len(rows)}"
                        + (f" — {', '.join(holders[:2])}" if holders else ""))
        elif holders:
            bits.append(f"Model report: {', '.join(holders[:3])}")
        elif row.get("reports"):
            bits.append("Model report: none in force (one exists but is not)")
        else:
            bits.append("Model report: none yet — no workshop can build it")
    if row.get("expiry"):
        bits.append(f"Register entry expires {row['expiry']}")
    bits.append(event["detail"])
    return "\n".join(b for b in bits if b)


def send_ntfy(bundles: list[dict], dry_run: bool, extra: int = 0) -> int:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    server = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").strip().rstrip("/")
    token = os.environ.get("NTFY_TOKEN", "").strip()

    if not bundles:
        return 0
    if dry_run:
        for bundle in bundles:
            rows = bundle["rows"]
            log(f"  [dry-run] would notify: {bundle['type']} — "
                f"{rows[0].get('sev')} {rows[0]['title']}"
                + (f" (+{len(rows) - 1} variants)" if len(rows) > 1 else ""))
        return 0
    if not topic:
        log("  NTFY_TOPIC is not set — skipping notifications")
        return 0

    sent = 0
    for index, event in enumerate(bundles):
        row = event["rows"][0]
        count = len(event["rows"])
        title, tag, priority = EVENT_META.get(event["type"],
                                              ("SEVs Register", "bell", 3))
        title = f"{title}: {row['title']}" + (f" ×{count}" if count > 1 else "")
        body = notification_body(event)
        if extra and index == len(bundles) - 1:
            body += f"\n\n(+{extra} more changes this run — see the dashboard)"
        headers = {k: header_safe(v) for k, v in {
            "Title": title,
            "Priority": str(priority),
            "Tags": tag,
        }.items()}
        if row.get("url"):
            headers["Click"] = header_safe(row["url"])
            headers["Actions"] = header_safe(f"view, Open on ROVER, {row['url']}")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        # A dropped notification is gone for good — the event is recorded as
        # handled either way and never re-fires — so retry before giving up,
        # and let the caller surface whatever still did not land.
        for attempt in range(1, 4):
            try:
                resp = requests.post(f"{server}/{topic}", data=body.encode("utf-8"),
                                     headers=headers, timeout=20)
                resp.raise_for_status()
                sent += 1
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 3:
                    log(f"  notification FAILED after {attempt} attempts: {exc}")
                else:
                    time.sleep(2 * attempt)
    log(f"  sent {sent}/{len(bundles)} notifications")
    return sent


def record_events(state: dict, events: list[dict]) -> None:
    """Prepend events to the rolling activity log the dashboard reads.

    write_dashboard_data takes no events argument on purpose: it reads this
    log, so anything that produces events must land here first or it will not
    show up on the dashboard at all.
    """
    stamp = now_iso()
    state["recent_events"] = ([
        {"type": e["type"], "detail": e["detail"], "sev": e["row"].get("sev"),
         "title": e["row"]["title"], "url": e["row"].get("url"), "at": stamp}
        for e in events
    ] + state.get("recent_events", []))[:200]


# --------------------------------------------------------------------------
# dashboard
# --------------------------------------------------------------------------

DASHBOARD_FIELDS = ("sev", "make", "model", "category", "model_code",
                    "build_from_iso", "build_to_iso", "expiry_iso",
                    "under_review", "status", "url", "criterion", "variant",
                    "variant_details", "notes", "first_seen", "has_report")


def write_dashboard_data(state: dict, warnings: list[str], started: str,
                         cfg: dict) -> None:
    dash = cfg.get("dashboard", {})
    today = date.today()
    soon = (today + timedelta(days=int(dash.get("expiring_soon_days", 120)))).isoformat()
    recent_days = int(dash.get("recent_days", 30))
    new_since = (datetime.now(timezone.utc) - timedelta(days=recent_days)).isoformat()

    # Empty and false fields are left out entirely: the register is ~1100
    # entries and this file is fetched by a phone. The dashboard treats a
    # missing key as empty.
    rows = []
    for entry in state.get("entries", {}).values():
        row = {k: entry[k] for k in DASHBOARD_FIELDS if entry.get(k)}
        if row.get("notes"):
            row["notes"] = row["notes"][:400]
        # Keep ROVER's own date wording only where our parse failed, so a
        # surprising format still shows up instead of a blank cell.
        for raw, iso in (("build_from", "build_from_iso"),
                         ("build_to", "build_to_iso"),
                         ("expiry", "expiry_iso")):
            if entry.get(raw) and not entry.get(iso):
                row[raw] = entry[raw]
        # Only what the card shows: number, who holds it, and whether it is
        # actually in force. The full record stays in state.json.
        reports = [{"mre": r["mre"], "holder": r.get("holder"),
                    "status": r.get("status"), "website": r.get("website")}
                   for r in entry.get("reports", [])]
        if reports:
            row["reports"] = reports
        if (entry.get("first_seen") or "") >= new_since:
            row["is_new"] = True
        if (entry.get("status") == "in_force" and entry.get("expiry_iso")
                and today.isoformat() <= entry["expiry_iso"] <= soon):
            row["expiring_soon"] = True
        rows.append(row)

    rows.sort(key=lambda r: (r.get("first_seen") or "", r.get("sev") or ""),
              reverse=True)

    tally: dict[str, dict] = {}
    for row in rows:
        make = (row.get("make") or "").strip()
        if row.get("status") != "in_force" or not make:
            continue
        # ROVER's own spelling is inconsistent — "NISSAN" and "Nissan" are one
        # brand and must not become two entries in the picker. Group on the
        # upper-case key, then show whichever spelling the register uses most
        # (preferring a mixed-case one when it is a tie).
        slot = tally.setdefault(make.upper(),
                                {"count": 0, "with_report": 0, "spellings": {}})
        slot["count"] += 1
        slot["with_report"] += 1 if row.get("has_report") else 0
        slot["spellings"][make] = slot["spellings"].get(make, 0) + 1
    makes = [{"key": key, "label": preferred_spelling(slot["spellings"]),
              "count": slot["count"], "with_report": slot["with_report"]}
             for key, slot in tally.items()]
    makes.sort(key=lambda m: (-m["count"], m["label"].lower()))

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps({
        "generated_at": now_iso(),
        "started_at": started,
        "source": {
            "name": "SEVs Register (ROVER)",
            "list_url": f"{cfg['source']['base']}{cfg['source']['list_path']}",
        },
        "counts": {
            "total": len(rows),
            "in_force": sum(1 for r in rows if r.get("status") == "in_force"),
            "expired": sum(1 for r in rows if r.get("status") == "expired"),
            "under_review": sum(1 for r in rows if r.get("under_review")
                                and r.get("status") == "in_force"),
            "new_recent": sum(1 for r in rows if r.get("is_new")),
            "expiring_soon": sum(1 for r in rows if r.get("expiring_soon")),
            "with_report": sum(1 for r in rows if r.get("status") == "in_force"
                               and r.get("has_report")),
            "without_report": sum(1 for r in rows if r.get("status") == "in_force"
                                  and not r.get("has_report")),
            "model_reports": len(state.get("reports", {})),
        },
        # Brand picker: in-force entries only, with counts.
        "makes": makes,
        "recent_days": recent_days,
        "expiring_soon_days": int(dash.get("expiring_soon_days", 120)),
        "warnings": warnings,
        "watch": cfg.get("watch", {}),
        "recent_events": state.get("recent_events", [])[:60],
        "entries": rows,
    }, indent=1) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------

def backfill_details(rover: Rover, cfg: dict, rows: dict[str, dict],
                     state: dict) -> None:
    """Read each entry's own page once, for the fields the grid does not carry.

    Half the in-force register shares a make and model with something else, and
    148 entries are indistinguishable from the grid columns alone — five NISSAN
    Stagea rows all reading "M35 SERIES" are really HM35 300RX, M35 Axis 350S,
    NM35 250T, PM35 350RX and PNM35 350RX FOUR. Only the entry page says so.

    ~1100 pages at ~450 KB each, so it is paced by `details.budget_per_run` and
    the result is carried in state forever. Entries never seen before jump the
    queue: their notification is worth nothing without the variant in it.
    """
    dcfg = cfg.get("details", {})
    if not dcfg.get("enabled", True):
        return
    known = state.get("entries", {})
    # On the first run every entry is "never seen before"; queue-jumping them
    # all would mean 1100 page fetches in one go, inside a 25-minute Actions
    # job. A baseline run is plain backfill, paced like any other.
    first_ever = not known
    budget = int(dcfg.get("budget_per_run", 60))
    new_cap = int(dcfg.get("max_new_per_run", 25))
    include_expired = bool(dcfg.get("include_expired", True))

    fresh: list[dict] = []
    live: list[dict] = []
    rest: list[dict] = []
    for sev, row in rows.items():
        before = known.get(sev)
        if before and before.get("detailed"):
            for field in DETAIL_CARRY:
                if before.get(field):
                    row[field] = before[field]
            row["detailed"] = True
            continue
        if not row.get("id"):
            continue
        if before is None and not first_ever:
            fresh.append(row)
        elif row["status"] == "in_force":
            live.append(row)
        elif include_expired:
            rest.append(row)

    fresh = fresh[:new_cap]
    queue = fresh + live + rest
    if not queue:
        return
    take = max(budget, len(fresh))
    for row in queue[:take]:
        try:
            extra = rover.detail(cfg["source"]["detail_path"], row["id"])
        except PortalError as exc:
            log(f"    detail fetch failed for {row.get('sev')}: {exc}")
            continue
        for field in DETAIL_CARRY:
            if extra.get(field):
                row[field] = extra[field]
        row["detailed"] = True
    outstanding = max(0, len(queue) - take)
    tail = "" if not outstanding else \
        f" — {outstanding} still to read, rerun or use --backfill"
    log(f"  read {min(len(queue), take)} entry detail page(s){tail}")


def run(args: argparse.Namespace) -> int:
    started = now_iso()
    cfg = load_config()
    state = load_state()
    baseline = not state.get("entries")
    if args.rebuild:
        baseline = True
        # The report link map survives a rebuild: it is a cache of ~950
        # page fetches, not part of the diff baseline, and re-earning it
        # would take a fortnight of budgeted runs.
        state = {"version": STATE_VERSION, "entries": {},
                 "reports": state.get("reports", {}),
                 "recent_events": state.get("recent_events", [])}

    if args.backfill:
        cfg.setdefault("model_reports", {})["link_budget_per_run"] = 100000
        cfg.setdefault("details", {})["budget_per_run"] = 100000

    log(f"SEVs Register check — {melbourne_time(started)} (Melbourne)")
    rover = Rover(cfg)
    try:
        rows, warnings = scan(rover, cfg)
        reports, report_warnings = scan_reports(rover, cfg)
    except PortalError as exc:
        log(f"FAILED: {exc}")
        return 2
    warnings += report_warnings

    backfilled = link_reports(rover, cfg, reports, state) if reports else set()
    reports, reports_trusted, merge_warnings = reconcile_reports(state, reports, cfg)
    warnings += merge_warnings
    attach_reports(rows, reports)
    # Before the diff, so that a notification about a brand-new entry already
    # knows which variant it is about.
    backfill_details(rover, cfg, rows, state)
    unsettled = frozenset(sev for mre in backfilled
                          for sev in reports.get(mre, {}).get("sev", []))

    events, diff_warnings = diff(state, rows, cfg, report_events=reports_trusted,
                                 unsettled=unsettled)
    warnings += diff_warnings
    for warning in warnings:
        log(f"  warning: {warning}")

    with_report = sum(1 for r in rows.values() if r.get("has_report"))
    if baseline:
        log(f"  baseline run — recorded {len(rows)} entries "
            f"({with_report} with a model report), notifying nothing")
        events = []
    else:
        log(f"  {len(rows)} entries, {with_report} with a model report, "
            f"{len(events)} changes")

    notify_cfg = cfg.get("notify", {})
    watch = cfg.get("watch", {})
    watched_only = bool(watch.get("notify_watched_only"))
    to_send = bundle_events(
        [e for e in events
         if notify_cfg.get(e["type"], False)
         and (not watched_only or matches_watch(e["row"], watch))], cfg)
    cap = int(notify_cfg.get("max_per_run", 12))
    overflow = max(0, len(to_send) - cap)

    record_events(state, events)
    apply_events(state, rows, events, baseline=baseline)
    state["reports"] = reports
    sent = send_ntfy(to_send[:cap], args.dry_run, extra=overflow)
    if to_send and not args.dry_run and sent < len(to_send[:cap]):
        warnings.append(f"{len(to_send[:cap]) - sent} notification(s) could not be "
                        "delivered to ntfy — those alerts are lost, they do not "
                        "re-fire.")

    for event in events[:25]:
        log(f"    {event['type']:14} {event['row'].get('sev', ''):12} "
            f"{event['row']['title']} — {event['detail']}")
    if len(events) > 25:
        log(f"    ... and {len(events) - 25} more")

    write_dashboard_data(state, warnings, started, cfg)
    if not args.dry_run:
        save_state(state)
    log("done")
    return 0


def self_test() -> int:
    """Prove the portal is still shaped the way this scraper expects."""
    cfg = load_config()
    rover = Rover(cfg)
    problems: list[str] = []
    try:
        grid_url, layouts = rover.list_page(cfg["source"]["list_path"])
        log(f"  grid endpoint: {grid_url}")
        log(f"  views offered: {', '.join(sorted(layouts))}")
        for status, view in cfg["source"]["views"].items():
            if view not in layouts:
                problems.append(f"view '{view}' ({status}) is missing")
        rows, warnings = scan(rover, cfg)
        problems += warnings
        log(f"  parsed {len(rows)} entries")
        sample = next((r for r in rows.values() if r["status"] == "in_force"), None)
        if sample is None:
            problems.append("no in-force entries were parsed")
        else:
            log(f"  sample: {sample['sev']} {sample['title']} — {build_range(sample)}")
            if sample.get("id"):
                detail = rover.detail(cfg["source"]["detail_path"], sample["id"])
                missing = [f for f in ("criterion", "variant", "build_range")
                           if f not in detail]
                if missing:
                    problems.append("detail page no longer exposes: "
                                    + ", ".join(missing))
                else:
                    log(f"  detail page OK — criterion: {detail['criterion']}")

        if cfg.get("model_reports", {}).get("enabled", True):
            mcfg = cfg["model_reports"]
            _, mre_layouts = rover.list_page(mcfg["list_path"])
            for key, view in mcfg["views"].items():
                if view not in mre_layouts:
                    problems.append(f"model report view '{view}' ({key}) is "
                                    "missing; offered: "
                                    + ", ".join(sorted(mre_layouts)))
            reports, report_warnings = scan_reports(rover, cfg)
            problems += report_warnings
            log(f"  parsed {len(reports)} model reports")
            linkable = next((r for r in reports.values()
                             if r.get("subtype", "").lower()
                             == "specialist and enthusiast vehicles"), None)
            if linkable is None:
                problems.append("no SEV-subtype model reports were parsed")
            else:
                html = rover.detail_page(mcfg["detail_path"], linkable["id"])
                linked = parse_related_sevs(html)
                holder = parse_labels(html, REPORT_DETAIL_FIELDS).get("holder")
                if not linked:
                    problems.append(
                        f"{linkable['mre']} lists no SEV entry — the 'Based on' "
                        "link is the only thing tying reports to the register")
                else:
                    log(f"  {linkable['mre']} -> {', '.join(linked)} "
                        f"({holder or 'holder unknown'})")
    except PortalError as exc:
        problems.append(str(exc))

    if problems:
        log("SELF-TEST FAILED:")
        for problem in problems:
            log(f"  - {problem}")
        return 1
    log("SELF-TEST PASSED")
    return 0


def notify_test() -> int:
    fake = {
        "type": "new",
        "rows": [{"sev": "SEV-000000", "title": "Test Motors Example GT-R",
                  "make": "Test Motors", "model": "Example GT-R",
                  "category": "MA - Passenger Vehicle", "model_code": "TEST-1",
                  "build_from_iso": "1999-01-01", "build_to_iso": "2002-12-01",
                  "criterion": "Performance Criterion", "expiry": "01/01/2030",
                  "url": "https://www.rover.infrastructure.gov.au"
                         "/PublishedApprovals/SEVApprovals"}],
        "detail": "this is a test notification from the SEVs monitor",
    }
    return 0 if send_ntfy([fake], dry_run=False) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="check and report, but send nothing and save nothing")
    parser.add_argument("--self-test", action="store_true",
                        help="verify the ROVER portal is still parseable")
    parser.add_argument("--notify-test", action="store_true",
                        help="send one test notification and exit")
    parser.add_argument("--rebuild", action="store_true",
                        help="discard the baseline and rebuild it silently")
    parser.add_argument("--backfill", action="store_true",
                        help="link every outstanding model report to its SEV "
                             "entries in one run, ignoring the per-run budget "
                             "(slow: one page fetch per report)")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if args.notify_test:
        return notify_test()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
