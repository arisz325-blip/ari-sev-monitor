"""A stand-in for the ROVER portal (and for ntfy.sh), for offline testing.

Serves the four shapes checker.py depends on:

  GET  /PublishedApprovals/SEVApprovals        -> page carrying the entity-grid
                                                  config (data-get-url +
                                                  base64 data-view-layouts)
  GET  /_layout/tokenhtml                      -> the anti-forgery token
  POST /_services/entity-grid-data.json/<id>   -> paged, sorted register rows
  GET  /PublishedApprovals/SEVDetails/?id=..   -> the per-entry detail page

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

# sev number -> entry. Tests mutate this between runs.
REGISTER: dict[str, dict] = {}
# Every ntfy publish this server received: {"topic", "headers", "body"}.
NTFY_POSTS: list[dict] = []
# Requests that arrived without the anti-forgery token.
REJECTED: list[str] = []


def set_register(entries: dict[str, dict]) -> None:
    REGISTER.clear()
    REGISTER.update({sev: dict(entry) for sev, entry in entries.items()})


def reset_ntfy() -> None:
    NTFY_POSTS.clear()
    REJECTED.clear()


def _view_layouts() -> str:
    layouts = [
        {
            "ViewName": VIEWS["in_force"],
            # The real blob is encrypted; here it just names the view back to us.
            "Base64SecureConfiguration": "SECURE-in_force",
            "Configuration": {"EntityName": "rvr_approval", "PageSize": 10,
                              "ViewDisplayName": "In Force Entries"},
        },
        {
            "ViewName": VIEWS["expired"],
            "Base64SecureConfiguration": "SECURE-expired",
            "Configuration": {"EntityName": "rvr_approval", "PageSize": 10,
                              "ViewDisplayName": "Expired Entries"},
        },
    ]
    return base64.b64encode(json.dumps(layouts).encode()).decode()


def _list_page() -> str:
    return (
        "<!doctype html><html><body><h1>Specialist and Enthusiast Vehicles</h1>"
        f'<div class="entity-grid entitylist" data-get-url="{GRID_PATH}" '
        f"data-view-layouts='{_view_layouts()}' data-selected-view=\"test\">"
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
            return self._send(200, _list_page().encode(), "text/html; charset=utf-8")
        if path == "/_layout/tokenhtml":
            body = (f'<input name="__RequestVerificationToken" type="hidden" '
                    f'value="{TOKEN}" />')
            return self._send(200, body.encode(), "text/html; charset=utf-8")
        if path == "/PublishedApprovals/SEVDetails/":
            record_id = self.path.split("id=")[-1]
            for sev, entry in REGISTER.items():
                if entry.get("id", f"id-{sev}") == record_id:
                    return self._send(200, _detail_page(sev, entry).encode(),
                                      "text/html; charset=utf-8")
            return self._send(404, b"no such entry", "text/plain")
        return self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        if self.path.split("?")[0] == GRID_PATH:
            if self.headers.get("__RequestVerificationToken") != TOKEN:
                REJECTED.append(self.path)
                return self._send(403, b"missing token", "text/plain")
            request = json.loads(raw or b"{}")
            status = request.get("base64SecureConfiguration", "").replace("SECURE-", "")
            rows = [(sev, entry) for sev, entry in REGISTER.items()
                    if entry.get("status", "in_force") == status]
            rows.sort(key=lambda pair: pair[0],
                      reverse="DESC" in request.get("sortExpression", "").upper())
            size = max(1, int(request.get("pageSize", 10)))
            page = max(1, int(request.get("page", 1)))
            window = rows[(page - 1) * size: page * size]
            body = json.dumps({
                "Records": [_record(sev, entry) for sev, entry in window],
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
