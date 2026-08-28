"""A stand-in for the ROVER portal (and for ntfy.sh), for offline testing.

Serves the shapes checker.py depends on:

  GET  /PublishedApprovals/SEVApprovals            -> page carrying the
                                                      entity-grid config
                                                      (data-get-url + base64
                                                      data-view-layouts)
  GET  /PublishedApprovals/MREApprovals            -> same, for model reports
  GET  /_layout/tokenhtml                          -> the anti-forgery token
  POST /_services/entity-grid-data.json/<id>       -> paged, sorted rows for
                                                      whichever view was asked
  GET  /PublishedApprovals/SEVDetails/?id=..       -> the per-entry detail page
  GET  /PublishedApprovals/ModelReportDetails/?id= -> the model report page,
                                                      including its "Based on"
                                                      links to SEV entries

Anything else POSTed is treated as an ntfy publish and recorded in NTFY_POSTS,
so tests can assert on what would have hit the phone.

Related columns are deliberately served under an alias prefix
("a_TESTALIAS.rvr_model"), the way the real grid does — that is what
checker.FIELD_MAP's alias stripping exists for.
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ALIAS = "a_TESTALIAS"
GRID_PATH = "/_services/entity-grid-data.json/test-grid"
TOKEN = "test-token-123"

VIEWS = {
    "in_force": "Portal View - SEV Entries In Force",
    "expired": "Portal View - SEV Entries Expired",
}
REPORT_VIEW = "Portal View - All Approvals: MRE"

# sev number -> entry. Tests mutate this between runs.
REGISTER: dict[str, dict] = {}
# MRE number -> model report. Tests mutate this between runs.
REPORTS: dict[str, dict] = {}
# Detail pages fetched this run, so tests can assert on the link budget.
DETAIL_HITS: list[str] = []
# Every ntfy publish this server received: {"topic", "headers", "body"}.
NTFY_POSTS: list[dict] = []
# Requests that arrived without the anti-forgery token.
REJECTED: list[str] = []


def set_register(entries: dict[str, dict]) -> None:
    REGISTER.clear()
    REGISTER.update({sev: dict(entry) for sev, entry in entries.items()})


def set_reports(reports: dict[str, dict]) -> None:
    REPORTS.clear()
    REPORTS.update({mre: dict(report) for mre, report in reports.items()})


def reset_ntfy() -> None:
    NTFY_POSTS.clear()
    REJECTED.clear()
    DETAIL_HITS.clear()


def _layouts(views: list[tuple[str, str]]) -> str:
    # The real Base64SecureConfiguration is encrypted; here it just names the
    # view back to us so the grid handler knows what was asked for.
    return base64.b64encode(json.dumps([
        {"ViewName": name, "Base64SecureConfiguration": f"SECURE-{marker}",
         "Configuration": {"EntityName": "rvr_approval", "PageSize": 10,
                           "ViewDisplayName": name}}
        for name, marker in views]).encode()).decode()


def _list_page(title: str, views: list[tuple[str, str]]) -> str:
    return (
        f"<!doctype html><html><body><h1>{title}</h1>"
        f'<div class="entity-grid entitylist" data-get-url="{GRID_PATH}" '
        f"data-view-layouts='{_layouts(views)}' data-selected-view=\"test\">"
        "</div></body></html>"
    )


def _attr(name: str, value) -> dict:
    return {"Name": name, "Type": "System.String", "Value": value,
            "FormattedValue": value, "DisplayValue": value,
            "AttributeMetadata": {"padding": "x" * 50}}


def _record(sev: str, entry: dict) -> dict:
    return {
        "Id": entry.get("id", f"id-{sev}"),
        "EntityName": "rvr_approval",
        "Attributes": [
            _attr("rvr_approvalnumber", sev),
            _attr(f"{ALIAS}.rvr_manufacturer", entry.get("make")),
            _attr(f"{ALIAS}.rvr_model", entry.get("model")),
            _attr(f"{ALIAS}.rvr_categorytype", entry.get("category")),
            _attr(f"{ALIAS}.rvr_modelcode", entry.get("model_code")),
            _attr(f"{ALIAS}.rvr_builddatefrom", entry.get("build_from")),
            _attr(f"{ALIAS}.rvr_builddateto", entry.get("build_to")),
            _attr("rvr_approvalexpirydate", entry.get("expiry")),
            _attr("rvr_underreview", "Yes" if entry.get("under_review") else "No"),
            _attr("statuscode", "Active"),
        ],
    }


def _report_record(mre: str, report: dict) -> dict:
    return {
        "Id": report.get("id", f"id-{mre}"),
        "EntityName": "rvr_approval",
        "Attributes": [
            _attr("rvr_approvalnumber", mre),
            _attr(f"{ALIAS}.rvr_manufacturer", report.get("make")),
            _attr(f"{ALIAS}.rvr_model", report.get("model")),
            _attr("rvr_approvalstatus", report.get("status", "In Force")),
            _attr("rvr_approvalsubtypeid",
                  report.get("subtype", "Specialist and Enthusiast Vehicles")),
            _attr(f"{ALIAS}.rvr_categorytype", report.get("category", "MA")),
            _attr(f"{ALIAS}.rvr_levelofcompliance",
                  report.get("compliance", "Complies")),
            _attr(f"{ALIAS}.rvr_mrebuilddaterange",
                  report.get("build_range", "1/2000 - 12/2004")),
        ],
    }


def _report_detail_page(mre: str, report: dict) -> str:
    fields = [
        ("rvr_approvalnumber", mre),
        ("rvr_publishedapprovalholder", report.get("holder", "TEST RAW PTY LTD")),
        ("rvr_publishedwebsite", report.get("website", "www.example.com")),
        ("rvr_approvalstatus", report.get("status", "In Force")),
        # The real page also publishes a contact name, phone and email; the
        # checker deliberately does not read them, so they are here to prove it.
        ("rvr_publishedcontact", "A PERSON"),
        ("rvr_publishedphone", "0400000000"),
    ]
    blocks = "".join(
        f'<div class="question-group"><div class="question-body">'
        f'<div id="{fid}" class="question-label">{value}</div>'
        "</div></div>" for fid, value in fields)
    links = "".join(
        f'<tr role="row"><td><a href="/PublishedApprovals/SEVDetails/'
        f'?id=id-{sev}" target="_blank">{sev}</a></td></tr>'
        for sev in report.get("sev", []))
    based_on = (
        '<div class="qtdiv" role="heading">Based on</div>'
        '<div id="related_approval_section" class="row datagrid-table">'
        f'<table id="RelatedApprovalList"><tbody>{links}</tbody></table></div>')
    return f"<!doctype html><html><body>{blocks}{based_on}</body></html>"


def _detail_page(sev: str, entry: dict) -> str:
    fields = [
        ("SEVApprovalNo", sev),
        ("SEVMake", entry.get("make", "")),
        ("SEVModel", entry.get("model", "")),
        ("SEVCategory", entry.get("category", "")),
        ("SEVBDR", entry.get("build_range", "01/2000 - No end date")),
        ("SEVVariant", entry.get("variant", "ALL")),
        ("SEVVariantDetails", entry.get("variant_details", "")),
        ("SEVCriterion", entry.get("criterion", "Rarity Criterion")),
        ("SEVExpiry", entry.get("expiry", "")),
        ("SEVModelCodeName", entry.get("model_code", "")),
        ("SEVNotes", entry.get("notes", "")),
    ]
    blocks = "".join(
        '<div class="question-group"><table><tbody><tr>'
        f'<td class="question-text-inline"><div class="qtdiv">{fid}</div></td>'
        f'<td><div class="question-body"><div id="{fid}" class="question-label">'
        f"{value}</div></div></td></tr></tbody></table></div>"
        for fid, value in fields)
    return f"<!doctype html><html><body>{blocks}</body></html>"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # keep the test output readable
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/PublishedApprovals/SEVApprovals":
            page = _list_page("Specialist and Enthusiast Vehicles",
                              [(VIEWS["in_force"], "sev-in_force"),
                               (VIEWS["expired"], "sev-expired")])
            return self._send(200, page.encode(), "text/html; charset=utf-8")
        if path == "/PublishedApprovals/MREApprovals":
            page = _list_page("Model Reports", [(REPORT_VIEW, "mre-all")])
            return self._send(200, page.encode(), "text/html; charset=utf-8")
        if path == "/_layout/tokenhtml":
            body = (f'<input name="__RequestVerificationToken" type="hidden" '
                    f'value="{TOKEN}" />')
            return self._send(200, body.encode(), "text/html; charset=utf-8")
        if path == "/PublishedApprovals/SEVDetails/":
            record_id = self.path.split("id=")[-1]
            for sev, entry in REGISTER.items():
                if entry.get("id", f"id-{sev}") == record_id:
                    DETAIL_HITS.append(sev)
                    return self._send(200, _detail_page(sev, entry).encode(),
                                      "text/html; charset=utf-8")
            return self._send(404, b"no such entry", "text/plain")
        if path == "/PublishedApprovals/ModelReportDetails/":
            record_id = self.path.split("id=")[-1]
            for mre, report in REPORTS.items():
                if report.get("id", f"id-{mre}") == record_id:
                    DETAIL_HITS.append(mre)
                    return self._send(200, _report_detail_page(mre, report).encode(),
                                      "text/html; charset=utf-8")
            return self._send(404, b"no such report", "text/plain")
        return self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        if self.path.split("?")[0] == GRID_PATH:
            if self.headers.get("__RequestVerificationToken") != TOKEN:
                REJECTED.append(self.path)
                return self._send(403, b"missing token", "text/plain")
            request = json.loads(raw or b"{}")
            marker = request.get("base64SecureConfiguration", "").replace("SECURE-", "")
            if marker.startswith("mre-"):
                rows = sorted(REPORTS.items())
                to_record = _report_record
            else:
                status = marker.replace("sev-", "")
                rows = sorted((sev, entry) for sev, entry in REGISTER.items()
                              if entry.get("status", "in_force") == status)
                to_record = _record
            if "DESC" in request.get("sortExpression", "").upper():
                rows.reverse()
            size = max(1, int(request.get("pageSize", 10)))
            page = max(1, int(request.get("page", 1)))
            window = rows[(page - 1) * size: page * size]
            body = json.dumps({
                "Records": [to_record(key, value) for key, value in window],
                "MoreRecords": page * size < len(rows),
                "ItemCount": len(rows),
                "PageNumber": page,
                "PageSize": size,
            }).encode()
            return self._send(200, body, "application/json")

        # Anything else is a notification publish.
        NTFY_POSTS.append({
            "topic": self.path.lstrip("/"),
            "headers": {k: v for k, v in self.headers.items()},
            "body": raw.decode("utf-8", "replace"),
        })
        return self._send(200, b'{"id":"test"}', "application/json")


def start(port: int = 0) -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"
