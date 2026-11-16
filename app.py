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

RUN_MODE=inline (default): jobs live in memory and one in-process worker runs them one at a time:
run ONE instance (one replica, one uvicorn worker).
RUN_MODE=queue: new checks go to an Azure Storage queue; worker.py (same image, other start command)
takes them from there, so several workers can run at once (an event-driven Container Apps job).
Status, results and tab-order screenshots live in a blob container and are served through this API
(never as direct blob links). Storage is reached with a managed identity (no keys).

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
    VIEWER_HOSTS      report viewer host names (comma-separated). Default: the viewer the Fabric
                      community gallery uses to show entries, so links copied from the gallery work
    AUDIT_CODE_DIR    folder containing audit_accessibility.py (default ./audit)
    RUN_MODE          "inline" (default) or "queue"
  queue mode only:
    STORAGE_ACCOUNT   storage account name (the queue and blob container live there)
    QUEUE_NAME        storage queue for new checks (default "audits")
    BLOB_CONTAINER    blob container for status, results and screenshots (default "audits")
    MAX_PARALLEL      checks that run at once, 1-10 (default 5); must match the job's parallelism,
                      it is only used to tell visitors how many checks are ahead of theirs
    AZURE_CLIENT_ID   client ID of a user-assigned managed identity (leave empty for system-assigned)
"""

import collections
import contextlib
import hashlib
import hmac
import json
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
from fastapi.responses import FileResponse, JSONResponse, Response
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
# The Fabric community gallery shows reports through this viewer (…/iframeEmbed/<id>).
VIEWER_HOSTS = {h.lower() for h in env_list("VIEWER_HOSTS", "delightful-wave-030b3f21e.1.azurestaticapps.net")}
POWERBI_HOSTS = set(audit.POWERBI_HOSTS)
REUSE_SECONDS = 10 * 60  # the same link within 10 minutes returns the earlier result
RUN_MODE = os.environ.get("RUN_MODE", "inline").strip().lower()
if RUN_MODE not in ("inline", "queue"):
    raise SystemExit(f"RUN_MODE must be 'inline' or 'queue', not {RUN_MODE!r}")
MAX_PARALLEL = 1 if RUN_MODE == "inline" else min(10, max(1, int(os.environ.get("MAX_PARALLEL", "5"))))
STALE_QUEUED = 30 * 60   # queue mode: a check no worker picked up in 30 minutes is reported as failed
STALE_RUNNING = 45 * 60  # ... and one still "running" after 45 minutes (its worker died)
UNEXPECTED = "The check stopped unexpectedly. Please try again in a few minutes."
UNAVAILABLE = "The checker is having trouble right now. Please try again in a few minutes."
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
                        "open a community post. Use the bookmarklet on the post, or paste the "
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

    FIELDS = ("id", "url", "created", "status", "started", "finished", "page", "pages", "state", "result", "error")

    def to_dict(self):
        return {name: getattr(self, name) for name in self.FIELDS}

    @classmethod
    def from_dict(cls, data):
        return cls(**{name: data[name] for name in cls.FIELDS if name in data})


jobs = {}
waiting = queue.Queue(MAX_QUEUE)
lock = threading.Lock()
visits = collections.defaultdict(collections.deque)


def position(ahead, running):
    """Checks that must finish before this queued one can start (0 = it starts as soon as a worker is up)."""
    return max(0, ahead + running - MAX_PARALLEL + 1)


def queue_position(job):
    if RUN_MODE == "queue":
        listed = store().listing()
        ahead = sum(1 for j in listed if j["status"] == "queued" and j["created"] < job.created)
        running = sum(1 for j in listed if j["status"] == "running")
        return position(ahead, running)
    with lock:
        ahead = [j for j in jobs.values() if j.status == "queued" and j.created < job.created]
        running = any(j.status == "running" for j in jobs.values())
    return position(len(ahead), 1 if running else 0)


def friendly_error(status):
    text = status.split(":", 1)[-1].strip()
    if "did not load" in text or "Timeout" in text:
        return "The report didn't load. Check that the link opens the report without signing in."
    return "The check couldn't finish: " + text[:200]


def run_job(browser, job, on_progress=None):
    job.folder = WORK / job.id
    job.folder.mkdir(parents=True, exist_ok=True)

    def progress(number, total, state=0):
        job.page, job.pages, job.state = number, total, state
        if on_progress:
            on_progress(job)

    row = {"report_id": job.id, "url": job.url}
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


def queue_worker():
    """Queue mode worker (worker.py): takes one check from the queue, runs it, writes progress and the
    result to storage, then exits. While more checks wait and time allows, it takes the next one
    instead of exiting, so waiting checks don't depend on the scaler starting a new execution."""
    max_jobs = max(1, int(os.environ.get("WORKER_MAX_JOBS", "3")))
    budget = int(os.environ.get("WORKER_SECONDS", "3000"))      # keep below the job's replica timeout
    per_job = int(os.environ.get("WORKER_JOB_SECONDS", "1200"))  # assumed worst case for one check
    visibility = per_job + 300  # a message comes back for a second try if its worker died
    began = time.time()
    s = store()
    with sync_playwright() as pw:
        browser = None
        for _ in range(max_jobs):
            if time.time() - began + per_job > budget:
                break
            message = s.receive(visibility)
            if message is None:
                break
            try:
                job_id = json.loads(message.content).get("id", "")
            except (ValueError, AttributeError):
                job_id = ""
            job = s.load(job_id)
            if job is None or job.status in ("done", "error"):  # expired, or delivered twice
                s.done_with(message)
                continue
            if message.dequeue_count > 2:  # its worker died twice: give up
                job.status, job.error, job.finished = "error", UNEXPECTED, time.time()
                s.save(job)
                s.done_with(message)
                continue
            print(f"check {job.id}: start", flush=True)
            last = [0.0]

            def report(j):
                if time.time() - last[0] >= 1:
                    last[0] = time.time()
                    try:
                        s.save(j)
                    except Exception:
                        traceback.print_exc()  # progress is cosmetic: never let it stop the check

            try:
                if browser is None or not browser.is_connected():
                    browser = pw.chromium.launch(headless=True, channel=BROWSER_CHANNEL)
                job.status, job.started, job.page, job.pages, job.state = "running", time.time(), 0, 0, 0
                s.save(job)
                run_job(browser, job, on_progress=report)
                if job.status == "done":
                    for image in job.result["images"]:
                        s.put_image(job.id, job.folder / Path(image["url"]).name)
            except Exception:
                traceback.print_exc()
                job.status, job.error, job.result = "error", UNEXPECTED, None
                with contextlib.suppress(Exception):
                    browser.close()
                browser = None
            job.finished = time.time()
            try:
                s.save(job)
                s.done_with(message)
                print(f"check {job.id}: {job.status} in {round(job.finished - job.started)} s", flush=True)
            except Exception:
                traceback.print_exc()  # the message comes back and the check runs again
            if job.folder:
                shutil.rmtree(job.folder, ignore_errors=True)
        if browser is not None:
            with contextlib.suppress(Exception):
                browser.close()
    try:  # the web service may be scaled to zero, so expired results are also removed here
        s.clean()
    except Exception:
        traceback.print_exc()


# ---------- queue mode: Azure Storage (queue + blob container), managed identity ----------

class Store:
    """Everything queue mode keeps outside the process. Blob layout in the container:
        status/<id>.json        the job (status, progress, result); its metadata repeats status, created,
                                finished and a hash of the link, so one listing gives queue positions
        images/<id>/<name>.png  tab-order screenshots
        visits/<key>.json       start times per visitor (keyed hash of the IP, never the IP itself)
        config/salt             random key for that hash, created on first use
    """

    def __init__(self):
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import ContainerClient
        from azure.storage.queue import QueueClient
        account = os.environ.get("STORAGE_ACCOUNT", "").strip()
        if not account:
            raise RuntimeError("RUN_MODE=queue needs STORAGE_ACCOUNT")
        credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        # Short timeouts and few retries: a visitor gets the generic error quickly instead of a hung page.
        tuning = {"retry_total": 2, "connection_timeout": 5, "read_timeout": 30}
        self.blobs = ContainerClient(f"https://{account}.blob.core.windows.net",
                                     os.environ.get("BLOB_CONTAINER", "audits"), credential=credential, **tuning)
        self.queue = QueueClient(f"https://{account}.queue.core.windows.net",
                                 os.environ.get("QUEUE_NAME", "audits"), credential=credential, **tuning)
        self._listed, self._listed_at, self._salt = [], 0.0, None
        self._lock = threading.Lock()

    # jobs
    def save(self, job):
        meta = {"status": job.status, "created": repr(job.created), "finished": repr(job.finished or 0),
                "urlhash": url_hash(job.url)}
        self.blobs.upload_blob(f"status/{job.id}.json", json.dumps(job.to_dict()), overwrite=True, metadata=meta)
        with self._lock:
            self._listed_at = 0  # the next listing reads fresh

    def load(self, job_id):
        from azure.core.exceptions import ResourceNotFoundError
        if not re.fullmatch(r"[0-9a-f]{12}", job_id or ""):
            return None
        try:
            return Job.from_dict(json.loads(self.blobs.download_blob(f"status/{job_id}.json").readall()))
        except ResourceNotFoundError:
            return None

    def listing(self, max_age=3.0):
        """All jobs as small dicts, read from blob metadata (cached for a few seconds)."""
        with self._lock:
            if time.time() - self._listed_at < max_age:
                return self._listed
        listed = []
        for blob in self.blobs.list_blobs(name_starts_with="status/", include=["metadata"]):
            meta = blob.metadata or {}
            listed.append({"id": blob.name[len("status/"):-len(".json")], "status": meta.get("status", ""),
                           "created": float(meta.get("created") or 0), "finished": float(meta.get("finished") or 0),
                           "urlhash": meta.get("urlhash", ""), "modified": blob.last_modified.timestamp()})
        with self._lock:
            self._listed, self._listed_at = listed, time.time()
        return listed

    def delete(self, job_id):
        from azure.core.exceptions import ResourceNotFoundError
        names = [b.name for b in self.blobs.list_blobs(name_starts_with=f"images/{job_id}/")]
        for name in names + [f"status/{job_id}.json"]:
            with contextlib.suppress(ResourceNotFoundError):
                self.blobs.delete_blob(name)

    def put_image(self, job_id, path):
        from azure.storage.blob import ContentSettings
        with open(path, "rb") as data:
            self.blobs.upload_blob(f"images/{job_id}/{path.name}", data, overwrite=True,
                                   content_settings=ContentSettings(content_type="image/png"))

    def get_image(self, job_id, name):
        return self.blobs.download_blob(f"images/{job_id}/{name}").readall()

    # queue
    def enqueue(self, job):
        self.queue.send_message(json.dumps({"id": job.id}))

    def receive(self, visibility):
        return self.queue.receive_message(visibility_timeout=visibility)

    def done_with(self, message):
        from azure.core.exceptions import ResourceNotFoundError
        with contextlib.suppress(ResourceNotFoundError):
            self.queue.delete_message(message)

    # rate limit
    def visitor_key(self, ip):
        from azure.core.exceptions import ResourceExistsError
        if self._salt is None:
            with contextlib.suppress(ResourceExistsError):
                self.blobs.upload_blob("config/salt", os.urandom(32).hex(), overwrite=False)
            self._salt = self.blobs.download_blob("config/salt").readall()
        return hmac.new(self._salt, ip.encode(), hashlib.sha256).hexdigest()[:32]

    def take_visit(self, ip, limit):
        """Records a start for this visitor and returns 0; when they already started `limit` checks
        this hour, records nothing and returns the seconds until the oldest one is an hour old."""
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
        blob = self.blobs.get_blob_client(f"visits/{self.visitor_key(ip)}.json")
        for _ in range(8):  # optimistic concurrency: try again when another instance wrote in between
            try:
                download = blob.download_blob()
                stamps, etag = json.loads(download.readall()), download.properties.etag
            except ResourceNotFoundError:
                stamps, etag = [], None
            now = time.time()
            stamps = [t for t in stamps if t > now - 3600]
            if len(stamps) >= limit:
                return retry_after(stamps, now)
            stamps.append(now)
            try:
                if etag:
                    blob.upload_blob(json.dumps(stamps), overwrite=True, etag=etag,
                                     match_condition=MatchConditions.IfNotModified)
                else:
                    blob.upload_blob(json.dumps(stamps), overwrite=False)
                return 0
            except (ResourceModifiedError, ResourceExistsError):
                continue
        return 60  # still racing after 8 tries: ask for a minute's patience

    # clean-up (the blob lifecycle rule deletes anything older than a day as a safety net)
    def clean(self):
        from azure.core.exceptions import ResourceNotFoundError
        now = time.time()
        for j in self.listing(max_age=0):
            if expired(j["status"], j["finished"], now) or now - j["modified"] > 6 * 3600:
                self.delete(j["id"])
        for blob in self.blobs.list_blobs(name_starts_with="visits/"):
            if now - blob.last_modified.timestamp() > 3600:
                with contextlib.suppress(ResourceNotFoundError):
                    self.blobs.delete_blob(blob.name)
        with self._lock:
            self._listed_at = 0


_store = None
_store_lock = threading.Lock()


def store():
    global _store
    with _store_lock:
        if _store is None:
            _store = Store()
        return _store


def url_hash(url):
    return hashlib.sha256(url.encode()).hexdigest()[:32]


def expired(status, finished, now=None):
    return status in ("done", "error") and bool(finished) and finished < (now or time.time()) - RESULT_SECONDS


def stale(job, now=None):
    """Queue mode: the worker for this check died or never started."""
    now = now or time.time()
    return ((job.status == "queued" and now - job.created > STALE_QUEUED)
            or (job.status == "running" and now - (job.started or job.created) > STALE_RUNNING))


def store_cleaner():
    while True:
        time.sleep(60)
        try:
            store().clean()
        except Exception:
            traceback.print_exc()


# ---------- result shape for the page ----------

def split(value):
    return [part for part in (value or "").split("; ") if part]


def items_of(value):
    try:
        items = json.loads(value or "[]")
    except ValueError:
        return []
    return items if isinstance(items, list) else []


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
            "items": items_of(r.get("visuals_items")),
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
    if RUN_MODE == "queue":
        threading.Thread(target=store_cleaner, daemon=True, name="cleaner").start()
    else:
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


def retry_after(stamps, now=None):
    """Seconds until the oldest of this hour's starts drops out of the hour."""
    return max(1, int(min(stamps) + 3600 - (now or time.time())) + 1)


def rate_limited(wait):
    """retry_after: seconds until this visitor can start the next check (the page counts down)."""
    return JSONResponse({"code": "rate_limit", "error": f"You can start {RATE_LIMIT} checks per hour. "
                         "Please try again later.", "retry_after": wait, "limit": RATE_LIMIT},
                        status_code=429, headers={"Retry-After": str(wait)})


def busy():
    return JSONResponse({"code": "busy", "error": "The checker is busy. Please try again in a few minutes."},
                        status_code=503)


def unavailable():
    """Queue mode: storage trouble. Its exception text names the storage account, so visitors get this."""
    traceback.print_exc()
    return JSONResponse({"code": "unavailable", "error": UNAVAILABLE}, status_code=503)


@app.post("/api/audits")
def start_audit(body: AuditRequest, request: Request):
    try:
        url = check_link(body.url)
    except LinkError as exc:
        return JSONResponse({"code": exc.code, "error": exc.message}, status_code=422)
    if RUN_MODE == "queue":
        try:
            return start_queued(url, client_ip(request))
        except Exception:
            return unavailable()
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
        return rate_limited(retry_after(stamps))
    job = Job(id=uuid.uuid4().hex[:12], url=url)
    try:
        waiting.put_nowait(job)
    except queue.Full:
        return busy()
    stamps.append(time.time())
    with lock:
        jobs[job.id] = job
    return {"id": job.id}


def start_queued(url, ip):
    s, now, key = store(), time.time(), url_hash(url)
    listed = s.listing(max_age=0)
    recent = [j for j in listed if j["urlhash"] == key and j["status"] in ("queued", "running", "done")
              and (j["status"] != "done" or now - j["finished"] < REUSE_SECONDS)]
    for j in sorted(recent, key=lambda j: -j["created"]):
        job = s.load(j["id"])  # the hash is short: confirm the link itself
        if job and job.url == url and not stale(job, now):
            return {"id": job.id}
    if sum(1 for j in listed if j["status"] == "queued") >= MAX_QUEUE:
        return busy()
    wait = s.take_visit(ip, RATE_LIMIT)
    if wait:
        return rate_limited(wait)
    job = Job(id=uuid.uuid4().hex[:12], url=url)
    s.save(job)
    try:
        s.enqueue(job)
    except Exception:
        with contextlib.suppress(Exception):
            s.delete(job.id)
        raise
    return {"id": job.id}


def load_queued(job_id):
    """Queue mode: the job from storage, or None when unknown or expired."""
    job = store().load(job_id)
    if job is None or expired(job.status, job.finished):
        return None
    if stale(job):
        job.status, job.error, job.finished = "error", UNEXPECTED, time.time()
    return job


@app.get("/api/audits/{job_id}")
def audit_status(job_id: str):
    if RUN_MODE == "queue":
        try:
            job = load_queued(job_id)
            body = describe(job) if job else None
        except Exception:
            return unavailable()
    else:
        job = jobs.get(job_id)
        body = describe(job) if job else None
    if body is None:
        raise HTTPException(404, "This check has expired. Start a new one.")
    return body


@app.get("/api/audits/{job_id}/images/{name}")
def audit_image(job_id: str, name: str):
    headers = {"Cache-Control": "private, max-age=3600"}
    if RUN_MODE == "queue":
        try:
            job = load_queued(job_id)
            if job and job.status == "done" and name in {Path(i["url"]).name for i in job.result["images"]}:
                return Response(store().get_image(job.id, name), media_type="image/png", headers=headers)
        except Exception:
            return unavailable()
        raise HTTPException(404, "Image not found")
    job = jobs.get(job_id)
    if not job or job.status != "done" or name not in {Path(i["url"]).name for i in job.result["images"]}:
        raise HTTPException(404, "Image not found")
    return FileResponse(job.folder / name, media_type="image/png", headers=headers)


@app.get("/api/health")
def health():
    if RUN_MODE == "queue":
        try:
            return {"ok": True, "queued": sum(1 for j in store().listing(max_age=30) if j["status"] == "queued")}
        except Exception:
            traceback.print_exc()
            return {"ok": True, "queued": 0}
    return {"ok": True, "queued": waiting.qsize()}


app.mount("/", StaticFiles(directory=HERE / "static", html=True), name="static")
