#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click==8.3.3",
#     "requests==2.34.2",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""matera-cli — read a Matera (syndic de copropriété) account from the terminal.

Matera publishes no API. Its web app (app.matera.eu) talks to a private JSON API
at api-core.matera.eu authenticated by JWT access tokens that live 15 minutes.
The web app keeps its current access token in IndexedDB and renews it itself, so
this CLI reads that token out of a signed-in Chromium browser and never mints
one. Refresh tokens are single-use: consuming the browser's would sign it out.
Files are served from pre-signed URLs, so only listing needs the token.

Reads: identity, document folders, documents (every file the account can see,
council members included), general assemblies, accounting years and their
general ledger PDF. `sync` mirrors every document into a local folder tree.
"""

from __future__ import annotations

import base64
import json as jsonlib
import logging
import mimetypes
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import click
import requests

log = logging.getLogger("matera")

API = "https://api-core.matera.eu"
UA = "matera-cli (+https://github.com/ClementWalter/matera-cli)"
def default_out() -> Path:
    """$MATERA_DOWNLOAD_DIR (the extension's Download folder setting), else ~/Downloads/matera."""
    value = os.environ.get("MATERA_DOWNLOAD_DIR", "").strip()
    if value and not value.startswith("${"):
        return Path(value).expanduser()
    return Path.home() / "Downloads" / "matera"


DEFAULT_OUT = default_out()
MANIFEST = ".matera-manifest.json"
TIMEOUT = 60
MAX_RETRIES = 3
# A token this close to expiry is treated as expired so a listing never 401s halfway.
EXPIRY_MARGIN = 20

# Chromium browsers whose IndexedDB may hold the app.matera.eu session.
BROWSER_ROOTS = [
    ("chrome", "Library/Application Support/Google/Chrome"),
    ("arc", "Library/Application Support/Arc/User Data"),
    ("brave", "Library/Application Support/BraveSoftware/Brave-Browser"),
    ("edge", "Library/Application Support/Microsoft Edge"),
]
IDB_DIR = "IndexedDB/https_app.matera.eu_0.indexeddb.leveldb"
JWT_RE = re.compile(rb"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")

LOGIN_HINT = (
    "Open or reload https://app.matera.eu in Chrome (or Arc/Brave/Edge), signed in, "
    "then retry: the app renews the 15-minute token this CLI reads."
)


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #
def jwt_claims(token: str) -> dict:
    """Decode a JWT payload without verifying it (the server verifies)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = jsonlib.loads(base64.urlsafe_b64decode(payload))
        return claims if isinstance(claims, dict) else {}
    except (IndexError, ValueError):
        return {}


def is_access_token(claims: dict) -> bool:
    return claims.get("aud") == "commonhold" and "building_id" in claims


def newest_access_token(blob: bytes, now: float | None = None) -> str | None:
    """Pick the unexpired access token with the latest expiry in a raw IndexedDB dump."""
    now = time.time() if now is None else now
    best, best_exp = None, now + EXPIRY_MARGIN
    for match in JWT_RE.finditer(blob):
        token = match.group().decode()
        claims = jwt_claims(token)
        if is_access_token(claims) and claims.get("exp", 0) > best_exp:
            best, best_exp = token, claims["exp"]
    return best


def browser_access_tokens(home: Path | None = None, now: float | None = None):
    """Yield (label, access_token) for each browser profile holding a live Matera token."""
    home = home or Path.home()
    for label, root in BROWSER_ROOTS:
        base = home / root
        if not base.is_dir():
            continue
        for profile in sorted(p for p in base.iterdir() if p.is_dir()):
            idb = profile / IDB_DIR
            if not idb.is_dir():
                continue
            # .log holds recent writes uncompressed; .ldb may hold older ones.
            files = sorted(idb.glob("*.log")) + sorted(idb.glob("*.ldb"))
            try:
                blob = b"".join(f.read_bytes() for f in files)
            except OSError:
                continue
            token = newest_access_token(blob, now)
            if token:
                yield f"{label}:{profile.name}", token


def browser_token() -> tuple[str, str]:
    """(source, token): $MATERA_ACCESS_TOKEN, else the freshest browser token."""
    env = os.environ.get("MATERA_ACCESS_TOKEN", "").strip()
    if env and not env.startswith("${"):
        return "env", env
    found = list(browser_access_tokens())
    if not found:
        raise click.ClickException(f"No live Matera token in any local browser. {LOGIN_HINT}")
    return max(found, key=lambda lt: jwt_claims(lt[1]).get("exp", 0))


# --------------------------------------------------------------------------- #
# HTTP client
# --------------------------------------------------------------------------- #
class Matera:
    """Bearer-token client over a borrowed browser access token."""

    def __init__(self, access_token: str, source: str = "", session: requests.Session | None = None):
        self.access_token = access_token
        self.source = source
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept": "application/json", "Origin": "https://app.matera.eu"})

    def _send(self, method: str, url: str, **kw) -> requests.Response:
        """One request with bounded retries on 429/5xx, honoring Retry-After."""
        for attempt in range(MAX_RETRIES + 1):
            r = self.s.request(method, url, timeout=TIMEOUT, **kw)
            if r.status_code != 429 and r.status_code < 500:
                return r
            if attempt == MAX_RETRIES:
                return r
            wait = r.headers.get("Retry-After")
            delay = float(wait) if wait and wait.isdigit() else 2 ** attempt
            log.warning("%s %s → %s, retrying in %.0fs", method, url, r.status_code, delay)
            time.sleep(delay)
        return r

    def get(self, path: str, params: dict | None = None) -> requests.Response:
        url = path if path.startswith("http") else API + path
        r = self._send("GET", url, params=params, headers={"Authorization": f"Bearer {self.access_token}"})
        if r.status_code == 401:
            raise click.ClickException(f"401 — Matera rejected the browser token. {LOGIN_HINT}")
        if r.status_code == 403:
            raise click.ClickException(f"403 — this Matera account may not read {path}.")
        if not r.ok:
            raise click.ClickException(f"{r.status_code} — GET {path} failed: {r.text[:200]}")
        return r

    def json(self, path: str, params: dict | None = None) -> dict:
        return self.get(path, params).json()

    def results(self, path: str, params: dict | None = None) -> list[dict]:
        """A list endpoint's results; fails loudly if the server says more pages exist."""
        body = self.json(path, params)
        meta = body.get("meta") or {}
        if meta.get("has_next_page"):
            raise click.ClickException(
                f"{path} returned a partial page ({meta.get('count')}/{meta.get('total_count')}); "
                "pagination is not implemented for it yet."
            )
        return body.get("results") or []

    def download(self, url: str) -> bytes:
        """Fetch a file: ActiveStorage blob URLs redirect to pre-signed S3, no token needed."""
        r = self._send("GET", url, allow_redirects=True)
        if not r.ok:
            raise click.ClickException(f"{r.status_code} — file download failed")
        return r.content


def get_client() -> Matera:
    source, token = browser_token()
    return Matera(token, source)


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #
def safe_name(name: str) -> str:
    """A folder/file name valid on macOS, Linux and Google Drive, in NFC form.

    NFC matters: macOS treats NFC and NFD spellings of one name as the same file.
    """
    cleaned = re.sub(r'[/\\:\0]', "-", unicodedata.normalize("NFC", name or "")).strip().rstrip(".")
    return cleaned or "_"


def with_extension(name: str, content_type: str | None) -> str:
    """Append the MIME type's extension when the name has none (Drive would add it anyway)."""
    if re.search(r"\.[A-Za-z0-9]{1,5}$", name) or not content_type:
        return name
    ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
    return name + ext if ext else name


def folder_resolver(folders: list[dict]):
    """Return fid → 'Parent/Child' path; folders the API hides become '_dossier-<id>'."""
    by_id = {f["id"]: f for f in folders}
    cache: dict[int, str] = {}

    def path(fid: int, depth: int = 0) -> str:
        if fid in cache:
            return cache[fid]
        node = by_id.get(fid)
        if node is None or depth > 50:
            return f"_dossier-{fid}"
        name = safe_name(node["name"])
        parent = node.get("parent_id")
        cache[fid] = f"{path(parent, depth + 1)}/{name}" if parent else name
        return cache[fid]

    return path


def normalize_documents(docs: list[dict], folders: list[dict]) -> list[dict]:
    """Stable records with the folder path resolved; file URLs are never exposed."""
    resolve = folder_resolver(folders)
    out = []
    for d in docs:
        f = d.get("file") or {}
        fid = d.get("folder_id")
        out.append({
            "id": d["id"],
            "name": d.get("name"),
            "file_name": f.get("file_name") or d.get("name"),
            "kind": d.get("kind"),
            "folder_id": fid,
            "folder": resolve(fid) if fid else "",
            "created_at": d.get("created_at"),
            "updated_at": d.get("updated_at"),
            "size": f.get("size"),
            "content_type": f.get("content_type"),
            "visible_by_owners": d.get("visible_by_owners"),
        })
    return sorted(out, key=lambda r: (r["folder"], r["created_at"] or "", r["id"]))


def filter_documents(docs: list[dict], folder=None, match=None, kind=None, since=None, owners_only=False) -> list[dict]:
    """Client-side filters: the API's own search only matches names."""
    def keep(d):
        if folder and folder.lower() not in d["folder"].lower():
            return False
        if match and match.lower() not in (d["name"] or "").lower():
            return False
        if kind and d["kind"] != kind:
            return False
        if since and (d["created_at"] or "")[:10] < since:
            return False
        return not (owners_only and not d["visible_by_owners"])
    return [d for d in docs if keep(d)]


def local_path(doc: dict, taken: set[str]) -> str:
    """Relative path for a document; same-named files get their id appended."""
    name = with_extension(safe_name(doc["file_name"] or str(doc["id"])), doc["content_type"])
    rel = f"{doc['folder']}/{name}" if doc["folder"] else f"_sans-dossier/{name}"
    if rel.lower() in taken:
        stem, dot, ext = name.rpartition(".")
        name = f"{stem} ({doc['id']}).{ext}" if dot else f"{name} ({doc['id']})"
        rel = f"{doc['folder'] or '_sans-dossier'}/{name}"
    taken.add(rel.lower())
    return rel


def read_manifest(output: Path) -> dict:
    """Previous sync's {id: {path, updated_at}}; unreadable means start fresh."""
    try:
        data = jsonlib.loads((output / MANIFEST).read_text())
        return {k: v for k, v in data.items() if isinstance(v, dict)}
    except (OSError, ValueError):
        return {}


def plan_sync(docs: list[dict], output: Path, previous: dict) -> list[dict]:
    """Decide per document whether it is already on disk or must be downloaded.

    Matera's declared size is sometimes off by a few hundred bytes, so a file
    the manifest recorded for the same path and revision also counts as present.
    """
    taken: set[str] = set()
    plan = []
    for d in docs:
        rel = local_path(d, taken)
        dest = output / rel
        prev = previous.get(str(d["id"])) or {}
        same_revision = prev.get("path") == rel and prev.get("updated_at") == d["updated_at"]
        present = dest.exists() and (dest.stat().st_size == d["size"] or same_revision)
        plan.append({"id": d["id"], "path": rel, "size": d["size"], "updated_at": d["updated_at"],
                     "status": "present" if present else "download"})
    return plan


def fmt_size(n) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def emit(data, as_json: bool, render) -> None:
    if as_json:
        click.echo(jsonlib.dumps(data, indent=2, ensure_ascii=False))
    else:
        render(data)


def fetch_documents(client: Matera) -> list[dict]:
    folders = client.results("/api/v1/document_folders")
    docs = client.results("/api/v1/documents", {"view": "extended", "includes[file]": "true"})
    return normalize_documents(docs, folders)


def document_index(client: Matera) -> tuple[dict[int, dict], dict[int, dict]]:
    """(normalized by id, raw by id): raw records carry the download URL."""
    folders = client.results("/api/v1/document_folders")
    raw = client.results("/api/v1/documents", {"view": "extended", "includes[file]": "true"})
    norm = normalize_documents(raw, folders)
    return {d["id"]: d for d in norm}, {d["id"]: d for d in raw}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
JSON_OPT = click.option("--json", "as_json", is_flag=True, help="Output JSON.")
LIMIT_OPT = click.option("--limit", type=int, default=None, help="Show at most N rows.")


@click.group(context_settings={"help_option_names": ["-h", "--help"]},
             epilog="\b\nExamples:\n  matera connect\n  matera documents --folder assemblées\n  matera sync --output ~/Matera")
@click.option("--debug", is_flag=True, help="Log HTTP details to stderr.")
def cli(debug):
    """Read a Matera copropriété account: documents, assemblies, accounts."""
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, format="%(message)s", stream=sys.stderr)


@cli.command(epilog="\b\nExamples:\n  matera connect\n  matera connect --json")
@JSON_OPT
def connect(as_json):
    """Check that a signed-in browser holds a live Matera token."""
    client = get_client()
    ctx = client.json("/api/v1/context")["result"]
    exp = jwt_claims(client.access_token).get("exp", 0)
    data = {"connected": True, "source": client.source, "person": (ctx.get("person") or {}).get("full_name"),
            "token_expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(exp))}
    emit(data, as_json, lambda d: click.echo(f"connected\t{d['person']}\t{d['source']}\texpires {d['token_expires_at']}"))


@cli.command(epilog="\b\nExamples:\n  matera whoami\n  matera whoami --json")
@JSON_OPT
def whoami(as_json):
    """The signed-in person, their role and building."""
    ctx = get_client().json("/api/v1/context")["result"]
    person, building = ctx.get("person") or {}, ctx.get("building") or {}
    data = {
        "person_id": person.get("id"), "name": person.get("full_name"), "role": person.get("role"),
        "access_level": person.get("access_level"), "email": (ctx.get("user") or {}).get("email"),
        "building_id": building.get("id"), "management_type": building.get("management_type"),
    }
    emit(data, as_json, lambda d: click.echo(f"{d['name']}\t{d['role']}/{d['access_level']}\tbuilding {d['building_id']}\t{d['email']}"))


@cli.command(epilog="\b\nExamples:\n  matera folders\n  matera folders --json")
@JSON_OPT
@LIMIT_OPT
def folders(as_json, limit):
    """Document folders with their full path and document count."""
    client = get_client()
    docs = fetch_documents(client)
    raw = client.results("/api/v1/document_folders")
    resolve = folder_resolver(raw)
    counts: dict = {}
    for d in docs:
        counts[d["folder_id"]] = counts.get(d["folder_id"], 0) + 1
    rows = sorted(({"id": f["id"], "path": resolve(f["id"]), "kind": f.get("kind"), "documents": counts.get(f["id"], 0)}
                   for f in raw), key=lambda r: r["path"])[:limit]
    emit(rows, as_json, lambda rs: [click.echo(f"{r['documents']:>4}\t{r['path']}\t{r['id']}") for r in rs])


@cli.command(epilog="\b\nExamples:\n  matera documents --folder 'Mes assemblées'\n  matera documents --match 'PV AG' --json\n  matera documents --kind assembly_minutes --since 2025-01-01")
@click.option("--folder", help="Keep documents whose folder path contains this text (case-insensitive).")
@click.option("--match", help="Keep documents whose name contains this text (case-insensitive).")
@click.option("--kind", help="Exact document kind, e.g. assembly_minutes, fund_call, quotation.")
@click.option("--since", metavar="YYYY-MM-DD", help="Created on or after this date.")
@click.option("--owners-only", is_flag=True, help="Only documents visible to every co-owner.")
@JSON_OPT
@LIMIT_OPT
def documents(folder, match, kind, since, owners_only, as_json, limit):
    """List every document the account can see (filters are client-side)."""
    rows = filter_documents(fetch_documents(get_client()), folder, match, kind, since, owners_only)[:limit]
    emit(rows, as_json, lambda rs: [click.echo(
        f"{r['id']}\t{(r['created_at'] or '')[:10]}\t{fmt_size(r['size'])}\t{r['kind']}\t{r['folder']}/{r['name']}") for r in rs])


@cli.command(epilog="\b\nExamples:\n  matera download 7521821\n  matera download 7521821 7984796 --target-dir ~/Matera/PV\n  matera download 7521821 -o pv-2024.pdf --json")
@click.argument("ids", nargs=-1, type=int, required=True)
@click.option("--target-dir", type=click.Path(file_okay=False, path_type=Path), default=DEFAULT_OUT, show_default=True)
@click.option("-o", "--output", type=click.Path(dir_okay=False, path_type=Path), help="Exact file path (single id only).")
@JSON_OPT
def download(ids, target_dir, output, as_json):
    """Download documents by id; prints the absolute path of each file."""
    if output and len(ids) != 1:
        raise click.BadParameter("-o takes exactly one id, e.g. `matera download 7521821 -o pv.pdf`.")
    client = get_client()
    norm, raw = document_index(client)
    missing = [i for i in ids if i not in raw]
    if missing:
        raise click.ClickException(f"Unknown document id(s): {missing}. List them with `matera documents --match TEXT`.")
    out = []
    for i in ids:
        dest = output or target_dir / with_extension(safe_name(norm[i]["file_name"]), norm[i]["content_type"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(client.download(raw[i]["file"]["url"]))
        out.append({"id": i, "path": str(dest.resolve())})
    emit(out, as_json, lambda rs: [click.echo(f"{r['id']}\t{r['path']}") for r in rs])


@cli.command(epilog="\b\nExamples:\n  matera sync --output ~/Matera --dry-run\n  matera sync --output ~/Matera --folder 'Mes assemblées'\n  matera sync --output ~/Matera --json")
@click.option("--output", type=click.Path(file_okay=False, path_type=Path), default=DEFAULT_OUT, show_default=True)
@click.option("--folder", help="Only folders whose path contains this text.")
@click.option("--owners-only", is_flag=True, help="Only documents visible to every co-owner.")
@click.option("--dry-run", is_flag=True, help="List what would be downloaded; write nothing.")
@click.option("--workers", type=int, default=6, show_default=True)
@JSON_OPT
def sync(output, folder, owners_only, dry_run, workers, as_json):
    """Mirror every document into OUTPUT, keeping Matera's folder tree.

    Idempotent: a file is skipped when its size matches, or when the manifest
    (.matera-manifest.json) says this document revision was already saved there.
    """
    client = get_client()
    norm, raw = document_index(client)
    docs = filter_documents(sorted(norm.values(), key=lambda d: (d["folder"], d["id"])), folder, owners_only=owners_only)
    previous = read_manifest(output)
    plan = plan_sync(docs, output, previous)
    todo = [p for p in plan if p["status"] == "download"]
    log.info("%d documents, %d to download (%s).", len(plan), len(todo), fmt_size(sum(p["size"] or 0 for p in todo)))
    if not dry_run:
        def fetch(p):
            dest = output / p["path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".part")
            tmp.write_bytes(client.download(raw[p["id"]]["file"]["url"]))
            tmp.replace(dest)
            return p
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fetch, p): p for p in todo}
            for n, fut in enumerate(as_completed(futures), 1):
                p = futures[fut]
                try:
                    fut.result()
                    p["status"] = "downloaded"
                except (click.ClickException, requests.RequestException, OSError) as exc:
                    p["status"] = f"failed: {exc}"
                    log.warning("failed %s: %s", p["path"], exc)
                if n % 50 == 0:
                    log.info("%d/%d", n, len(todo))
        output.mkdir(parents=True, exist_ok=True)
        saved = {str(p["id"]): {"path": p["path"], "updated_at": p["updated_at"]}
                 for p in plan if p["status"] in ("present", "downloaded")}
        (output / MANIFEST).write_text(jsonlib.dumps(
            previous | saved, indent=1, ensure_ascii=False, sort_keys=True))
    failed = [p for p in plan if p["status"].startswith("failed")]
    summary = {"output": str(output.resolve()), "documents": len(plan),
               "downloaded": sum(p["status"] == "downloaded" for p in plan),
               "present": sum(p["status"] == "present" for p in plan),
               "to_download": len(todo) if dry_run else 0, "failed": failed}
    emit(summary, as_json, lambda s: click.echo(
        f"{s['output']}\tdocuments={s['documents']}\tdownloaded={s['downloaded']}\tpresent={s['present']}"
        f"\tto_download={s['to_download']}\tfailed={len(s['failed'])}"))
    if failed:
        raise click.ClickException(f"{len(failed)} download(s) failed; re-run `matera sync` to retry them.")


@cli.command(epilog="\b\nExamples:\n  matera assemblies\n  matera assemblies --json")
@JSON_OPT
def assemblies(as_json):
    """General assemblies (AG): date, kind and status."""
    rows = sorted(({"id": a["id"], "date": a.get("date"), "kind": a.get("kind"), "status": a.get("status")}
                   for a in get_client().results("/api/v1/assemblies")), key=lambda a: a["date"] or "")
    emit(rows, as_json, lambda rs: [click.echo(f"{r['id']}\t{(r['date'] or '')[:16]}\t{r['kind']}\t{r['status']}") for r in rs])


@cli.command(epilog="\b\nExamples:\n  matera exercices\n  matera exercices --json")
@JSON_OPT
def exercices(as_json):
    """Accounting years (exercices) and their approval status."""
    rows = sorted(({"id": e["id"], "year": e.get("year"), "start": e.get("start_date"), "end": e.get("end_date"),
                    "status": e.get("status"), "approved_at": e.get("approved_at")}
                   for e in get_client().results("/api/v1/exercices")), key=lambda e: e["start"] or "")
    emit(rows, as_json, lambda rs: [click.echo(f"{r['id']}\t{r['year']}\t{r['status']}\t{r['approved_at'] or ''}") for r in rs])


@cli.command(epilog="\b\nExamples:\n  matera ledger 2025\n  matera ledger 2025 -o grand-livre-2025.pdf --json")
@click.argument("year")
@click.option("-o", "--output", type=click.Path(dir_okay=False, path_type=Path), help="Output PDF path.")
@JSON_OPT
def ledger(year, output, as_json):
    """Download the general ledger (grand livre) PDF of an accounting year."""
    client = get_client()
    years = {e.get("year"): e["id"] for e in client.results("/api/v1/exercices")}
    if year not in years:
        raise click.ClickException(f"No exercice {year}; known years: {', '.join(sorted(years))}.")
    dest = output or DEFAULT_OUT / f"grand-livre-{year}.pdf"
    dest.parent.mkdir(parents=True, exist_ok=True)
    pdf = client.get(f"/api/exercices/{years[year]}/download_ledger.pdf").content
    if not pdf.startswith(b"%PDF"):
        raise click.ClickException(f"Exercice {year}: the ledger endpoint did not return a PDF.")
    dest.write_bytes(pdf)
    emit({"year": year, "path": str(dest.resolve())}, as_json, lambda d: click.echo(d["path"]))


if __name__ == "__main__":
    cli()
