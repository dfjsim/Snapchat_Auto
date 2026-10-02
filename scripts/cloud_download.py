"""Retrieve Memories media from Snapchat's servers — opt-in, authority-gated, paced and recorded.

A Memory's row in ``scdb-27.sqlite3`` records where its media is stored: ``ZMEDIADOWNLOADURL`` (and
``ZOVERLAYDOWNLOADURL`` for its overlay), with ``ZMEDIAREDIRECTURI`` / ``ZOVERLAYREDIRECTURI`` as an
alternative address. A plain HTTPS GET of that address returns the media encrypted with the Memory's
own AES key — the key the device holds for it, which the Memories report already reads to decrypt
the device's cached copies. This module does the requesting; :mod:`scripts.cloud_memories` decides
what to request and puts what comes back into the reports.

Nothing here runs unless the examiner asks for it, and :class:`Engine` refuses to start without an
:class:`Authority` — an attestation that the examiner holds the legal authority to retrieve the data
from Snapchat's servers and a note of what that authority is. Every request is then:

* **limited to the addresses the device recorded**: HTTPS only, the first host one of the CDN domains
  those columns hold (``*.sc-cdn.net``) unless the examiner adds one, and no redirect may lead to a
  private, loopback or link-local address — the URLs come from evidence, which could have been
  tampered with;
* **plain**: no cookies, no credentials, no account token, nothing that signs in; the User-Agent
  names this tool and its version;
* **paced**: one at a time, a delay with jitter between requests and a cap per minute, exponential
  back-off honouring ``Retry-After`` on 429 / 5xx, and a stop after too many failures in a row — all
  adjustable while it runs (:class:`Control`);
* **recorded**: what was asked, when, under which authority, and what came back — status, headers,
  sizes and hashes of the bytes as received and as decrypted — in an append-only, hash-chained
  ``CloudDownloads/cloud_manifest.jsonl`` (:class:`Store`), beside the bytes themselves. Nothing there
  is ever overwritten; a re-run reuses what was already retrieved.

What comes back is **not device evidence**. It is kept apart from ``ExtractedData/`` and marked as
retrieved wherever a report shows it.

The method — request the recorded URL, decrypt with the Memory's key — follows DFIR-HBG's
Snapchat_DownloadMemories_iOS (https://github.com/DFIR-HBG/Snapchat_DownloadMemories_iOS), with
overlay retrieval contributed there by John Hyla (snoop168). That repository carries no licence, so
none of its code is used; this implementation is this project's own.
"""
import datetime
import email.utils
import hashlib
import ipaddress
import json
import logging
import os
import random
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

STORE_DIR = "CloudDownloads"
MANIFEST = "cloud_manifest.jsonl"
#: The hosts the Memories URL columns point at, in every extraction seen. The first request must go
#: to one of them (or to a host the examiner adds); a redirect may lead elsewhere, but never to a
#: private address.
DEFAULT_HOST_SUFFIXES = ("sc-cdn.net",)
#: Status codes that say "this address will not give it to you" — try the next address, don't retry.
PERMANENT = {400, 401, 403, 404, 405, 410, 451}
#: The minimum note: something a reader can identify the authority by.
MIN_NOTE = 8
#: What the request headers say about the client.
HEADERS_KEPT = ("Date", "Content-Type", "Content-Length", "ETag", "Last-Modified", "Server",
                "Age", "X-Cache", "Cache-Control")


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ----------------------------------------------------------------------------- what the run is

@dataclass
class Authority:
    """The examiner's statement of authority. Never remembered between runs."""
    note: str
    attested: bool
    attested_utc: str = ""

    def problems(self):
        out = []
        if not self.attested:
            out.append("the examiner has not confirmed holding the legal authority to retrieve "
                       "this data from Snapchat's servers")
        if len((self.note or "").strip()) < MIN_NOTE:
            out.append("the legal authority is not described (e.g. 'Search warrant #… issued by … "
                       "on …', 'Consent of <name>, <date>')")
        return out

    def as_dict(self):
        return {"note": (self.note or "").strip(), "attested": bool(self.attested),
                "attested_utc": self.attested_utc}


@dataclass
class Pace:
    """How fast to ask. Every field can change while the engine runs (see :class:`Control`)."""
    delay_s: float = 4.0            # between two requests
    jitter_s: float = 2.0           # + a random 0..jitter_s on top
    max_per_min: int = 10           # at most this many requests in any 60 seconds
    max_failures: int = 5           # stop after this many transient failures in a row
    max_refusals: int = 25          # stop after this many refused addresses (403/404/…) in a row
    backoff_base_s: float = 30.0    # first wait after a 429 / 5xx without Retry-After
    backoff_max_s: float = 900.0
    retries: int = 3                # attempts per address on a transient failure
    timeout_s: float = 60.0
    max_bytes: int = 1 << 30

    def problems(self):
        out = []
        if self.delay_s < 0 or self.jitter_s < 0:
            out.append("delay and jitter cannot be negative")
        if self.max_per_min < 1:
            out.append("at least one request per minute")
        if self.max_failures < 1 or self.max_refusals < 1 or self.retries < 1:
            out.append("failure limits and retries must be at least 1")
        if self.timeout_s <= 0:
            out.append("the timeout must be positive")
        return out


@dataclass
class Job:
    """One file to retrieve: a Memory's media or overlay, from one or more recorded addresses."""
    snap_id: str
    role: str                         # "media" | "overlay"
    urls: list                        # [(column, url)] in the order to try them
    reason: str = ""                  # why this one: missing / incomplete … / selection / requested


@dataclass
class Event:
    """What the engine tells its front end (progress window, or the log)."""
    kind: str                         # start item_start wait request item_done item_failed
    job: Job = None                   # item_skipped log end
    text: str = ""
    done: int = 0
    failed: int = 0
    skipped: int = 0
    total: int = 0
    bytes: int = 0
    wait_s: float = 0.0
    eta_s: float = None


@dataclass
class Summary:
    session: str = ""
    done: int = 0
    failed: int = 0
    skipped: int = 0
    bytes: int = 0
    stopped: str = "completed"        # completed | examiner | failures | refused
    records: list = field(default_factory=list)


# ------------------------------------------------------------------------------------- fetching

class FetchError(Exception):
    def __init__(self, text, *, status=None, permanent=False, retry_after_s=None, redirects=None):
        super().__init__(text)
        self.status = status
        self.permanent = permanent
        self.retry_after_s = retry_after_s
        self.redirects = redirects or []


@dataclass
class Response:
    status: int
    final_url: str
    redirects: list
    headers: dict
    path: str
    bytes: int
    sha256: str
    md5: str


class HostPolicy:
    """Which addresses may be contacted. See the module docstring."""

    def __init__(self, suffixes=DEFAULT_HOST_SUFFIXES, allow_loopback=False):
        self.suffixes = tuple(s.lower().lstrip(".") for s in suffixes if s)
        self.allow_loopback = allow_loopback

    def refuse(self, url, *, first):
        """Why ``url`` may not be requested, or None."""
        parts = urlparse(url or "")
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" and not (self.allow_loopback and parts.scheme == "http"):
            return f"not an https address ({parts.scheme or 'no scheme'})"
        if not host:
            return "no host"
        if first and not any(host == s or host.endswith("." + s) for s in self.suffixes):
            return f"{host} is not one of the recorded CDN hosts ({', '.join(self.suffixes)})"
        try:
            addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
        except OSError as error:
            return f"{host} does not resolve ({error})"
        for address in addresses:
            ip = ipaddress.ip_address(address.split("%")[0])
            if ip.is_loopback and self.allow_loopback:
                continue
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved \
                    or ip.is_multicast or ip.is_unspecified:
                return f"{host} resolves to a non-public address ({address})"
        return None


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Records every hop and refuses one the policy does not allow."""

    def __init__(self, policy):
        super().__init__()
        self.policy = policy
        self.hops = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.hops.append({"status": code, "location": newurl})
        why = self.policy.refuse(newurl, first=False)
        if why:
            raise FetchError(f"redirect refused: {why}", status=code, permanent=True,
                             redirects=list(self.hops))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _retry_after(value):
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
        return max(0.0, (when - datetime.datetime.now(when.tzinfo)).total_seconds())
    except (TypeError, ValueError):
        return None


class UrllibFetcher:
    """One GET with the standard library: no cookie handling, no credentials, redirects recorded."""

    def __init__(self, policy, user_agent):
        self.policy = policy
        self.user_agent = user_agent

    def fetch(self, url, *, timeout, max_bytes, dest):
        why = self.policy.refuse(url, first=True)
        if why:
            raise FetchError(f"address refused: {why}", permanent=True)
        redirects = _Redirects(self.policy)
        opener = urllib.request.build_opener(redirects)    # no HTTPCookieProcessor: no cookies
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent,
                                                       "Accept": "*/*"})
        try:
            with opener.open(request, timeout=timeout) as resp:
                headers = {k: resp.headers.get(k) for k in HEADERS_KEPT if resp.headers.get(k)}
                expected = resp.headers.get("Content-Length")
                sha, md5, total = hashlib.sha256(), hashlib.md5(), 0
                part = dest + ".part"
                with open(part, "wb") as out:
                    while True:
                        block = resp.read(1 << 16)
                        if not block:
                            break
                        total += len(block)
                        if total > max_bytes:
                            raise FetchError(f"more than {max_bytes} bytes", permanent=True,
                                             redirects=redirects.hops)
                        sha.update(block)
                        md5.update(block)
                        out.write(block)
                if expected and expected.isdigit() and int(expected) != total:
                    os.remove(part)
                    raise FetchError(f"short body ({total} of {expected} bytes)",
                                     status=resp.status, redirects=redirects.hops)
                os.replace(part, dest)
                return Response(resp.status, resp.geturl(), list(redirects.hops), headers, dest,
                                total, sha.hexdigest(), md5.hexdigest())
        except FetchError:
            raise
        except urllib.error.HTTPError as error:
            status = error.code
            raise FetchError(f"HTTP {status}", status=status,
                             permanent=status in PERMANENT,
                             retry_after_s=_retry_after(error.headers.get("Retry-After")),
                             redirects=redirects.hops) from error
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as error:
            reason = getattr(error, "reason", error)
            if isinstance(reason, FetchError):
                raise reason
            raise FetchError(f"network error: {reason}", redirects=redirects.hops) from error
        finally:
            if os.path.exists(dest + ".part"):
                try:
                    os.remove(dest + ".part")
                except OSError:
                    pass


# ---------------------------------------------------------------------------------- control

class Control:
    """Pause / resume / stop and the pace, shared between the engine and its front end."""

    def __init__(self, pace=None):
        self._lock = threading.Lock()
        self._pace = pace or Pace()
        self._paused = threading.Event()
        self._stopped = threading.Event()
        self.version = 0

    @property
    def pace(self):
        with self._lock:
            return Pace(**asdict(self._pace))

    def update(self, **changes):
        with self._lock:
            candidate = Pace(**dict(asdict(self._pace), **changes))
            problems = candidate.problems()
            if problems:
                return problems
            self._pace = candidate
            self.version += 1
        return []

    def pause(self):
        self._paused.set()

    def resume(self):
        self._paused.clear()

    def stop(self):
        self._stopped.set()

    @property
    def paused(self):
        return self._paused.is_set()

    @property
    def stopped(self):
        return self._stopped.is_set()


# ------------------------------------------------------------------------------------ storage

class Store:
    """``<run>/CloudDownloads``: the bytes, and the manifest that says where each came from."""

    def __init__(self, run_folder):
        self.base = os.path.join(run_folder, STORE_DIR)
        self.manifest = os.path.join(self.base, MANIFEST)
        self._prev = None
        self._lockfile = None

    # ---- the lock: one process writes at a time
    def lock(self):
        os.makedirs(self.base, exist_ok=True)
        path = os.path.join(self.base, ".lock")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            raise RuntimeError(f"{path} exists: another retrieval is writing to this run folder "
                               f"(remove the file if none is)") from error
        os.write(fd, f"{os.getpid()} {now_utc()}".encode())
        os.close(fd)
        self._lockfile = path
        self._write_readme()

    def unlock(self):
        if self._lockfile and os.path.exists(self._lockfile):
            os.remove(self._lockfile)
        self._lockfile = None

    def _write_readme(self):
        path = os.path.join(self.base, "README.txt")
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("Memories media retrieved from Snapchat's servers by Snapchat_Auto, at the\n"
                         "examiner's request and under the legal authority recorded in\n"
                         f"{MANIFEST}. These files are NOT device evidence: they are what the\n"
                         "addresses recorded on the device returned when they were requested.\n"
                         "encrypted/ holds the bytes as received, decrypted/ the same bytes\n"
                         "decrypted with the Memory's own key. Nothing here is ever overwritten.\n")

    # ---- the manifest
    def records(self):
        if not os.path.exists(self.manifest):
            return []
        out = []
        with open(self.manifest, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def record(self, kind, **fields):
        if self._prev is None:
            self._prev = self._last_line_hash()
        rec = dict(type=kind, **fields, prev=self._prev or None)
        line = json.dumps(rec, sort_keys=True, ensure_ascii=False)
        with open(self.manifest, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._prev = hashlib.sha256(line.encode("utf-8")).hexdigest()
        return rec

    def _last_line_hash(self):
        """The SHA-256 of the manifest's last line as written, or "" for a new manifest."""
        last = ""
        if os.path.exists(self.manifest):
            with open(self.manifest, encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        last = line.rstrip("\n")
        return hashlib.sha256(last.encode("utf-8")).hexdigest() if last else ""

    def verify(self):
        """``[problem]`` — empty when every record's ``prev`` is the hash of the line before it."""
        problems, prev = [], ""
        if not os.path.exists(self.manifest):
            return problems
        with open(self.manifest, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                line = line.rstrip("\n")
                if not line:
                    continue
                rec = json.loads(line)
                if (rec.get("prev") or "") != prev:
                    problems.append(f"line {n}: does not follow the line before it")
                prev = hashlib.sha256(line.encode("utf-8")).hexdigest()
        return problems

    def latest(self, snap_id, role):
        """The last successful retrieval of this file, or None."""
        found = None
        for rec in self.records():
            if rec.get("type") == "request" and rec.get("snap_id") == snap_id \
                    and rec.get("role") == role and rec.get("http_status") == 200 \
                    and rec.get("stored_encrypted"):
                found = rec
        return found

    def next_path(self, folder, snap_id, role, ext):
        directory = os.path.join(self.base, folder, snap_id)
        os.makedirs(directory, exist_ok=True)
        seq = 1
        while any(name.startswith(f"{role}-{seq}.") for name in os.listdir(directory)):
            seq += 1
        return os.path.join(directory, f"{role}-{seq}.{ext}"), seq

    def rel(self, path):
        return os.path.relpath(path, self.base).replace("\\", "/")


# ------------------------------------------------------------------------------------- engine

class Engine:
    """Runs a list of :class:`Job` through the fetcher at the :class:`Control`'s pace."""

    def __init__(self, store, fetcher, control, authority, *, on_event=None, clock=time.monotonic,
                 sleep=time.sleep, rng=None):
        self.store = store
        self.fetcher = fetcher
        self.control = control
        self.authority = authority
        self.on_event = on_event or (lambda event: None)
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.Random()
        self._sent = []                                    # clock() of each request, for the cap

    # ---- waiting, in slices, so pause / stop / a pace change act at once
    def _wait(self, seconds, reason, counts):
        end = self.clock() + seconds
        version = self.control.version
        was_paused = False
        while True:
            if self.control.stopped:
                return False
            if self.control.paused:
                if not was_paused:
                    self._emit("wait", text="paused", wait_s=0, **counts)
                    was_paused = True
                self.sleep(0.2)
                end += 0.2
                continue
            was_paused = False
            if self.control.version != version:            # the pace changed: recompute
                return None
            left = end - self.clock()
            if left <= 0:
                return True
            self._emit("wait", text=reason, wait_s=left, **counts)
            self.sleep(min(0.5, left))

    def _pace_wait(self, counts):
        while True:
            pace = self.control.pace
            gap = pace.delay_s + self.rng.uniform(0, pace.jitter_s) if self._sent else 0.0
            since = (self.clock() - self._sent[-1]) if self._sent else None
            wait = max(0.0, gap - since) if since is not None else 0.0
            window = [t for t in self._sent if self.clock() - t < 60.0]
            if len(window) >= pace.max_per_min:
                wait = max(wait, 60.0 - (self.clock() - window[0]))
            if wait <= 0:
                return not self.control.stopped
            result = self._wait(wait, "pace", counts)
            if result is None:
                continue                                    # pace changed while waiting
            return result

    def _emit(self, kind, **fields):
        try:
            self.on_event(Event(kind, **fields))
        except Exception as error:                          # noqa: BLE001 - a UI must not stop it
            logger.debug(f"cloud: event handler failed ({error})")

    def _eta(self, remaining, avg_transfer):
        pace = self.control.pace
        per = max(avg_transfer + pace.delay_s + pace.jitter_s / 2, 60.0 / pace.max_per_min)
        return remaining * per

    def run(self, jobs, *, decrypt, redownload=False, scope=None, entry_point="", date_rules=None,
            run_id=""):
        problems = self.authority.problems() + self.control.pace.problems()
        if problems:
            raise ValueError("; ".join(problems))
        jobs = list(jobs)
        summary = Summary(session=uuid.uuid4().hex)
        self.store.lock()
        try:
            self.store.record("session", session=summary.session, utc=now_utc(),
                              tool_version=_version(), authority=self.authority.as_dict(),
                              scope=scope or {}, date_rules=date_rules or [],
                              pace=asdict(self.control.pace), entry_point=entry_point,
                              run_id=run_id, jobs=len(jobs),
                              user_agent=getattr(self.fetcher, "user_agent", ""),
                              proxy=_proxy_note())
            self._emit("start", total=len(jobs), text=self.authority.note)
            transfer_times, transient, refused = [], 0, 0
            for index, job in enumerate(jobs):
                counts = dict(done=summary.done, failed=summary.failed, skipped=summary.skipped,
                              total=len(jobs), bytes=summary.bytes)
                if self.control.stopped:
                    summary.stopped = "examiner"
                    break
                if not redownload and self.store.latest(job.snap_id, job.role):
                    summary.skipped += 1
                    self._emit("item_skipped", job=job, text="already retrieved", **counts)
                    continue
                avg = sum(transfer_times) / len(transfer_times) if transfer_times else 2.0
                self._emit("item_start", job=job, eta_s=self._eta(len(jobs) - index, avg), **counts)
                outcome = self._run_job(job, summary, decrypt, counts, transfer_times)
                if outcome == "stopped":
                    summary.stopped = "examiner"
                    break
                if outcome == "done":
                    summary.done += 1
                    transient = refused = 0
                else:
                    summary.failed += 1
                    if outcome == "refused":
                        refused += 1
                    else:
                        transient += 1
                pace = self.control.pace
                if transient >= pace.max_failures:
                    summary.stopped = "failures"
                    self._emit("log", text=f"stopped: {transient} failures in a row")
                    break
                if refused >= pace.max_refusals:
                    summary.stopped = "refused"
                    self._emit("log", text=f"stopped: {refused} addresses refused in a row")
                    break
            self.store.record("end", session=summary.session, utc=now_utc(), done=summary.done,
                              failed=summary.failed, skipped=summary.skipped, bytes=summary.bytes,
                              stopped=summary.stopped)
            self._emit("end", done=summary.done, failed=summary.failed, skipped=summary.skipped,
                       total=len(jobs), bytes=summary.bytes, text=summary.stopped)
        finally:
            self.store.unlock()
        return summary

    def _run_job(self, job, summary, decrypt, counts, transfer_times):
        """``done`` / ``refused`` / ``failed`` / ``stopped`` for one job, every attempt recorded."""
        last = "failed"
        for column, url in job.urls:
            attempt = 0
            while True:
                attempt += 1
                if not self._pace_wait(counts):
                    return "stopped"
                pace = self.control.pace
                dest, seq = self.store.next_path("encrypted", job.snap_id, job.role, "bin")
                started_utc, t0 = now_utc(), self.clock()
                self._sent.append(t0)
                self._emit("request", job=job, text=f"{column} attempt {attempt}", **counts)
                try:
                    resp = self.fetcher.fetch(url, timeout=pace.timeout_s, max_bytes=pace.max_bytes,
                                              dest=dest)
                except FetchError as error:
                    rec = self.store.record(
                        "request", session=summary.session, seq=seq, snap_id=job.snap_id,
                        role=job.role, reason=job.reason, url_column=column, url=url,
                        utc_start=started_utc, utc_end=now_utc(), attempt=attempt,
                        http_status=error.status, redirects=error.redirects, error=str(error),
                        authority=self.authority.as_dict(), pace=asdict(pace))
                    summary.records.append(rec)
                    self._emit("item_failed", job=job, text=f"{column}: {error}", **counts)
                    if error.permanent:
                        last = "refused"
                        break                                # next address, if any
                    if attempt >= pace.retries:
                        last = "failed"
                        break
                    backoff = (error.retry_after_s if error.retry_after_s is not None else
                               min(pace.backoff_max_s, pace.backoff_base_s * 2 ** (attempt - 1))
                               + self.rng.uniform(0, pace.jitter_s))
                    if self._wait(backoff, f"back-off after {error}", counts) is False:
                        return "stopped"
                    continue
                transfer_times.append(self.clock() - t0)
                raw = open(resp.path, "rb").read()
                result = decrypt(job, raw) or {}
                decrypted = {"result": result.get("result", "not-decrypted"),
                             "ext": result.get("ext"), "tail_ok": result.get("tail_ok")}
                data = result.get("data")
                if data:
                    out, _seq = self.store.next_path("decrypted", job.snap_id, job.role,
                                                     result.get("ext") or "bin")
                    with open(out, "wb") as fh:
                        fh.write(data)
                    decrypted.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                                     md5=hashlib.md5(data).hexdigest(), stored=self.store.rel(out))
                rec = self.store.record(
                    "request", session=summary.session, seq=seq, snap_id=job.snap_id,
                    role=job.role, reason=job.reason, url_column=column, url=url,
                    final_url=resp.final_url, redirects=resp.redirects, utc_start=started_utc,
                    utc_end=now_utc(), attempt=attempt, http_status=resp.status,
                    headers=resp.headers, bytes=resp.bytes, sha256_encrypted=resp.sha256,
                    md5_encrypted=resp.md5, stored_encrypted=self.store.rel(resp.path),
                    decrypt=decrypted, authority=self.authority.as_dict(), pace=asdict(pace),
                    error=None)
                summary.records.append(rec)
                summary.bytes += resp.bytes
                self._emit("item_done", job=job, bytes=summary.bytes,
                           text=f"{column} {resp.status} {resp.bytes} bytes, {decrypted['result']}",
                           **{k: v for k, v in counts.items() if k != "bytes"})
                return "done"
        return last


def _version():
    try:
        from scripts.app_version import get_version
        return get_version()
    except Exception:                                          # noqa: BLE001
        return ""


def _proxy_note():
    proxies = urllib.request.getproxies()
    return ", ".join(f"{k}={v}" for k, v in sorted(proxies.items())) or "none"


def default_user_agent():
    return f"Snapchat_Auto/{_version() or 'unknown'}"
