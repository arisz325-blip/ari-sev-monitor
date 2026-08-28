"""End-to-end tests for the SEVs monitor, run against a local mock portal.

    python -m tests.test_checker

Every test runs the real checker.py as a subprocess against mock_rover, so the
scraping, diffing, notification and dashboard paths are all exercised together.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests import mock_rover  # noqa: E402
from checker import (  # noqa: E402
    build_range, header_safe, matches_watch, month_year, parse_au_date,
    parse_related_sevs, preferred_spelling, title_of,
)

ROOT = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}{f' — {detail}' if detail and not condition else ''}")
    if not condition:
        FAILURES.append(label)


def entry(make: str, model: str, **kw) -> dict:
    row = {
        "make": make, "model": model,
        "category": kw.pop("category", "MA - Passenger Vehicle"),
        "model_code": kw.pop("model_code", "TEST-1"),
        "build_from": kw.pop("build_from", "1/01/2000"),
        "build_to": kw.pop("build_to", "1/12/2004"),
        "expiry": kw.pop("expiry", "01/01/2030"),
        "under_review": kw.pop("under_review", False),
        "status": kw.pop("status", "in_force"),
    }
    row.update(kw)
    return row


BASE_REGISTER = {
    "SEV-000100": entry("NISSAN", "Skyline GT-R", model_code="BNR32"),
    "SEV-000101": entry("Toyota", "Chaser", model_code="JZX100"),
    "SEV-000102": entry("Honda", "Beat", model_code="PP1"),
    "SEV-000103": entry("Mazda", "RX-7", model_code="FD3S"),
    "SEV-000104": entry("Subaru", "Impreza WRX STI", model_code="GC8"),
    "SEV-000105": entry("MITSUBISHI", "Lancer Evolution VI", model_code="CP9A"),
    "SEV-000106": entry("Suzuki", "Cappuccino", model_code="EA11R"),
    "SEV-000090": entry("LEXUS", "LFA", model_code="LFA10", status="expired",
                        expiry="01/01/2024"),
    "SEV-000091": entry("PORSCHE", "959", model_code="959", status="expired",
                        expiry="01/06/2023"),
}


BASE_REPORTS = {
    "MRE-000001": {"make": "NISSAN", "model": "Skyline GT-R",
                   "holder": "SYDNEY RAW PTY LTD", "sev": ["SEV-000100"]},
    "MRE-000002": {"make": "Toyota", "model": "Chaser",
                   "holder": "MELBOURNE RAW PTY LTD",
                   "sev": ["SEV-000101", "SEV-000102"]},
    # Not written against a SEV entry at all — must never be fetched.
    "MRE-000003": {"make": "Suzuki", "model": "GSX-R",
                   "subtype": "Used 2 or 3 Wheeled Vehicle", "sev": []},
}


def workdir(tmp: Path, base_url: str, **overrides) -> Path:
    """A throwaway copy of the project, pointed at the mock portal."""
    work = tmp / f"run{len(list(tmp.iterdir()))}"
    work.mkdir()
    shutil.copy(ROOT / "checker.py", work / "checker.py")
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    cfg["source"]["base"] = base_url
    cfg["http"]["request_delay_seconds"] = 0
    cfg["http"]["timeout_seconds"] = 20
    cfg["sanity"]["min_expected_entries"] = 5
    for key, value in overrides.items():
        cfg[key].update(value) if isinstance(value, dict) else cfg.update({key: value})
    (work / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return work


def run_checker(work: Path, base_url: str, extra: list[str] | None = None,
                topic: str = "sev-test") -> subprocess.CompletedProcess:
    env = dict(os.environ, ROVER_BASE=base_url, NTFY_SERVER=base_url,
               NTFY_TOPIC=topic, PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        [sys.executable, str(work / "checker.py"), *(extra or [])],
        cwd=work, env=env, capture_output=True, text=True, timeout=180,
        encoding="utf-8", errors="replace")
    if result.returncode not in (0, 1):
        print(result.stdout)
        print(result.stderr)
    return result


def state_of(work: Path) -> dict:
    return json.loads((work / "state.json").read_text(encoding="utf-8"))


def data_of(work: Path) -> dict:
    return json.loads((work / "docs" / "data.json").read_text(encoding="utf-8"))


def titles_pushed() -> list[str]:
    return [p["headers"].get("Title", "") for p in mock_rover.NTFY_POSTS]


# --------------------------------------------------------------------------

def unit_checks() -> None:
    print("\nUnit checks")
    check("parse_au_date reads d/m/yyyy, not m/d/yyyy",
          parse_au_date("1/04/2009") == "2009-04-01", parse_au_date("1/04/2009"))
    check("parse_au_date rejects junk", parse_au_date("soon") is None)
    check("parse_au_date tolerates an empty cell", parse_au_date(None) is None)
    check("month_year formats for display", month_year("2009-04-01") == "04/2009")
    check("build_range says so when there is no end date",
          build_range({"build_from_iso": "2025-09-01"}) == "09/2025 - no end date",
          build_range({"build_from_iso": "2025-09-01"}))
    check("build_range shows both ends",
          build_range({"build_from_iso": "2004-01-01",
                       "build_to_iso": "2007-12-01"}) == "01/2004 - 12/2007")
    check("build_range falls back to ROVER's own text when the parse failed",
          build_range({"build_from": "sometime"}) == "sometime - no end date")
    check("header_safe strips characters that would kill the ntfy request",
          header_safe("Mitsuoka Le–Seyde ‘Kei’") == "Mitsuoka Le-Seyde 'Kei'",
          header_safe("Mitsuoka Le–Seyde ‘Kei’"))
    check("header_safe survives an emoji", "?" in header_safe("Skyline \U0001f697"))
    check("title_of joins make and model",
          title_of({"make": "Honda", "model": "N-ONE e"}) == "Honda N-ONE e")
    check("title_of falls back to the SEV number",
          title_of({"sev": "SEV-000001"}) == "SEV-000001")

    check("the nicest spelling of a make wins ties",
          preferred_spelling({"NISSAN": 1, "Nissan": 1, "nissan": 1}) == "Nissan")
    check("but the most-used spelling wins outright",
          preferred_spelling({"NISSAN": 9, "Nissan": 1}) == "NISSAN")
    check("the 'Based on' table is what links a report to its entries",
          parse_related_sevs(
              '<td><a href="/PublishedApprovals/SEVDetails/?id=abc" '
              'target="_blank">SEV-000643</a></td>'
              '<td><a href="/PublishedApprovals/SEVDetails/?id=def">'
              "SEV-000644</a></td>") == ["SEV-000643", "SEV-000644"])
    check("a report based on nothing links to nothing",
          parse_related_sevs("<html><body>no table here</body></html>") == [])

    row = {"make": "NISSAN", "model": "Skyline GT-R", "model_code": "BNR32",
           "category": "MA - Passenger Vehicle"}
    check("no watch filters means everything matches", matches_watch(row, {}))
    check("watch matches on make", matches_watch(row, {"makes": ["nissan"]}))
    check("watch matches on a model-code keyword",
          matches_watch(row, {"keywords": ["bnr32"]}))
    check("watch rejects what it does not cover",
          not matches_watch(row, {"makes": ["toyota"]}))
    check("watch matches on category",
          matches_watch(row, {"categories": ["lc - motor cycle",
                                             "ma - passenger vehicle"]}))


def main() -> int:
    server, base = mock_rover.start()
    tmp = Path(tempfile.mkdtemp(prefix="sev-tests-"))
    unit_checks()

    try:
        # -- baseline ------------------------------------------------------
        print("\nBaseline run")
        mock_rover.set_register(BASE_REGISTER)
        mock_rover.reset_ntfy()
        work = workdir(tmp, base)
        result = run_checker(work, base)
        state = state_of(work)
        data = data_of(work)
        check("baseline run exits clean", result.returncode == 0, result.stderr[-400:])
        check("baseline reads both views", len(state["entries"]) == 9,
              str(len(state["entries"])))
        check("baseline notifies nothing", not mock_rover.NTFY_POSTS,
              str(titles_pushed()))
        check("baseline sends the anti-forgery token", not mock_rover.REJECTED)
        check("in-force and expired are counted separately",
              (data["counts"]["in_force"], data["counts"]["expired"]) == (7, 2),
              str(data["counts"]))
        check("baseline entries are not marked new",
              data["counts"]["new_recent"] == 0, str(data["counts"]))
        check("the alias prefix on related columns is stripped",
              state["entries"]["SEV-000100"]["model"] == "Skyline GT-R")
        check("dates are stored as ISO",
              state["entries"]["SEV-000100"]["build_from_iso"] == "2000-01-01")
        check("no per-entry timestamp churns the state file",
              "last_seen" not in state["entries"]["SEV-000100"])

        # -- a new approval ------------------------------------------------
        print("\nA model becomes importable")
        register = dict(BASE_REGISTER)
        register["SEV-000107"] = entry(
            "Honda", "N-ONE e", model_code="ZAA-JG5", build_from="1/09/2025",
            build_to=None, criterion="Environmental Criterion",
            variant_details="MUST be JG5 Model Code BEV ONLY.")
        mock_rover.set_register(register)
        mock_rover.reset_ntfy()
        result = run_checker(work, base)
        data = data_of(work)
        push = mock_rover.NTFY_POSTS[0] if mock_rover.NTFY_POSTS else {"headers": {}, "body": ""}
        check("exactly one notification is sent", len(mock_rover.NTFY_POSTS) == 1,
              str(titles_pushed()))
        check("the notification names the car",
              push["headers"].get("Title") == "New SEV approval: Honda N-ONE e",
              push["headers"].get("Title", ""))
        check("the body carries the SEV number", "SEV-000107" in push["body"],
              push["body"])
        check("the body carries the build date range",
              "09/2025 - no end date" in push["body"], push["body"])
        check("the criterion is pulled from the detail page",
              "Environmental Criterion" in push["body"], push["body"])
        check("the variant condition is included",
              "JG5 Model Code BEV ONLY" in push["body"], push["body"])
        check("tapping the notification opens the ROVER entry",
              push["headers"].get("Click", "").endswith("id=id-SEV-000107"),
              push["headers"].get("Click", ""))
        check("the dashboard counts it as new", data["counts"]["new_recent"] == 1,
              str(data["counts"]))
        check("the newest entry sorts first",
              data["entries"][0]["sev"] == "SEV-000107",
              data["entries"][0]["sev"])
        check("the dashboard flags it new", data["entries"][0].get("is_new") is True)
        check("the activity feed records it",
              data["recent_events"][0]["type"] == "new"
              and data["recent_events"][0]["sev"] == "SEV-000107",
              str(data["recent_events"][:1]))
        check("a second run with nothing new stays silent",
              (mock_rover.reset_ntfy() or run_checker(work, base).returncode == 0)
              and not mock_rover.NTFY_POSTS, str(titles_pushed()))

        # -- lifecycle changes ---------------------------------------------
        print("\nLifecycle changes")
        register["SEV-000101"]["status"] = "expired"
        register["SEV-000102"]["under_review"] = True
        register["SEV-000103"]["expiry"] = "01/01/2031"
        mock_rover.set_register(register)
        mock_rover.reset_ntfy()
        run_checker(work, base)
        data = data_of(work)
        events = {(e["type"], e["sev"]) for e in data["recent_events"]}
        check("expiry into the expired view is detected",
              ("expired", "SEV-000101") in events, str(sorted(events)))
        check("an expiry is not pushed by default",
              all("expired" not in t for t in titles_pushed()), str(titles_pushed()))
        check("a review flag is detected",
              ("under_review", "SEV-000102") in events, str(sorted(events)))
        check("a review flag is pushed",
              any(t.startswith("SEV under review") for t in titles_pushed()),
              str(titles_pushed()))
        check("an expiry date change is detected",
              ("expiry_changed", "SEV-000103") in events, str(sorted(events)))
        check("an expiry date change is not pushed by default",
              not any("expiry changed" in t for t in titles_pushed()),
              str(titles_pushed()))
        check("the expired entry moved between the dashboard counts",
              (data["counts"]["in_force"], data["counts"]["expired"]) == (7, 3),
              str(data["counts"]))

        register["SEV-000101"]["status"] = "in_force"
        mock_rover.set_register(register)
        mock_rover.reset_ntfy()
        run_checker(work, base)
        check("an entry coming back in force is pushed",
              any(t.startswith("SEV back in force") for t in titles_pushed()),
              str(titles_pushed()))

        # -- removal, and the guard against a portal glitch -----------------
        print("\nRemovals")
        shrunk = {k: v for k, v in register.items() if k != "SEV-000106"}
        mock_rover.set_register(shrunk)
        mock_rover.reset_ntfy()
        run_checker(work, base)
        data = data_of(work)
        check("a single disappearance is reported as a removal",
              any(t.startswith("SEV entry removed") for t in titles_pushed()),
              str(titles_pushed()))
        check("the removed entry is kept on the dashboard, marked gone",
              any(e["sev"] == "SEV-000106" and e["status"] == "gone"
                  for e in data["entries"]))

        half = dict(list(shrunk.items())[:4])
        mock_rover.set_register(half)
        mock_rover.reset_ntfy()
        run_checker(work, base)
        data = data_of(work)
        check("a register that halves does not fire a wave of removals",
              not any(t.startswith("SEV entry removed") for t in titles_pushed()),
              str(titles_pushed()))
        check("the shrink is surfaced as a warning instead",
              any("smaller" in w for w in data["warnings"]), str(data["warnings"]))
        check("entries survive the glitch in state",
              len(state_of(work)["entries"]) == 10,
              str(len(state_of(work)["entries"])))

        # -- model reports --------------------------------------------------
        print("\nModel reports")
        mock_rover.set_register(BASE_REGISTER)
        mock_rover.set_reports({})
        reports_dir = workdir(tmp, base)
        mock_rover.reset_ntfy()
        run_checker(reports_dir, base)                 # baseline, no reports
        check("an entry with no model report is counted as such",
              data_of(reports_dir)["counts"]["with_report"] == 0,
              str(data_of(reports_dir)["counts"]))

        mock_rover.set_reports(BASE_REPORTS)
        mock_rover.reset_ntfy()
        run_checker(reports_dir, base)
        data = data_of(reports_dir)
        by_sev = {e["sev"]: e for e in data["entries"]}
        check("a model report is linked to its SEV entry through the "
              "'Based on' table",
              by_sev["SEV-000100"].get("has_report") is True,
              str(by_sev["SEV-000100"].get("reports")))
        check("one report can cover several SEV entries",
              by_sev["SEV-000101"].get("has_report") is True
              and by_sev["SEV-000102"].get("has_report") is True)
        check("the workshop holding the report is carried through",
              by_sev["SEV-000100"]["reports"][0]["holder"] == "SYDNEY RAW PTY LTD",
              str(by_sev["SEV-000100"]["reports"]))
        check("entries without a report stay flagged",
              by_sev["SEV-000104"].get("has_report") in (None, False))
        check("the dashboard counts entries with a report",
              data["counts"]["with_report"] == 3, str(data["counts"]))
        check("a non-SEV model report is never fetched",
              "MRE-000003" not in mock_rover.DETAIL_HITS,
              str(mock_rover.DETAIL_HITS))
        check("backfilling links does not push anything",
              not titles_pushed(), str(titles_pushed()))

        mock_rover.reset_ntfy()
        run_checker(reports_dir, base)
        check("a linked report is not fetched again on later runs",
              not [h for h in mock_rover.DETAIL_HITS if h.startswith("MRE")],
              str(mock_rover.DETAIL_HITS))

        with_new = dict(BASE_REPORTS)
        with_new["MRE-000004"] = {"make": "Mazda", "model": "RX-7",
                                  "holder": "PERTH RAW PTY LTD",
                                  "sev": ["SEV-000103"]}
        mock_rover.set_reports(with_new)
        mock_rover.reset_ntfy()
        run_checker(reports_dir, base)
        check("a model report appearing later IS pushed",
              titles_pushed() == ["Model report approved: Mazda RX-7"],
              str(titles_pushed()))
        check("the push names the workshop that can build it",
              "PERTH RAW PTY LTD" in mock_rover.NTFY_POSTS[0]["body"],
              mock_rover.NTFY_POSTS[0]["body"])

        suspended = {k: dict(v) for k, v in with_new.items()}
        suspended["MRE-000004"]["status"] = "Suspended"
        mock_rover.set_reports(suspended)
        mock_rover.reset_ntfy()
        run_checker(reports_dir, base)
        check("a suspended report means the car can no longer be built",
              titles_pushed() == ["Model report gone: Mazda RX-7"],
              str(titles_pushed()))
        check("a suspended report is still listed, marked not in force",
              {e["sev"]: e for e in data_of(reports_dir)["entries"]}
              ["SEV-000103"]["reports"][0]["status"] == "Suspended")

        mock_rover.set_reports({"MRE-000001": BASE_REPORTS["MRE-000001"]})
        mock_rover.reset_ntfy()
        run_checker(reports_dir, base)
        check("a collapsed model report register is not believed",
              not titles_pushed(), str(titles_pushed()))
        check("the collapse is surfaced as a warning",
              any("model report" in w for w in data_of(reports_dir)["warnings"]),
              str(data_of(reports_dir)["warnings"]))

        mock_rover.set_reports(BASE_REPORTS)
        mock_rover.reset_ntfy()
        run_checker(reports_dir, base, ["--rebuild"])
        check("--rebuild keeps the model report links it paid for",
              not [h for h in mock_rover.DETAIL_HITS if h.startswith("MRE")]
              and data_of(reports_dir)["counts"]["with_report"] == 3,
              str(mock_rover.DETAIL_HITS))

        # -- the brand picker ------------------------------------------------
        print("\nBrand picker")
        mixed = dict(BASE_REGISTER)
        mixed["SEV-000200"] = entry("Nissan", "Silvia", model_code="S15")
        mixed["SEV-000201"] = entry("nissan", "180SX", model_code="RPS13")
        mock_rover.set_register(mixed)
        mock_rover.set_reports(BASE_REPORTS)
        brands = workdir(tmp, base)
        run_checker(brands, base)
        makes = {m["key"]: m for m in data_of(brands)["makes"]}
        check("makes are grouped case-insensitively",
              makes["NISSAN"]["count"] == 3, str(makes.get("NISSAN")))
        check("the picker shows the register's most common spelling",
              makes["NISSAN"]["label"] == "Nissan", str(makes.get("NISSAN")))
        check("each make carries its own model report count",
              makes["NISSAN"]["with_report"] == 1, str(makes.get("NISSAN")))
        check("expired entries stay out of the picker",
              "LEXUS" not in makes, str(sorted(makes)))
        check("makes are ordered by how many entries they have",
              [m["key"] for m in data_of(brands)["makes"]][0] == "NISSAN",
              str([m["key"] for m in data_of(brands)["makes"]][:3]))

        # -- notification budget -------------------------------------------
        print("\nNotification budget")
        mock_rover.set_register(BASE_REGISTER)
        capped = workdir(tmp, base, notify={"max_per_run": 2})
        mock_rover.reset_ntfy()
        run_checker(capped, base)                      # baseline, silent
        flood = dict(BASE_REGISTER)
        for n in range(5):
            flood[f"SEV-0002{n:02d}"] = entry("Toyota", f"Test {n}")
        mock_rover.set_register(flood)
        mock_rover.reset_ntfy()
        run_checker(capped, base)
        check("the per-run cap holds", len(mock_rover.NTFY_POSTS) == 2,
              str(len(mock_rover.NTFY_POSTS)))
        check("the last notification says how many were held back",
              "+3 more" in mock_rover.NTFY_POSTS[-1]["body"],
              mock_rover.NTFY_POSTS[-1]["body"][-80:])
        check("every change still reaches the dashboard",
              len([e for e in data_of(capped)["recent_events"]
                   if e["type"] == "new"]) == 5,
              str(len(data_of(capped)["recent_events"])))

        # -- watch filter ---------------------------------------------------
        print("\nWatch filter")
        watched = workdir(tmp, base,
                          watch={"notify_watched_only": True, "makes": ["nissan"]})
        mock_rover.set_register(BASE_REGISTER)
        mock_rover.reset_ntfy()
        run_checker(watched, base)                     # baseline, silent
        picky = dict(BASE_REGISTER)
        picky["SEV-000300"] = entry("NISSAN", "Silvia", model_code="S15")
        picky["SEV-000301"] = entry("Toyota", "Soarer", model_code="JZZ30")
        mock_rover.set_register(picky)
        mock_rover.reset_ntfy()
        run_checker(watched, base)
        check("only the watched make is pushed",
              titles_pushed() == ["New SEV approval: NISSAN Silvia"],
              str(titles_pushed()))
        check("the unwatched one is still on the dashboard",
              any(e["sev"] == "SEV-000301" for e in data_of(watched)["entries"]))

        # -- paging ----------------------------------------------------------
        print("\nPaging and self-test")
        paged = workdir(tmp, base, source={"page_size": 3})
        mock_rover.set_register(BASE_REGISTER)
        mock_rover.reset_ntfy()
        run_checker(paged, base)
        check("a register larger than one page is read in full",
              len(state_of(paged)["entries"]) == 9,
              str(len(state_of(paged)["entries"])))

        selftest = run_checker(paged, base, ["--self-test"])
        check("--self-test passes against a healthy portal",
              selftest.returncode == 0 and "SELF-TEST PASSED" in selftest.stdout,
              selftest.stdout[-300:])

        broken = workdir(tmp, base, source={"views": {
            "in_force": "Portal View - SEV Entries In Force",
            "expired": "Portal View - Renamed By The Department"}})
        broken_result = run_checker(broken, base, ["--self-test"])
        check("--self-test fails loudly when a view is renamed",
              broken_result.returncode == 1
              and "Renamed By The Department" in broken_result.stdout,
              broken_result.stdout[-300:])

        # -- dry run ---------------------------------------------------------
        print("\nDry run")
        mock_rover.set_register(BASE_REGISTER)
        dry = workdir(tmp, base)
        run_checker(dry, base)                          # baseline
        before = (dry / "state.json").read_text(encoding="utf-8")
        dry_register = dict(BASE_REGISTER)
        dry_register["SEV-000400"] = entry("Honda", "S2000", model_code="AP2")
        mock_rover.set_register(dry_register)
        mock_rover.reset_ntfy()
        dry_result = run_checker(dry, base, ["--dry-run"])
        check("--dry-run sends nothing", not mock_rover.NTFY_POSTS,
              str(titles_pushed()))
        check("--dry-run leaves the baseline untouched",
              (dry / "state.json").read_text(encoding="utf-8") == before)
        check("--dry-run still reports what it would have sent",
              "would notify" in dry_result.stdout, dry_result.stdout[-200:])

    finally:
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
