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
    "returned":       ("SEV back in force",    "arrows_counterclockwise", 4),
    "expired":        ("SEV expired",          "hourglass_flowing_sand",  3),
    "removed":        ("SEV entry removed",    "wastebasket",             3),
    "under_review":   ("SEV under review",     "warning",                 3),
    "review_cleared": ("Review finished",      "white_check_mark",        2),
    "expiry_changed": ("SEV expiry changed",   "calendar",                2),
    "updated":        ("SEV details updated",  "pencil2",                 2),
}

EVENT_ORDER = ["new", "returned", "under_review", "removed", "expired",
               "review_cleared", "expiry_changed", "updated"]

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

    def detail(self, detail_path: str, record_id: str) -> dict:
        """Parse the extra fields only an entry's own page carries."""
        html = self.get_text(f"{detail_path}{record_id}")
        out: dict[str, str] = {}
        for element_id, field in DETAIL_FIELDS.items():
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


def diff(state: dict, rows: dict[str, dict], cfg: dict) -> tuple[list[dict], list[str]]:
    """Compare this run against the stored baseline.

    Returns (events, warnings). Every event carries the row it is about; the
    caller decides which of them become notifications.
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
        # Detail-page extras are fetched only for new entries; keep whatever we
        # learned earlier rather than dropping it on the next plain run.
        for field in ("criterion", "variant", "variant_details", "notes",
                      "build_range"):
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


def notification_body(event: dict) -> str:
    row = event["row"]
    bits = [f"{row['title']} ({row.get('sev', '')})".strip(), build_range(row)]
    line = " · ".join(p for p in (row.get("category"), row.get("model_code")) if p)
    if line:
        bits.append(line)
    if row.get("criterion"):
        bits.append(f"Criterion: {row['criterion']}")
    if row.get("variant") and row["variant"].upper() != "ALL":
        bits.append(f"Variant: {row['variant']}")
    if row.get("variant_details"):
        bits.append(row["variant_details"][:200])
    if row.get("expiry"):
        bits.append(f"Register entry expires {row['expiry']}")
    bits.append(event["detail"])
    return "\n".join(b for b in bits if b)


def send_ntfy(events: list[dict], dry_run: bool, extra: int = 0) -> int:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    server = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").strip().rstrip("/")
    token = os.environ.get("NTFY_TOKEN", "").strip()

    if not events:
        return 0
    if dry_run:
        for event in events:
            log(f"  [dry-run] would notify: {event['type']} — "
                f"{event['row'].get('sev')} {event['row']['title']}")
        return 0
    if not topic:
        log("  NTFY_TOPIC is not set — skipping notifications")
        return 0

    sent = 0
    for index, event in enumerate(events):
        row = event["row"]
        title, tag, priority = EVENT_META.get(event["type"],
                                              ("SEVs Register", "bell", 3))
        body = notification_body(event)
        if extra and index == len(events) - 1:
            body += f"\n\n(+{extra} more changes this run — see the dashboard)"
        headers = {k: header_safe(v) for k, v in {
            "Title": f"{title}: {row['title']}",
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
    log(f"  sent {sent}/{len(events)} notifications")
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
                    "variant_details", "notes", "first_seen")


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
        if (entry.get("first_seen") or "") >= new_since:
            row["is_new"] = True
        if (entry.get("status") == "in_force" and entry.get("expiry_iso")
                and today.isoformat() <= entry["expiry_iso"] <= soon):
            row["expiring_soon"] = True
        rows.append(row)

    rows.sort(key=lambda r: (r.get("first_seen") or "", r.get("sev") or ""),
              reverse=True)

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
        },
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

def enrich(rover: Rover, cfg: dict, events: list[dict]) -> None:
    """Pull criterion / variant / notes for the entries worth an extra request.

    Only entries the user will actually be told about get enriched — a detail
    page is ~450 KB, so fetching all 1000+ of them every run would be absurd.
    """
    enrich_cfg = cfg.get("enrich", {})
    if not enrich_cfg.get("enabled", True):
        return
    wanted = [e for e in events if e["type"] in ("new", "returned")]
    budget = int(enrich_cfg.get("max_per_run", 15))
    for event in wanted[:budget]:
        row = event["row"]
        if not row.get("id"):
            continue
        try:
            extra = rover.detail(cfg["source"]["detail_path"], row["id"])
        except PortalError as exc:
            log(f"    detail fetch failed for {row.get('sev')}: {exc}")
            continue
        for field in ("criterion", "variant", "variant_details", "notes",
                      "build_range"):
            if extra.get(field):
                row[field] = extra[field]
    if len(wanted) > budget:
        log(f"    enriched {budget} of {len(wanted)} new entries (budget)")


def run(args: argparse.Namespace) -> int:
    started = now_iso()
    cfg = load_config()
    state = load_state()
    baseline = not state.get("entries")
    if args.rebuild:
        baseline = True
        state = {"version": STATE_VERSION, "entries": {},
                 "recent_events": state.get("recent_events", [])}

    log(f"SEVs Register check — {melbourne_time(started)} (Melbourne)")
    rover = Rover(cfg)
    try:
        rows, warnings = scan(rover, cfg)
    except PortalError as exc:
        log(f"FAILED: {exc}")
        return 2

    events, diff_warnings = diff(state, rows, cfg)
    warnings += diff_warnings
    for warning in warnings:
        log(f"  warning: {warning}")

    if baseline:
        log(f"  baseline run — recorded {len(rows)} entries, notifying nothing")
        events = []
    else:
        log(f"  {len(rows)} entries, {len(events)} changes")
        enrich(rover, cfg, events)

    notify_cfg = cfg.get("notify", {})
    watch = cfg.get("watch", {})
    watched_only = bool(watch.get("notify_watched_only"))
    to_send = [e for e in events
               if notify_cfg.get(e["type"], False)
               and (not watched_only or matches_watch(e["row"], watch))]
    cap = int(notify_cfg.get("max_per_run", 12))
    overflow = max(0, len(to_send) - cap)

    record_events(state, events)
    apply_events(state, rows, events, baseline=baseline)
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
        "row": {"sev": "SEV-000000", "title": "Test Motors Example GT-R",
                "make": "Test Motors", "model": "Example GT-R",
                "category": "MA - Passenger Vehicle", "model_code": "TEST-1",
                "build_from_iso": "1999-01-01", "build_to_iso": "2002-12-01",
                "criterion": "Performance Criterion", "expiry": "01/01/2030",
                "url": "https://www.rover.infrastructure.gov.au"
                       "/PublishedApprovals/SEVApprovals"},
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
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if args.notify_test:
        return notify_test()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
