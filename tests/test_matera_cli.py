#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click==8.3.3",
#     "requests==2.34.2",
#     "pytest==9.1.1",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Offline tests for matera-cli: token discovery, normalization, sync planning, CLI output."""

import base64
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

NOW = 2_000_000_000


@pytest.fixture(scope="module")
def m():
    path = Path(__file__).resolve().parents[1] / "matera_cli.py"
    loader = importlib.machinery.SourceFileLoader("matera_cli", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def jwt(claims: dict) -> str:
    """A syntactically valid, unsigned JWT carrying synthetic claims."""
    def enc(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'HS256'})}.{enc(claims)}.c2lnbmF0dXJl"


@pytest.fixture
def access_old():
    return jwt({"aud": "commonhold", "building_id": 1, "exp": NOW + 300})


@pytest.fixture
def access_new():
    return jwt({"aud": "commonhold", "building_id": 1, "exp": NOW + 800})


@pytest.fixture
def refresh():
    return jwt({"aud": "https://api-core.matera.eu/api/v1/auth/refresh_token", "exp": NOW + 10**6})


@pytest.fixture
def folders():
    return [
        {"id": 1, "name": "Mes assemblées", "parent_id": None},
        {"id": 2, "name": "AG 2026", "parent_id": 1},
        {"id": 3, "name": "Orphan child", "parent_id": 99},
        {"id": 4, "name": "A/B: C", "parent_id": None},
    ]


@pytest.fixture
def raw_docs():
    return [
        {"id": 10, "name": "PV AG.pdf", "kind": "assembly_minutes", "folder_id": 2,
         "created_at": "2025-11-05T10:00:00.000+01:00", "updated_at": "2025-11-05T10:00:00.000+01:00",
         "visible_by_owners": True,
         "file": {"file_name": "PV AG.pdf", "size": 100, "content_type": "application/pdf",
                  "url": "https://api-core.example.com/blob/10"}},
        {"id": 11, "name": "Facture", "kind": "other", "folder_id": 4,
         "created_at": "2024-01-02T10:00:00.000+01:00", "updated_at": "2024-01-02T10:00:00.000+01:00",
         "visible_by_owners": False,
         "file": {"file_name": "Facture", "size": 50, "content_type": "application/pdf",
                  "url": "https://api-core.example.com/blob/11"}},
    ]


@pytest.fixture
def docs(m, raw_docs, folders):
    return m.normalize_documents(raw_docs, folders)


# --- tokens -----------------------------------------------------------------

def test_jwt_claims_decodes_payload(m, access_new):
    assert m.jwt_claims(access_new)["exp"] == NOW + 800


def test_jwt_claims_tolerates_garbage(m):
    assert m.jwt_claims("not-a-jwt") == {}


def test_newest_access_token_picks_latest_expiry(m, access_old, access_new, refresh):
    blob = b"\x00".join(t.encode() for t in (access_old, refresh, access_new))
    assert m.newest_access_token(blob, now=NOW) == access_new


def test_newest_access_token_ignores_refresh_tokens(m, refresh):
    assert m.newest_access_token(refresh.encode(), now=NOW) is None


def test_newest_access_token_ignores_expired(m, access_old):
    assert m.newest_access_token(access_old.encode(), now=NOW + 1000) is None


def test_browser_access_tokens_reads_profile_indexeddb(m, tmp_path, access_new):
    idb = tmp_path / "Library/Application Support/Google/Chrome/Default" / m.IDB_DIR
    idb.mkdir(parents=True)
    (idb / "000003.log").write_bytes(b"\x00junk" + access_new.encode())
    assert list(m.browser_access_tokens(home=tmp_path, now=NOW)) == [("chrome:Default", access_new)]


def test_browser_token_prefers_env(m, monkeypatch):
    monkeypatch.setenv("MATERA_ACCESS_TOKEN", "synthetic")
    assert m.browser_token() == ("env", "synthetic")


def test_browser_token_ignores_unfilled_extension_setting(m, monkeypatch):
    monkeypatch.setenv("MATERA_ACCESS_TOKEN", "${user_config.token}")
    monkeypatch.setattr(m, "browser_access_tokens", lambda: iter(()))
    with pytest.raises(click.ClickException):
        m.browser_token()


@pytest.mark.parametrize("value, expected", [
    ("/tmp/matera-out", Path("/tmp/matera-out")),
    ("${user_config.download_dir}", Path.home() / "Downloads" / "matera"),
    ("", Path.home() / "Downloads" / "matera"),
])
def test_default_out(m, monkeypatch, value, expected):
    monkeypatch.setenv("MATERA_DOWNLOAD_DIR", value)
    assert m.default_out() == expected


# --- normalization ----------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("A/B: C", "A-B- C"),
    ("trailing. ", "trailing"),
    ("", "_"),
    ("été", "été"),
])
def test_safe_name(m, raw, expected):
    assert m.safe_name(raw) == expected


@pytest.mark.parametrize("name, ctype, expected", [
    ("Facture", "application/pdf", "Facture.pdf"),
    ("photo.png", "image/png", "photo.png"),
    ("EPALZEO -", "application/pdf", "EPALZEO -.pdf"),
    ("notes", None, "notes"),
])
def test_with_extension(m, name, ctype, expected):
    assert m.with_extension(name, ctype) == expected


@pytest.mark.parametrize("fid, expected", [
    (2, "Mes assemblées/AG 2026"),
    (3, "_dossier-99/Orphan child"),
    (4, "A-B- C"),
])
def test_folder_resolver(m, folders, fid, expected):
    assert m.folder_resolver(folders)(fid) == expected


def test_normalize_never_exposes_file_url(m, docs):
    assert "blob" not in json.dumps(docs)


def test_normalize_sorts_by_folder(m, docs):
    assert [d["id"] for d in docs] == [11, 10]


@pytest.mark.parametrize("kwargs, ids", [
    ({"folder": "assemblées"}, [10]),
    ({"match": "facture"}, [11]),
    ({"kind": "assembly_minutes"}, [10]),
    ({"since": "2025-01-01"}, [10]),
    ({"owners_only": True}, [10]),
])
def test_filter_documents(m, docs, kwargs, ids):
    assert [d["id"] for d in m.filter_documents(docs, **kwargs)] == ids


def test_local_path_appends_id_on_collision(m):
    taken = set()
    doc = {"id": 7, "file_name": "a.pdf", "folder": "F", "content_type": "application/pdf"}
    m.local_path(doc, taken)
    assert m.local_path(doc, taken) == "F/a (7).pdf"


def test_local_path_collision_is_unicode_insensitive(m):
    taken = set()
    m.local_path({"id": 1, "file_name": "copropriété.pdf", "folder": "F", "content_type": None}, taken)
    nfd = {"id": 2, "file_name": "copropriété.pdf", "folder": "F", "content_type": None}
    assert m.local_path(nfd, taken) == "F/copropriété (2).pdf"


# --- sync planning ----------------------------------------------------------

def test_plan_marks_missing_file_for_download(m, docs, tmp_path):
    assert {p["status"] for p in m.plan_sync(docs, tmp_path, {})} == {"download"}


def test_plan_skips_file_with_matching_size(m, docs, tmp_path):
    (tmp_path / "Mes assemblées/AG 2026").mkdir(parents=True)
    (tmp_path / "Mes assemblées/AG 2026/PV AG.pdf").write_bytes(b"x" * 100)
    plan = {p["id"]: p["status"] for p in m.plan_sync(docs, tmp_path, {})}
    assert plan[10] == "present"


def test_plan_trusts_manifest_when_declared_size_is_wrong(m, docs, tmp_path):
    (tmp_path / "A-B- C").mkdir()
    (tmp_path / "A-B- C/Facture.pdf").write_bytes(b"x" * 49)
    previous = {"11": {"path": "A-B- C/Facture.pdf", "updated_at": "2024-01-02T10:00:00.000+01:00"}}
    plan = {p["id"]: p["status"] for p in m.plan_sync(docs, tmp_path, previous)}
    assert plan[11] == "present"


def test_plan_redownloads_new_revision(m, docs, tmp_path):
    (tmp_path / "A-B- C").mkdir()
    (tmp_path / "A-B- C/Facture.pdf").write_bytes(b"x" * 49)
    previous = {"11": {"path": "A-B- C/Facture.pdf", "updated_at": "2023-01-01T00:00:00.000+01:00"}}
    plan = {p["id"]: p["status"] for p in m.plan_sync(docs, tmp_path, previous)}
    assert plan[11] == "download"


def test_read_manifest_tolerates_missing_file(m, tmp_path):
    assert m.read_manifest(tmp_path) == {}


# --- CLI --------------------------------------------------------------------

class StubClient:
    """Answers the endpoints the commands call from canned data."""

    def __init__(self, folders, raw_docs):
        self.access_token = jwt({"aud": "commonhold", "building_id": 1, "exp": NOW})
        self.source = "chrome:Default"
        self.data = {
            "/api/v1/document_folders": folders,
            "/api/v1/documents": raw_docs,
            "/api/v1/assemblies": [{"id": 5, "date": "2026-12-02T13:00:00.000+01:00", "kind": "ordinary", "status": "agenda_sent"}],
            "/api/v1/exercices": [{"id": 6, "year": "2025", "start_date": "2025-01-01", "end_date": "2025-12-31", "status": "closed", "approved_at": None}],
        }

    def results(self, path, params=None):
        return self.data[path]

    def json(self, path, params=None):
        return {"result": {"person": {"full_name": "Someone Example", "role": "council", "access_level": "extended"},
                           "user": {"email": "someone@example.com"}, "building": {"id": 1, "management_type": "professional"}}}

    def download(self, url):
        return b"%PDF-stub"


@pytest.fixture
def run(m, monkeypatch, folders, raw_docs):
    stub = StubClient(folders, raw_docs)
    monkeypatch.setattr(m, "get_client", lambda: stub)
    return lambda *args: CliRunner().invoke(m.cli, list(args))


@pytest.mark.parametrize("args", [
    ["whoami", "--json"],
    ["folders", "--json"],
    ["documents", "--json"],
    ["assemblies", "--json"],
    ["exercices", "--json"],
    ["connect", "--json"],
])
def test_json_output_parses(run, args):
    assert isinstance(json.loads(run(*args).output), (list, dict))


def test_documents_limit(run):
    assert len(json.loads(run("documents", "--json", "--limit", "1").output)) == 1


def test_download_unknown_id_names_fix(run):
    assert "matera documents --match" in run("download", "999").output


def test_download_writes_file(run, tmp_path):
    out = json.loads(run("download", "10", "--target-dir", str(tmp_path), "--json").output)
    assert Path(out[0]["path"]).read_bytes() == b"%PDF-stub"


def test_sync_writes_manifest(run, tmp_path):
    run("sync", "--output", str(tmp_path), "--json")
    assert set(json.loads((tmp_path / ".matera-manifest.json").read_text())) == {"10", "11"}


def test_sync_dry_run_writes_nothing(run, tmp_path):
    run("sync", "--output", str(tmp_path), "--dry-run")
    assert list(tmp_path.iterdir()) == []


def test_ledger_unknown_year_lists_known(run):
    assert "known years: 2025" in run("ledger", "1999").output


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
