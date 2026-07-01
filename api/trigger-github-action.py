from http.server import BaseHTTPRequestHandler
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        expected_auth = os.environ.get("CRON_SECRET", "").strip()
        if expected_auth:
            authorization = self.headers.get("authorization", "")
            if authorization != f"Bearer {expected_auth}":
                self._json_response(401, {"ok": False, "error": "unauthorized"})
                return

        token = os.environ.get("GITHUB_DISPATCH_TOKEN", "").strip()
        owner = os.environ.get("GITHUB_OWNER", "csaizbc").strip()
        repo = os.environ.get("GITHUB_REPO", "Select-Stock").strip()
        workflow = os.environ.get("GITHUB_WORKFLOW", "update.yml").strip()
        ref = os.environ.get("GITHUB_REF", "main").strip()

        if not token:
            self._json_response(500, {"ok": False, "error": "GITHUB_DISPATCH_TOKEN is missing"})
            return

        url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow}/dispatches"
        body = json.dumps({"ref": ref}).encode("utf-8")
        dispatch_request = Request(
            url,
            data=body,
            method="POST",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "select-stock-vercel-cron",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

        try:
            with urlopen(dispatch_request, timeout=20) as response:
                github_status = response.status
        except HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            self._json_response(exc.code, {"ok": False, "error": message})
            return
        except (OSError, URLError) as exc:
            self._json_response(502, {"ok": False, "error": str(exc)})
            return

        self._json_response(200, {"ok": True, "github_status": github_status})

    def _json_response(self, status_code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
