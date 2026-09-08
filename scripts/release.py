#!/usr/bin/env python3
"""Plan and finish immutable releases using the distributions actually served by PyPI."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import time
import tomllib
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile


class ReleaseError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the GitHub credential to another host.
        raise ReleaseError(f"HTTP redirect refused ({code})")


class Client:
    def __init__(self, repository):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ReleaseError("Invalid repository")
        self.base = f"https://api.github.com/repos/{repository}/releases"
        self.opener = build_opener(NoRedirect())

    def request(self, url, *, missing=False):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc not in {
            "api.github.com", "pypi.org", "files.pythonhosted.org"
        }:
            raise ReleaseError("Untrusted download host")
        headers = {"User-Agent": "knowledge-gateway-release"}
        if parsed.netloc == "api.github.com":
            token = os.environ.get("GH_TOKEN")
            if not token:
                raise ReleaseError("GH_TOKEN is required for GitHub reads")
            headers["Authorization"] = f"Bearer {token}"
            headers["Accept"] = "application/vnd.github+json"
        try:
            with self.opener.open(Request(url, headers=headers), timeout=30) as response:
                return response.read()
        except HTTPError as error:
            if missing and error.code == 404:
                return None
            raise ReleaseError(f"{parsed.netloc}: HTTP {error.code}") from None
        except (URLError, TimeoutError, OSError):
            raise ReleaseError(f"{parsed.netloc}: network request failed") from None

    def json(self, url, *, missing=False):
        data = self.request(url, missing=missing)
        if data is None:
            return None
        try:
            return json.loads(data)
        except (ValueError, UnicodeError):
            raise ReleaseError("Invalid API JSON") from None

    def pypi(self, version):
        return self.json(f"https://pypi.org/pypi/knowledge-gateway/{version}/json", missing=True)

    def release(self, tag):
        # A masked authorization 404 is not evidence that a release is absent.
        collection = self.json(self.base + "?per_page=1")
        if not isinstance(collection, list):
            raise ReleaseError("GitHub release collection could not be verified")
        return self.json(self.base + "/tags/" + quote(tag, safe=""), missing=True)


def git(*args):
    result = subprocess.run(["git", *args], capture_output=True)
    if result.returncode:
        raise ReleaseError("Git command failed: " + args[0])
    return result.stdout


def packaged_version(data=None):
    data = Path("pyproject.toml").read_bytes() if data is None else data
    version = tomllib.loads(data.decode())["project"]["version"]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ReleaseError("Expected a final X.Y.Z package version")
    return version


def distributions(metadata, version, *, incomplete=False):
    if metadata is None:
        if incomplete:
            return None
        raise ReleaseError("PyPI version is missing")
    if metadata.get("info", {}).get("version") != version:
        raise ReleaseError("PyPI version mismatch")
    expected = {
        f"knowledge_gateway-{version}-py3-none-any.whl": "bdist_wheel",
        f"knowledge_gateway-{version}.tar.gz": "sdist",
    }
    result = {}
    for item in metadata.get("urls", []):
        name = item.get("filename")
        if name not in expected or name in result or item.get("packagetype") != expected[name]:
            raise ReleaseError("Unexpected or duplicate PyPI distribution")
        digest = item.get("digests", {}).get("sha256", "")
        if item.get("yanked") or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ReleaseError("Yanked distribution or invalid PyPI SHA256")
        parsed = urlsplit(item.get("url", ""))
        if parsed.scheme != "https" or parsed.netloc != "files.pythonhosted.org":
            raise ReleaseError("Untrusted PyPI artifact host")
        result[name] = item
    if set(result) != set(expected):
        if incomplete:
            return None
        raise ReleaseError("PyPI wheel or sdist is missing")
    return result


def complete(release, files):
    if release is None or release.get("draft"):
        return False
    assets = release.get("assets", [])
    seen = set()
    for asset in assets:
        name = asset.get("name")
        if name not in files:
            continue
        if name in seen or asset.get("digest") != "sha256:" + files[name]["digests"]["sha256"]:
            raise ReleaseError("Published GitHub asset digest mismatch; refusing replacement")
        seen.add(name)
    return seen == set(files)


def decision(client, event, ref=None, *, dry_run=False):
    version = packaged_version()
    tag = "v" + version
    if event == "push" and ref != tag:
        raise ReleaseError("Manual tag does not match packaged version")
    tagged = git("tag", "--list", tag).decode().strip()
    source = git("rev-parse", "HEAD").decode().strip()
    metadata = client.pypi(version)
    release = client.release(tag)
    done = False
    if tagged:
        if packaged_version(git("show", f"{tag}:pyproject.toml")) != version:
            raise ReleaseError("Tagged pyproject version mismatch")
        source = git("rev-parse", f"{tag}^{{commit}}").decode().strip()
        files = distributions(metadata, version, incomplete=True)
        if files:
            done = complete(release, files)
        if not done and not dry_run:
            git("checkout", "--detach", source)
    elif metadata is not None or release is not None:
        raise ReleaseError(
            "PyPI version or GitHub release exists without its immutable tag; "
            "investigate the original source and recover that tag before retrying"
        )
    return {"version": version, "tag": tag, "source_sha": source, "release": not done}


def safe_name(name):
    path = PurePosixPath(name)
    if not path.parts or "\\" in name or path.is_absolute() or ".." in path.parts or str(path) != name.rstrip("/"):
        raise ReleaseError("Unsafe archive member path")
    return path


def archive_files(data, name):
    files = {}
    if name.endswith(".whl"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for member in archive.infolist():
                safe_name(member.filename)
                if member.filename in files or (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ReleaseError("Duplicate or symlink archive member")
                if not member.is_dir():
                    files[member.filename] = archive.read(member)
        return files
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        roots = set()
        seen = set()
        aliases = set()
        for member in archive:
            path = safe_name(member.name)
            roots.add(path.parts[0])
            if str(path) in seen:
                raise ReleaseError("Duplicate archive member")
            seen.add(str(path))
            if member.issym() and len(path.parts) == 2 and path.name in {"CLAUDE.md", "GEMINI.md"}:
                if member.linkname != "AGENTS.md":
                    raise ReleaseError("Unsafe documentation alias target")
                aliases.add(path.name)
            elif not (member.isfile() or member.isdir()):
                raise ReleaseError("Non-regular archive member")
            if member.isfile():
                if len(path.parts) < 2:
                    raise ReleaseError("Invalid sdist root")
                files[str(PurePosixPath(*path.parts[1:]))] = archive.extractfile(member).read()
        if len(roots) != 1 or roots != {name.removesuffix(".tar.gz")}:
            raise ReleaseError("Invalid sdist root")
        if aliases and "AGENTS.md" not in files:
            raise ReleaseError("Documentation aliases require a regular AGENTS.md in the sdist")
    return files


def source_files():
    paths = git("ls-files", "-z", "gateway/").decode().split("\0")
    result = {}
    for name in filter(None, paths):
        data = git("show", f"HEAD:{name}")
        if Path(name).is_symlink() or Path(name).read_bytes() != data:
            raise ReleaseError("Checkout differs from tracked source")
        result[name] = data
    if not result:
        raise ReleaseError("No tracked gateway source")
    project = git("show", "HEAD:pyproject.toml")
    if Path("pyproject.toml").read_bytes() != project:
        raise ReleaseError("Checkout pyproject differs from source")
    return result, project


def verify_artifact(data, name, item, sources, project):
    if hashlib.sha256(data).hexdigest() != item["digests"]["sha256"]:
        raise ReleaseError("PyPI artifact SHA256 mismatch")
    files = archive_files(data, name)
    runtime = {key: value for key, value in files.items() if key.startswith("gateway/")}
    if runtime != sources:
        raise ReleaseError("Published gateway source does not match checked-out source")
    if name.endswith(".tar.gz") and files.get("pyproject.toml") != project:
        raise ReleaseError("Published pyproject does not match checked-out source")


def stage(client, version, destination, *, timeout=300, interval=15, sleep=time.sleep, now=time.monotonic):
    deadline = now() + timeout

    def wait_for_visibility():
        remaining = deadline - now()
        if remaining <= 0:
            raise ReleaseError("PyPI visibility timed out; retry from the immutable tag")
        sleep(min(interval, remaining))

    while True:
        files = distributions(client.pypi(version), version, incomplete=True)
        if files:
            break
        wait_for_visibility()
    sources, project = source_files()
    verified = {}
    for name, item in files.items():
        while True:
            data = client.request(item["url"], missing=True)
            if data is not None:
                break
            wait_for_visibility()
        verify_artifact(data, name, item, sources, project)
        verified[name] = data
    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise ReleaseError("Release asset directory must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    for name, data in verified.items():
        (destination / name).write_bytes(data)
    return files


def gh(*args):
    result = subprocess.run(["gh", *args], capture_output=True)
    if result.returncode:
        raise ReleaseError("GitHub release command failed (output withheld)")


def publish(client, repository, version, destination):
    tag = "v" + version
    files = distributions(client.pypi(version), version)
    sources, project = source_files()
    assets = []
    for name, item in files.items():
        path = Path(destination) / name
        verify_artifact(path.read_bytes(), name, item, sources, project)
        assets.append(str(path))
    release = client.release(tag)
    if complete(release, files):
        return
    if release is None:
        gh("release", "create", tag, *assets, "--repo", repository, "--target",
           git("rev-parse", "HEAD").decode().strip(), "--title", tag, "--generate-notes")
    elif release.get("draft"):
        gh("release", "upload", tag, *assets, "--repo", repository, "--clobber")
        gh("release", "edit", tag, "--repo", repository, "--draft=false")
    else:
        existing = {asset["name"] for asset in release.get("assets", [])}
        missing = [str(Path(destination) / name) for name in files if name not in existing]
        gh("release", "upload", tag, *missing, "--repo", repository)
    if not complete(client.release(tag), files):
        raise ReleaseError("GitHub release is still incomplete after publication")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["decision", "stage", "publish"])
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "fszalaj/knowledge-gateway"))
    parser.add_argument("--event", choices=["push", "workflow_run"], default=os.environ.get("GITHUB_EVENT_NAME", "workflow_run"))
    parser.add_argument("--ref", default=os.environ.get("GITHUB_REF_NAME"))
    parser.add_argument("--version")
    parser.add_argument("--assets", default="release-assets")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        client = Client(args.repository)
        if args.command == "decision":
            plan = decision(client, args.event, args.ref, dry_run=args.dry_run)
            print(json.dumps(plan, sort_keys=True))
            if not args.dry_run:
                if os.environ.get("GITHUB_OUTPUT"):
                    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                        for key, value in plan.items():
                            output.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")
                if os.environ.get("GITHUB_STEP_SUMMARY"):
                    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
                        summary.write(f"Release `{plan['tag']}` source: `{plan['source_sha']}`\n")
        elif args.dry_run:
            raise ReleaseError("--dry-run is supported only for decision")
        else:
            version = packaged_version()
            if args.version != version:
                raise ReleaseError("Requested version does not match checkout")
            if args.command == "stage":
                stage(client, version, args.assets)
            else:
                publish(client, args.repository, version, args.assets)
    except (ReleaseError, OSError, ValueError, KeyError, tarfile.TarError, zipfile.BadZipFile) as error:
        message = str(error) if isinstance(error, ReleaseError) else "Invalid release input or artifact"
        print(f"Release stopped: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
