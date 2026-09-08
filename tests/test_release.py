"""Release decisions and publication must fail closed around immutable artifacts."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
from urllib.error import HTTPError, URLError
import zipfile

import pytest
import yaml


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/release.py"
spec = importlib.util.spec_from_file_location("release_helper", SCRIPT)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
VERSION = "0.12.0"
TAG = "v" + VERSION
PROJECT = b'[project]\nname = "knowledge-gateway"\nversion = "0.12.0"\n'
SOURCE = {"gateway/__init__.py": b'VALUE = "original"\n'}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "config", "user.name", "Release Test"], check=True)
    Path("pyproject.toml").write_bytes(PROJECT)
    Path("gateway").mkdir()
    Path("gateway/__init__.py").write_bytes(SOURCE["gateway/__init__.py"])
    commit()
    return tmp_path


def commit():
    release.git("add", ".")
    release.git("commit", "-qm", "Test source")
    return release.git("rev-parse", "HEAD").decode().strip()


def artifacts(sources=None, project=PROJECT, extra=None):
    sources = SOURCE if sources is None else sources
    result = {}
    wheel = io.BytesIO()
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in {**sources, **(extra or {})}.items():
            archive.writestr(name, content)
    result[f"knowledge_gateway-{VERSION}-py3-none-any.whl"] = wheel.getvalue()
    sdist = io.BytesIO()
    with tarfile.open(fileobj=sdist, mode="w:gz") as archive:
        for name, content in {**sources, "pyproject.toml": project, **(extra or {})}.items():
            info = tarfile.TarInfo(f"knowledge_gateway-{VERSION}/{name}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    result[f"knowledge_gateway-{VERSION}.tar.gz"] = sdist.getvalue()
    return result


def metadata(blobs):
    return {"info": {"version": VERSION}, "urls": [
        {"filename": name, "packagetype": "bdist_wheel" if name.endswith(".whl") else "sdist",
         "digests": {"sha256": hashlib.sha256(data).hexdigest()}, "yanked": False,
         "url": "https://files.pythonhosted.org/packages/" + name}
        for name, data in blobs.items()
    ]}


def published(data, *, draft=False):
    return {"draft": draft, "tag_name": TAG, "body": "Preserved release notes", "assets": [
        {"name": item["filename"], "digest": "sha256:" + item["digests"]["sha256"]}
        for item in data["urls"]
    ]}


class FakeClient:
    def __init__(self, data=None, gh_release=None, blobs=None):
        self.data = data
        self.gh_release = gh_release
        self.blobs = blobs or {}

    def pypi(self, version):
        assert version == VERSION
        return self.data

    def release(self, tag):
        assert tag == TAG
        return self.gh_release

    def request(self, url, *, missing=False):
        return self.blobs[url.rsplit("/", 1)[1]]


def test_fresh_release(repo):
    plan = release.decision(FakeClient(), "workflow_run")
    assert plan == {"version": VERSION, "tag": TAG, "release": True,
                    "source_sha": release.git("rev-parse", "HEAD").decode().strip()}


def test_complete_release_noop(repo):
    release.git("tag", TAG)
    data = metadata(artifacts())
    assert not release.decision(FakeClient(data, published(data)), "workflow_run")["release"]


@pytest.mark.parametrize("dry_run", [True, False])
def test_retry_pins_tag_source(repo, dry_run):
    original = release.git("rev-parse", "HEAD").decode().strip()
    release.git("tag", TAG)
    Path("gateway/__init__.py").write_text("later = True\n")
    later = commit()
    plan = release.decision(FakeClient(), "workflow_run", dry_run=dry_run)
    assert plan["release"] and plan["source_sha"] == original
    assert release.git("rev-parse", "HEAD").decode().strip() == (later if dry_run else original)


def test_dry_run_does_not_write_workflow_files(repo, monkeypatch, capsys):
    monkeypatch.setattr(release, "Client", lambda _: FakeClient())
    monkeypatch.setenv("GITHUB_OUTPUT", str(repo / "output"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(repo / "summary"))
    monkeypatch.setattr("sys.argv", [str(SCRIPT), "decision", "--dry-run"])
    assert release.main() == 0
    assert json.loads(capsys.readouterr().out)["release"]
    assert not (repo / "output").exists() and not (repo / "summary").exists()


def test_pypi_without_tag_is_not_source_evidence(repo):
    with pytest.raises(release.ReleaseError, match="investigate the original source"):
        release.decision(FakeClient(metadata(artifacts())), "workflow_run")


def test_manual_tag_mismatch(repo):
    with pytest.raises(release.ReleaseError, match="Manual tag"):
        release.decision(FakeClient(), "push", "v0.11.0")


def test_tagged_version_must_match(repo):
    release.git("tag", "v0.13.0")
    Path("pyproject.toml").write_bytes(PROJECT.replace(b"0.12.0", b"0.13.0"))
    commit()
    client = FakeClient()
    client.pypi = lambda _: None
    client.release = lambda _: None
    with pytest.raises(release.ReleaseError, match="Tagged pyproject"):
        release.decision(client, "workflow_run")


@pytest.mark.parametrize("kind", ["draft", "missing", "mismatch"])
def test_existing_release_decisions(repo, kind):
    release.git("tag", TAG)
    data = metadata(artifacts())
    existing = published(data, draft=kind == "draft")
    if kind == "missing":
        existing["assets"].pop()
    if kind in {"mismatch", "draft"}:
        existing["assets"][0]["digest"] = "sha256:" + "0" * 64
    if kind == "mismatch":
        with pytest.raises(release.ReleaseError, match="refusing replacement"):
            release.decision(FakeClient(data, existing), "workflow_run")
    else:
        assert release.decision(FakeClient(data, existing), "workflow_run")["release"]


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_http_failures_sanitized(monkeypatch, status):
    monkeypatch.setenv("GH_TOKEN", "secret-value")
    client = release.Client("owner/repo")
    def fail(*args, **kwargs):
        raise HTTPError(client.base, status, "secret-value", {}, io.BytesIO(b"private body"))
    monkeypatch.setattr(client.opener, "open", fail)
    with pytest.raises(release.ReleaseError) as error:
        client.release(TAG)
    assert str(error.value) == f"api.github.com: HTTP {status}"


def test_network_failure_sanitized(monkeypatch):
    client = release.Client("owner/repo")
    def fail(*args, **kwargs):
        raise URLError("private-network-detail")
    monkeypatch.setattr(client.opener, "open", fail)
    with pytest.raises(release.ReleaseError, match="network request failed") as error:
        client.pypi(VERSION)
    assert "private" not in str(error.value)


def test_missing_github_release_requires_collection_access(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "secret-value")
    client = release.Client("owner/repo")
    calls = []
    def respond(request, **kwargs):
        calls.append(request)
        if len(calls) == 1:
            return io.BytesIO(b"[]")
        raise HTTPError(request.full_url, 404, "private", {}, None)
    monkeypatch.setattr(client.opener, "open", respond)
    assert client.release(TAG) is None
    assert calls[0].full_url.endswith("?per_page=1")
    assert all(request.get_header("Authorization") == "Bearer secret-value" for request in calls)
    calls.clear()
    def deny(request, **kwargs):
        raise HTTPError(request.full_url, 404, "private", {}, None)
    monkeypatch.setattr(client.opener, "open", deny)
    with pytest.raises(release.ReleaseError, match="HTTP 404"):
        client.release(TAG)


def test_token_only_sent_to_github(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "secret-value")
    client = release.Client("owner/repo")
    calls = []
    def respond(request, **kwargs):
        calls.append(request)
        return io.BytesIO(b"{}")
    monkeypatch.setattr(client.opener, "open", respond)
    client.pypi(VERSION)
    client.request("https://files.pythonhosted.org/packages/file")
    assert all(request.get_header("Authorization") is None for request in calls)
    with pytest.raises(release.ReleaseError, match="redirect refused"):
        release.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com")


@pytest.mark.parametrize("lag", ["version", "file", "timeout"])
def test_bounded_pypi_lag(repo, lag):
    blobs = artifacts()
    data = metadata(blobs)
    client = FakeClient(data, blobs=blobs)
    pending = None if lag != "file" else {**data, "urls": data["urls"][:1]}
    replies = [pending, pending, data]
    elapsed = [0]
    def sleep(seconds):
        elapsed[0] += seconds
    client.pypi = lambda _: pending if lag == "timeout" else replies.pop(0)
    if lag == "timeout":
        with pytest.raises(release.ReleaseError, match="timed out"):
            release.stage(client, VERSION, "assets", timeout=30, sleep=sleep, now=lambda: elapsed[0])
        assert not Path("assets").exists()
    else:
        release.stage(client, VERSION, "assets", timeout=30, sleep=sleep, now=lambda: elapsed[0])
        assert {path.name: path.read_bytes() for path in Path("assets").iterdir()} == blobs
    assert elapsed[0] == 30


@pytest.mark.parametrize("problem", ["hash", "traversal", "source", "extra_source", "project"])
def test_bad_artifacts_never_staged(repo, problem):
    blobs = artifacts(
        sources={"gateway/__init__.py": b"changed"} if problem == "source" else None,
        project=b"different" if problem == "project" else PROJECT,
        extra={"../escape": b"bad"} if problem == "traversal" else
              {"gateway/unexpected.py": b"bad"} if problem == "extra_source" else None,
    )
    data = metadata(blobs)
    if problem == "hash":
        data["urls"][0]["digests"]["sha256"] = "0" * 64
    with pytest.raises(release.ReleaseError):
        release.stage(FakeClient(data, blobs=blobs), VERSION, "assets")
    assert not Path("assets").exists() and not Path("escape").exists()


@pytest.mark.parametrize("problem", ["duplicate", "unexpected", "yanked", "host"])
def test_distribution_metadata_rejected(problem):
    data = metadata(artifacts())
    if problem == "duplicate":
        data["urls"].append(data["urls"][0])
    elif problem == "unexpected":
        data["urls"][0]["filename"] = "../escape.whl"
    elif problem == "yanked":
        data["urls"][0]["yanked"] = True
    else:
        data["urls"][0]["url"] = "https://example.com/file"
    with pytest.raises(release.ReleaseError):
        release.distributions(data, VERSION, incomplete=True)


@pytest.mark.parametrize("kind", ["new", "draft", "missing", "mismatch", "complete"])
def test_publish_only_verified_assets_and_preserve_existing_notes(repo, monkeypatch, kind):
    blobs = artifacts()
    data = metadata(blobs)
    existing = None if kind == "new" else published(data, draft=kind == "draft")
    if kind == "missing":
        existing["assets"].pop()
    if kind in {"mismatch", "draft"}:
        existing["assets"][0]["digest"] = "sha256:" + "0" * 64
    client = FakeClient(data, existing, blobs)
    release.stage(client, VERSION, "assets")
    calls = []
    def invoke(*args):
        calls.append(args)
        client.gh_release = published(data)
    monkeypatch.setattr(release, "gh", invoke)
    if kind == "mismatch":
        with pytest.raises(release.ReleaseError, match="refusing replacement"):
            release.publish(client, "owner/repo", VERSION, "assets")
        assert not calls
        return
    release.publish(client, "owner/repo", VERSION, "assets")
    if kind == "new":
        assert calls[0][:3] == ("release", "create", TAG)
        assert calls[0][calls[0].index("--target") + 1] == release.git("rev-parse", "HEAD").decode().strip()
    elif kind == "draft":
        assert "--clobber" in calls[0] and "--draft=false" in calls[1]
        assert all("--notes" not in call and "--generate-notes" not in call for call in calls)
    elif kind == "missing":
        assert calls[0][:3] == ("release", "upload", TAG)
        assert "--clobber" not in calls[0]
        assert len([arg for arg in calls[0] if arg.startswith("assets/")]) == 1
    else:
        assert not calls


def test_workflow_preserves_gate_and_orders_publication():
    text = (SCRIPT.parents[1] / ".github/workflows/release.yml").read_text()
    workflow = yaml.safe_load(text)
    job = workflow["jobs"]["release"]
    gate = job["if"]
    assert "conclusion == 'success'" in gate and "event == 'push'" in gate
    assert "head_branch == 'main'" in gate and "head_repository.full_name == github.repository" in gate
    assert workflow["concurrency"] == {"group": "release", "cancel-in-progress": False}
    assert job["permissions"] == {"contents": "write", "id-token": "write"}
    assert "github.event.workflow_run.head_sha" in job["steps"][0]["with"]["ref"]
    assert job["steps"][0]["with"]["fetch-depth"] == 0
    steps = job["steps"]
    preserve = next(i for i, step in enumerate(steps) if step.get("name") == "Preserve release helper")
    plan = next(i for i, step in enumerate(steps) if step.get("id") == "plan")
    upload = next(i for i, step in enumerate(steps) if "pypa/gh-action-pypi-publish@" in step.get("uses", ""))
    stage = next(i for i, step in enumerate(steps) if '" stage ' in step.get("run", ""))
    publish = next(i for i, step in enumerate(steps) if '" publish ' in step.get("run", ""))
    assert preserve < plan < upload < stage < publish
    assert steps[upload]["with"]["skip-existing"] is True
    assert "stable" not in text


@pytest.mark.parametrize("name", ["../escape", "/absolute", "a/../../escape", "a\\escape", "."])
def test_archive_paths_rejected(name):
    with pytest.raises(release.ReleaseError, match="Unsafe archive"):
        release.safe_name(name)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "traversal"])
def test_sdist_links_and_traversal_rejected(kind):
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as archive:
        item = tarfile.TarInfo(f"knowledge_gateway-{VERSION}/" + ("../escape" if kind == "traversal" else "gateway/link"))
        if kind != "traversal":
            item.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
            item.linkname = "../../escape"
        archive.addfile(item, io.BytesIO(b""))
    with pytest.raises(release.ReleaseError):
        release.archive_files(blob.getvalue(), f"knowledge_gateway-{VERSION}.tar.gz")


def test_stage_refuses_dirty_source(repo):
    blobs = artifacts()
    Path("gateway/__init__.py").write_text("dirty = True\n")
    with pytest.raises(release.ReleaseError, match="Checkout differs"):
        release.stage(FakeClient(metadata(blobs), blobs=blobs), VERSION, "assets")


def aliased_sdist(aliases, *, target_kind="file", duplicate=False):
    blob = io.BytesIO()
    root = f"knowledge_gateway-{VERSION}"
    with tarfile.open(fileobj=blob, mode="w:gz") as archive:
        for name, target in aliases.items():
            item = tarfile.TarInfo(f"{root}/{name}")
            item.type = tarfile.SYMTYPE
            item.linkname = target
            archive.addfile(item)
            if duplicate:
                archive.addfile(item)
        for name, content in {**SOURCE, "pyproject.toml": PROJECT}.items():
            item = tarfile.TarInfo(f"{root}/{name}")
            item.size = len(content)
            archive.addfile(item, io.BytesIO(content))
        if target_kind != "missing":
            item = tarfile.TarInfo(f"{root}/AGENTS.md")
            if target_kind == "directory":
                item.type = tarfile.DIRTYPE
                archive.addfile(item)
            else:
                content = b"Engineering instructions\n"
                item.size = len(content)
                archive.addfile(item, io.BytesIO(content))
    return blob.getvalue()


def test_sdist_known_documentation_aliases_verified_without_extraction(repo):
    blobs = artifacts()
    name = f"knowledge_gateway-{VERSION}.tar.gz"
    blobs[name] = aliased_sdist({"CLAUDE.md": "AGENTS.md", "GEMINI.md": "AGENTS.md"})
    release.stage(FakeClient(metadata(blobs), blobs=blobs), VERSION, "assets")
    assert Path("assets", name).read_bytes() == blobs[name]
    assert not Path("CLAUDE.md").exists()


@pytest.mark.parametrize("aliases,target_kind,duplicate", [
    ({"CLAUDE.md": "AGENTS.md"}, "missing", False),
    ({"CLAUDE.md": "AGENTS.md"}, "directory", False),
    ({"CLAUDE.md": "../AGENTS.md"}, "file", False),
    ({"GEMINI.md": "/AGENTS.md"}, "file", False),
    ({"CLAUDE.md": "gateway/__init__.py"}, "file", False),
    ({"OTHER.md": "AGENTS.md"}, "file", False),
    ({"gateway/CLAUDE.md": "AGENTS.md"}, "file", False),
    ({"CLAUDE.md": "AGENTS.md"}, "file", True),
])
def test_documentation_alias_exception_is_narrow(aliases, target_kind, duplicate):
    data = aliased_sdist(aliases, target_kind=target_kind, duplicate=duplicate)
    with pytest.raises(release.ReleaseError):
        release.archive_files(data, f"knowledge_gateway-{VERSION}.tar.gz")


@pytest.mark.parametrize("outcome", ["visible", "timeout", "http_error", "hash_error"])
def test_artifact_visibility_uses_shared_metadata_deadline(repo, outcome):
    blobs = artifacts()
    data = metadata(blobs)
    client = FakeClient(data, blobs=blobs)
    metadata_calls = []
    downloads = []
    elapsed = [0]
    def pypi(_):
        metadata_calls.append(True)
        return None if len(metadata_calls) == 1 else data
    def download(url, *, missing=False):
        assert missing
        downloads.append(url)
        if outcome == "http_error":
            raise release.ReleaseError("files.pythonhosted.org: HTTP 403")
        if outcome == "hash_error":
            return b"invalid artifact"
        if outcome == "timeout" or len(downloads) == 1:
            return None
        return blobs[url.rsplit("/", 1)[1]]
    def sleep(seconds):
        elapsed[0] += seconds
    client.pypi = pypi
    client.request = download
    if outcome == "visible":
        release.stage(client, VERSION, "assets", timeout=30, sleep=sleep, now=lambda: elapsed[0])
        assert elapsed[0] == 30
        assert len(downloads) == 3
    else:
        with pytest.raises(release.ReleaseError, match={
            "timeout": "timed out", "http_error": "HTTP 403", "hash_error": "SHA256 mismatch"
        }[outcome]):
            release.stage(client, VERSION, "assets", timeout=30, sleep=sleep, now=lambda: elapsed[0])
        assert elapsed[0] == (30 if outcome == "timeout" else 15)
        assert len(downloads) == (2 if outcome == "timeout" else 1)
        assert not Path("assets").exists()
