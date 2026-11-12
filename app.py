# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Karin Szilágyi
"""Public accessibility check for Power BI reports.

Wraps audit_report() from audit/audit_accessibility.py in a small web API:
    POST /api/audits                 {"url": "..."} -> {"id": "..."}   (queues an audit)
    GET  /api/audits/{id}            status, queue position, progress and, when done, the result
    GET  /api/audits/{id}/images/{f} tab-order screenshot of one page
    GET  /api/health
The page in static/ is served at / for local use; on the public site it calls this API cross-origin.

Accepted links: Power BI "Publish to web" links (app.powerbi.com or app.fabric.microsoft.com,
/view?r=...) and the Fabric community's report viewer (.../iframeEmbed/...). Community post links
are refused with code "community_post": the community site shows a bot check to automated
browsers, so the page points people to the bookmarklet instead.

Jobs live in memory: run ONE instance (one replica, one uvicorn worker).

Local run (from this folder):
    .venv\\Scripts\\python.exe -m uvicorn app:app --port 8000      (set BROWSER_CHANNEL=chrome on Windows)
Settings (environment variables):
    ALLOWED_ORIGINS   comma-separated origins allowed to call the API (default: localhost:8000), e.g. "https://example.com,https://www.example.com"
    BROWSER_CHANNEL   "chrome" to use installed Google Chrome; empty for Playwright's Chromium (container)
    MAX_PAGES         pages audited per report (default 12)
    MAX_STATES        bookmark-button views checked per page (default 8; 0 turns button clicking off)
    EXPLORE_SECONDS   time per report for clicking buttons (default 300)
    MAX_QUEUE         waiting audits before new ones are refused (default 20)
    RATE_LIMIT        audits one visitor can start per hour (default 6)
    RESULT_MINUTES    how long results and screenshots are kept (default 60)
    VIEWER_HOSTS      community report viewer host names (comma-separated)
    AUDIT_CODE_DIR    folder containing audit_accessibility.py (default ./audit)
"""

import collections
import contextlib
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(Path(os.environ.get("AUDIT_CODE_DIR", HERE / "audit")).resolve()))
import audit_accessibility as audit  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402


def env_list(name, default):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


ALLOWED_ORIGINS = env_list("ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000")
BROWSER_CHANNEL = os.environ.get("BROWSER_CHANNEL", "").strip() or None
MAX_PAGES = int(os.environ.get("MAX_PAGES", "12"))
MAX_STATES = int(os.environ.get("MAX_STATES", "8"))
EXPLORE_SECONDS = int(os.environ.get("EXPLORE_SECONDS", "300"))
MAX_QUEUE = int(os.environ.get("MAX_QUEUE", "20"))
RATE_LIMIT = int(os.environ.get("RATE_LIMIT", "6"))
RESULT_SECONDS = 60 * int(os.environ.get("RESULT_MINUTES", "60"))
VIEWER_HOSTS = {h.lower() for h in env_list("VIEWER_HOSTS", "delightful-wave-030b3f21e.1.azurestaticapps.net")}
POWERBI_HOSTS = set(audit.POWERBI_HOSTS)
REUSE_SECONDS = 10 * 60  # the same link within 10 minutes returns the earlier result
audit.MAX_ISSUES = 50    # the audit default is 5 issues per test; the public page shows more

WORK = Path(tempfile.mkdtemp(prefix="a11y-audits-"))


# ---------- link check ----------

class LinkError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def check_link(raw):
    """Returns the link to audit, or raises LinkError with a message for the visitor."""
    url = (raw or "").strip()
    if not url:
        raise LinkError("empty", "Paste the link to your report.")
    if len(url) > 2000:
        raise LinkError("unsupported", "That link is too long to be a report link.")
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        raise LinkError("unsupported", "Paste a full link starting with https://.")
    if host in POWERBI_HOSTS:
        if parsed.path.rstrip("/") == "/view" and parse_qs(parsed.query).get("r"):
            return url
        raise LinkError("not_public", "This Power BI link needs a sign-in. Use the report's "
                        "Publish to web link instead: it looks like https://app.powerbi.com/view?r=...")
    if host in VIEWER_HOSTS and "/iframeEmbed/" in parsed.path:
        return url
    if host == "community.fabric.microsoft.com" or host.endswith(".fabric.microsoft.com"):
        raise LinkError("community_post", "The Fabric community site blocks automated visitors, so we can't "
                        "open a submission post. Use the bookmarklet on your submission page, or paste the "
                        "report link itself.")
    raise LinkError("unsupported", "Only Power BI Publish to web links and Fabric community report "
                    "links can be checked.")


# ---------- jobs ----------

@dataclass
class Job:
    id: str
    url: str
    created: float = field(default_factory=time.time)
    status: str = "queued"      # queued, running, done, error
    started: float = 0
    finished: float = 0
    page: int = 0
    pages: int = 0
    state: int = 0
    result: dict = None
    error: str = ""
    folder: Path = None


jobs = {}
waiting = queue.Queue(MAX_QUEUE)
lock = threading.Lock()
visits = collections.defaultdict(collections.deque)


def queue_position(job):
    with lock:
        ahead = [j for j in jobs.values() if j.status == "queued" and j.created < job.created]
        running = any(j.status == "running" for j in jobs.values())
    return len(ahead) + (1 if running else 0)


def friendly_error(status):
    text = status.split(":", 1)[-1].strip()
    if "did not load" in text or "Timeout" in text:
        return "The report didn't load. Check that the link opens the report without signing in."
    return "The check couldn't finish: " + text[:200]


def run_job(browser, job):
    job.folder = WORK / job.id
    job.folder.mkdir(parents=True, exist_ok=True)

    def progress(number, total, state=0):
        job.page, job.pages, job.state = number, total, state

    row = {"submission_id": job.id, "title": "", "iframe_url": job.url}
    result = audit.audit_report(browser, row, job.folder, max_pages=MAX_PAGES, on_page=progress,
                                max_states=MAX_STATES, explore_seconds=EXPLORE_SECONDS)
    status = result.get("audit_status", "")
    if status.startswith("error") or status.startswith("not audited"):
        job.error = friendly_error(status)
        job.status = "error"
    else:
        job.result = shape_result(job, result)
        job.status = "done"


def worker():
    with sync_playwright() as pw:
        browser = None
        while True:
            job = waiting.get()
            try:
                if browser is None or not browser.is_connected():
                    browser = pw.chromium.launch(headless=True, channel=BROWSER_CHANNEL)
                job.status, job.started = "running", time.time()
                run_job(browser, job)
            except Exception as exc:  # keep the worker alive whatever one report does
                traceback.print_exc()
                job.status, job.error = "error", "The check stopped unexpectedly: " + str(exc).splitlines()[0][:200]
                try:
                    browser.close()
                except Exception:
                    pass
                browser = None
            finally:
                job.finished = time.time()


def cleaner():
    while True:
        time.sleep(60)
        cutoff = time.time() - RESULT_SECONDS
        with lock:
            old = [j for j in jobs.values() if j.status in ("done", "error") and j.finished and j.finished < cutoff]
            for job in old:
                jobs.pop(job.id, None)
        for job in old:
            if job.folder:
                shutil.rmtree(job.folder, ignore_errors=True)
        for ip in list(visits):
            stamps = visits[ip]
            while stamps and stamps[0] < time.time() - 3600:
                stamps.popleft()
            if not stamps:
                visits.pop(ip, None)


# ---------- result shape for the page ----------

def split(value):
    return [part for part in (value or "").split("; ") if part]


def shape_result(job, r):
    tests = []
    for key in audit.TESTS:
        tests.append({"key": key, "score": int(r[f"{key}_score"]) if r.get(f"{key}_score") else None,
                      "checked": int(r.get(f"{key}_checked") or 0), "issues": split(r.get(f"{key}_issues"))})
    images = []
    titles = split(r.get("tab_order_titles"))
    for number, name in enumerate(split(r.get("tab_order_images"))):
        match = re.match(r".+?_p(\d+)(?:s(\d+))?(?:_(.+))?\.png$", name)
        title = titles[number] if len(titles) > number else ((match.group(3) or "").replace("-", " ") if match else "")
        images.append({"page": int(match.group(1)) if match else len(images) + 1,
                       "state": int(match.group(2) or 0) if match else 0,
                       "name": title, "url": f"/api/audits/{job.id}/images/{name}"})
    status = r.get("audit_status", "")
    return {
        "pages_audited": int(r.get("pages_audited") or 0),
        "states_audited": int(r.get("states_audited") or 0),
        "partial": status.split(":", 1)[1].strip() if status.startswith("partial") else "",
        "tests": tests,
        "visuals": {
            "core_data": int(r.get("visuals_core_data") or 0),
            "core_layout": int(r.get("visuals_core_layout") or 0),
            "custom": int(r.get("visuals_custom") or 0),
            "core_share": int(r["visuals_core_share"]) if r.get("visuals_core_share") else None,
            "core_data_types": r.get("visuals_core_data_types", ""),
            "custom_types": r.get("visuals_custom_types", ""),
            "by_page": split(r.get("visuals_by_page")),
        },
        "images": images,
        "notes": split(r.get("audit_notes")),
        "audited_at": r.get("audited_at", ""),
    }


def describe(job):
    body = {"id": job.id, "url": job.url, "status": job.status}
    if job.status == "queued":
        body["position"] = queue_position(job)
    if job.status == "running":
        body.update(page=job.page, pages=job.pages, state=job.state, seconds=round(time.time() - job.started))
    if job.status == "done":
        body.update(result=job.result, seconds=round(job.finished - job.started))
    if job.status == "error":
        body["error"] = job.error
    return body


# ---------- API ----------

@contextlib.asynccontextmanager
async def lifespan(_app):
    threading.Thread(target=worker, daemon=True, name="audit-worker").start()
    threading.Thread(target=cleaner, daemon=True, name="cleaner").start()
    yield
    shutil.rmtree(WORK, ignore_errors=True)


app = FastAPI(title="Power BI accessibility check", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["GET", "POST"], allow_headers=["Content-Type"])


class AuditRequest(BaseModel):
    url: str


def client_ip(request):
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


@app.post("/api/audits")
def start_audit(body: AuditRequest, request: Request):
    try:
        url = check_link(body.url)
    except LinkError as exc:
        return JSONResponse({"code": exc.code, "error": exc.message}, status_code=422)
    with lock:
        recent = [j for j in jobs.values() if j.url == url and j.status != "error"
                  and (j.status in ("queued", "running") or time.time() - j.finished < REUSE_SECONDS)]
    if recent:
        return {"id": max(recent, key=lambda j: j.created).id}
    ip = client_ip(request)
    stamps = visits[ip]
    while stamps and stamps[0] < time.time() - 3600:
        stamps.popleft()
    if len(stamps) >= RATE_LIMIT:
        return JSONResponse({"code": "rate_limit", "error": f"You can start {RATE_LIMIT} checks per hour. "
                             "Please try again later."}, status_code=429)
    job = Job(id=uuid.uuid4().hex[:12], url=url)
    try:
        waiting.put_nowait(job)
    except queue.Full:
        return JSONResponse({"code": "busy", "error": "The checker is busy. Please try again in a few minutes."},
                            status_code=503)
    stamps.append(time.time())
    with lock:
        jobs[job.id] = job
    return {"id": job.id}


@app.get("/api/audits/{job_id}")
def audit_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "This check has expired. Start a new one.")
    return describe(job)


@app.get("/api/audits/{job_id}/images/{name}")
def audit_image(job_id: str, name: str):
    job = jobs.get(job_id)
    if not job or job.status != "done" or name not in {Path(i["url"]).name for i in job.result["images"]}:
        raise HTTPException(404, "Image not found")
    return FileResponse(job.folder / name, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/health")
def health():
    return {"ok": True, "queued": waiting.qsize()}


app.mount("/", StaticFiles(directory=HERE / "static", html=True), name="static")
