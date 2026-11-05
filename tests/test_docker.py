"""Docker extractor: stages, images, the files that go into an image, compose services, scripts."""
import json
import os

import pytest

from gateway.codegraph import extract_docker

DIGEST = "sha256:" + "ab" * 32
SECRET = "s3cr3t-Zq9vX-planted-value"


def _mk(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _index(frag):
    return ({n["id"]: n for n in frag["nodes"]},
            {(e["source"], e["relation"], e["target"]): e for e in frag["edges"]})


def _multistage(root):
    _mk(root, "pyproject.toml", "[project]\nname = 'app'\ndependencies = []\n")
    _mk(root, "requirements.txt", "web-framework==1.0\n")
    _mk(root, "app/main.py", "print('hi')\n")
    _mk(root, "app/start.sh", "#!/bin/sh\nexec python main.py\n")
    _mk(root, "scripts/entrypoint.sh", "#!/bin/sh\nexec \"$@\"\n")
    _mk(root, "Dockerfile", f"""# syntax=docker/dockerfile:1
ARG BASE_TAG=3.12
FROM --platform=$BUILDPLATFORM python:3.12-slim@{DIGEST} AS build
WORKDIR /src
COPY pyproject.toml requirements.txt ./
COPY app/ ./app/
# a comment inside the stage
RUN pip install -r requirements.txt \\
    && chmod +x /src/app/start.sh

FROM build AS test
RUN python -m pytest

FROM example.com/base/runtime:1.0 AS final
COPY --from=build /src/app /app
COPY --from=example.com/tools/helper:2.0 /bin/helper /usr/bin/
COPY scripts/entrypoint.sh /entrypoint.sh
EXPOSE 8080/tcp
ENV APP_MODE=production LOG_LEVEL=info
ARG RELEASE
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "/app/main.py"]
""")


def test_stages_images_copies_and_runs(tmp_path):
    _multistage(tmp_path)
    nodes, edges = _index(extract_docker.extract(tmp_path))
    df = "dockerfile:Dockerfile"
    build, test, final = (f"dockerstage:Dockerfile#{s}" for s in ("build", "test", "final"))

    for st in (build, test, final):
        assert (df, "has_stage", st) in edges
    based = edges[(build, "based_on", "extimage:python:3.12-slim")]
    assert based["digest"] == DIGEST
    assert (test, "based_on", build) in edges
    assert (final, "based_on", "extimage:example.com/base/runtime:1.0") in edges
    assert nodes["extimage:python:3.12-slim"]["repository"] == "python"
    assert nodes["extimage:python:3.12-slim"]["tag"] == "3.12-slim"

    # COPY of local files lands on the node the owning pass gives that file
    assert (build, "copies", "manifest:pyproject.toml") in edges
    assert (build, "copies", "manifest:requirements.txt") in edges
    assert edges[(build, "copies", "manifest:requirements.txt")]["confidence"] == "INFERRED"
    assert (final, "copies", "module:scripts/entrypoint.sh") in edges
    assert nodes["module:scripts/entrypoint.sh"]["file_type"] == "bash"
    # a directory copy maps paths but is not an edge of its own
    assert not any(t == "file:app" or t == "module:app" for (_, _, t) in edges)

    assert (final, "copies_from", build) in edges
    assert (final, "copies_from", "extimage:example.com/tools/helper:2.0") in edges

    # ENTRYPOINT/CMD resolved through COPY destinations, WORKDIR and COPY --from
    assert edges[(final, "runs", "module:scripts/entrypoint.sh")]["instruction"] == "ENTRYPOINT"
    assert edges[(final, "runs", "module:app/main.py")]["instruction"] == "CMD"
    assert nodes["module:app/main.py"]["file_type"] == "python"
    # chmod +x is not running the script
    assert not any(r == "runs" and t == "module:app/start.sh" for (_, r, t) in edges)

    fin = nodes[final]
    assert fin["final"] is True and fin["expose"] == ["8080/tcp"]
    assert fin["env"] == ["APP_MODE", "LOG_LEVEL"] and fin["args"] == ["RELEASE"]
    assert "final" not in nodes[build]
    assert nodes[df]["build_args"] == ["BASE_TAG"] and nodes[df]["stages"] == 3
    assert nodes[build]["source_location"] == "L3"


def test_unnamed_stages_index_references_and_unresolved_tags(tmp_path):
    _mk(tmp_path, "tool.go", "package main\n")
    _mk(tmp_path, "deploy/app.Dockerfile", """FROM golang:1.22
COPY tool.go /src/
FROM ${REGISTRY}/runtime:1
FROM example.com/base:${VERSION}
COPY --from=0 /src/tool.go /bin/
FROM scratch
""")
    nodes, edges = _index(extract_docker.extract(tmp_path))
    s = [f"dockerstage:deploy/app.Dockerfile#{i}" for i in range(4)]
    assert all(x in nodes for x in s)
    assert nodes[s[0]]["label"] == "stage 0"
    # a variable repository produces no image node; a variable tag keeps the repository
    assert not any(r == "based_on" for (src, r, _) in edges if src == s[1])
    assert edges[(s[2], "based_on", "extimage:example.com/base")]["tag_unresolved"] is True
    assert (s[2], "copies_from", s[0]) in edges
    assert nodes[s[1]]["base"] == "${REGISTRY}/runtime:1"
    assert not any(t.startswith("extimage:scratch") for (_, _, t) in edges)
    # the default context is the Dockerfile's own directory: tool.go is not in deploy/
    assert not any(r == "copies" for (_, r, _) in edges)


def test_heredoc_and_escape_directive(tmp_path):
    _mk(tmp_path, "a.sh", "echo a\n")
    _mk(tmp_path, "Containerfile", """# escape=`
FROM alpine:3.20
RUN <<EOF
FROM bogus:1
COPY nothing /x
EOF
COPY a.sh `
     /usr/local/bin/a.sh
ENTRYPOINT /usr/local/bin/a.sh --flag
""")
    nodes, edges = _index(extract_docker.extract(tmp_path))
    st = "dockerstage:Containerfile#0"
    assert [n for n in nodes if n.startswith("dockerstage:")] == [st]
    assert "extimage:bogus:1" not in nodes
    assert (st, "copies", "module:a.sh") in edges
    assert (st, "runs", "module:a.sh") in edges


def test_compose_services(tmp_path):
    _mk(tmp_path, "web/run.sh", "#!/bin/sh\n")
    _mk(tmp_path, "web/Containerfile", "FROM alpine:3.20 AS runtime\nCOPY run.sh /run.sh\nCMD [\"/run.sh\"]\n")
    _mk(tmp_path, "config/web.conf", "listen 80\n")
    _mk(tmp_path, "web.env", "MODE=x\n")
    _mk(tmp_path, "compose.yaml", """services:
  web:
    build:
      context: ./web
      dockerfile: Containerfile
      target: runtime
      args:
        BUILD_FLAVOR: plain
    image: example.com/web:${VERSION:-1.0}
    depends_on: [db]
    env_file: [./web.env]
    environment:
      DB_HOST: db
      DB_USER: app
    volumes:
      - ./config/web.conf:/etc/web.conf:ro
      - data:/var/lib/data
  db:
    image: postgres:16
    environment:
      - POSTGRES_DB=app
      - POSTGRES_USER
    volumes:
      - type: bind
        source: ./config/web.conf
        target: /etc/db.conf
volumes:
  data: {}
""")
    nodes, edges = _index(extract_docker.extract(tmp_path))
    web, db = "composeservice:compose.yaml#web", "composeservice:compose.yaml#db"
    assert ("compose:compose.yaml", "has_service", web) in edges
    bf = edges[(web, "builds_from", "dockerfile:web/Containerfile")]
    assert bf["stage"] == "runtime" and bf["confidence"] == "EXTRACTED"
    assert (web, "targets_stage", "dockerstage:web/Containerfile#runtime") in edges
    ui = edges[(web, "uses_image", "extimage:example.com/web")]
    assert ui["tag_unresolved"] is True and ui["built"] is True
    assert (db, "uses_image", "extimage:postgres:16") in edges
    assert (web, "depends_on", db) in edges
    m = edges[(web, "mounts", "file:config/web.conf")]
    assert m["read_only"] is True and m["container_path"] == "/etc/web.conf"
    assert (db, "mounts", "file:config/web.conf") in edges
    assert (web, "uses_env_file", "file:web.env") in edges
    assert nodes[web]["environment_keys"] == ["DB_HOST", "DB_USER"]
    assert nodes[db]["environment_keys"] == ["POSTGRES_DB", "POSTGRES_USER"]
    assert nodes[web]["env_files"] == ["web.env"] and nodes[web]["build_args"] == ["BUILD_FLAVOR"]
    # the declared context makes the COPY resolution EXTRACTED
    st = "dockerstage:web/Containerfile#runtime"
    assert edges[(st, "copies", "module:web/run.sh")]["confidence"] == "EXTRACTED"
    assert (st, "runs", "module:web/run.sh") in edges
    assert not any("data" == t.rsplit(":", 1)[-1] for (_, _, t) in edges)


def test_scripts_build_and_push(tmp_path):
    _mk(tmp_path, "docker/app.Dockerfile", "FROM alpine:3.20\n")
    _mk(tmp_path, "worker/Dockerfile", "FROM alpine:3.20\n")
    _mk(tmp_path, "Dockerfile.dev", "FROM alpine:3.20\n")
    _mk(tmp_path, "scripts/release.sh", """#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
TAG="$(git rev-parse --short HEAD)"
# docker build -f ignored.Dockerfile .
docker build --build-arg FLAVOR=x -f docker/app.Dockerfile -t example.com/team/app:$TAG .
docker push example.com/team/app:$TAG && echo done
sudo docker --log-level warn buildx build --push --target final -t example.com/team/worker:1.2 worker
docker run --rm example.com/team/app:latest build
""")
    _mk(tmp_path, "build.ps1", "docker build `\n  -t app:1 `\n  -f Dockerfile.dev `\n  .\n")
    nodes, edges = _index(extract_docker.extract(tmp_path))
    rel = "module:scripts/release.sh"
    b = edges[(rel, "builds", "dockerfile:docker/app.Dockerfile")]
    assert b["confidence"] == "INFERRED"
    assert edges[(rel, "builds", "dockerfile:worker/Dockerfile")]["stage"] == "final"
    assert edges[(rel, "pushes", "extimage:example.com/team/app")]["tag_unresolved"] is True
    assert (rel, "pushes", "extimage:example.com/team/worker:1.2") in edges
    assert not any("ignored" in t for (_, _, t) in edges)
    assert not any(r == "pushes" and "latest" in t for (_, r, t) in edges)
    assert ("module:build.ps1", "builds", "dockerfile:Dockerfile.dev") in edges
    assert nodes["module:build.ps1"]["file_type"] == "powershell"
    assert nodes[rel]["file_type"] == "bash"


def test_copy_joins_other_passes_node_ids(tmp_path):
    _mk(tmp_path, "src/Api/Api.csproj", "<Project Sdk=\"Microsoft.NET.Sdk\"></Project>\n")
    _mk(tmp_path, "src/Api/Program.cs", "class P {}\n")
    _mk(tmp_path, "src/Api/appsettings.json", "{}\n")
    _mk(tmp_path, "src/Api/Dockerfile", "FROM example.com/sdk:8 AS build\nCOPY *.csproj ./\n"
                                         "COPY Program.cs appsettings.json /app/\n")
    _, edges = _index(extract_docker.extract(tmp_path))
    st = "dockerstage:src/Api/Dockerfile#build"
    assert (st, "copies", "project:src/Api/Api.csproj") in edges
    assert (st, "copies", "module:src/Api/Program.cs") in edges
    assert (st, "copies", "file:src/Api/appsettings.json") in edges


def test_parse_image():
    assert extract_docker.parse_image("localhost:5000/app")["name"] == "localhost:5000/app"
    info = extract_docker.parse_image(f"example.com:443/team/app:1.2@{DIGEST}")
    assert info == {"repository": "example.com:443/team/app", "tag": "1.2", "digest": DIGEST,
                    "name": "example.com:443/team/app:1.2"}
    assert extract_docker.parse_image("${REGISTRY}/app:1") is None
    assert extract_docker.parse_image("scratch") is None
    assert extract_docker.parse_image("{{ image }}") is None
    assert extract_docker.parse_image("app:$TAG") == {"repository": "app", "tag_unresolved": True,
                                                      "name": "app"}


def test_secret_values_never_reach_the_graph(tmp_path):
    _mk(tmp_path, "entry.sh", "#!/bin/sh\n")
    _mk(tmp_path, ".env", f"API_TOKEN={SECRET}\n")
    _mk(tmp_path, "app.env", f"API_TOKEN={SECRET}\n")
    _mk(tmp_path, "Dockerfile", f"""ARG GLOBAL_TOKEN={SECRET}
FROM alpine:3.20
ARG BUILD_TOKEN={SECRET}
ENV API_TOKEN={SECRET} OTHER="quoted {SECRET} value"
ENV LEGACY_TOKEN {SECRET}
LABEL token={SECRET}
ADD https://user:{SECRET}@example.com/pkg.tar.gz /opt/
RUN echo {SECRET} > /etc/token
HEALTHCHECK CMD curl -H "Authorization: {SECRET}" http://localhost/
COPY entry.sh /entry.sh
ENTRYPOINT ["/entry.sh", "--token", "{SECRET}"]
""")
    _mk(tmp_path, "docker-compose.override.yml", f"""services:
  app:
    build:
      context: .
      args:
        BUILD_TOKEN: {SECRET}
      labels:
        token: {SECRET}
    image: example.com/app:1
    command: ["--token", "{SECRET}"]
    env_file: [.env, app.env]
    environment:
      API_TOKEN: {SECRET}
      URL: https://user:{SECRET}@example.com/
    ports: ["8080:80"]
    healthcheck:
      test: ["CMD", "curl", "-u", "{SECRET}"]
  remote:
    build: https://{SECRET}@example.com/repo.git
  worker:
    image: example.com/worker:1
    environment:
      - API_TOKEN={SECRET}
secrets:
  token:
    environment: {SECRET}
""")
    _mk(tmp_path, "ci.sh", f"""docker login -u ci -p {SECRET} example.com
docker build --build-arg TOKEN={SECRET} --secret id=tok,env={SECRET} --label k={SECRET} -t app:1 .
docker push app:1
""")
    res = extract_docker.extract(tmp_path)
    nodes, edges = _index(res)
    assert ("module:ci.sh", "builds", "dockerfile:Dockerfile") in edges     # the pass did run
    assert nodes["composeservice:docker-compose.override.yml#app"]["environment_keys"] == ["API_TOKEN", "URL"]
    assert nodes["dockerstage:Dockerfile#0"]["env"] == ["API_TOKEN", "OTHER", "LEGACY_TOKEN"]
    assert SECRET not in json.dumps(res)


def test_robustness_symlinks_caps_pruning_and_determinism(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _mk(outside, "Dockerfile", "FROM leaked:1\n")
    _mk(outside, "secret.txt", "x\n")
    _mk(repo, "Dockerfile", "FROM alpine:3.20\nCOPY link.txt /x\nCOPY linkdir/secret.txt /y\n")
    try:
        os.symlink(outside / "Dockerfile", repo / "sub.Dockerfile")
        os.symlink(outside / "secret.txt", repo / "link.txt")
        os.symlink(outside, repo / "linkdir", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    _mk(repo, "node_modules/pkg/Dockerfile", "FROM vendored:1\n")
    _mk(repo, "skipme/Dockerfile", "FROM skipped:1\n")
    _mk(repo, "compose.yml", "services: [this: is: not: valid\n")
    (repo / "docker-compose.yml").write_bytes(b"\xff\xfe\x00garbage: [\n")
    _mk(repo, "big/Dockerfile", "FROM big:1\n" + "# pad\n" * 200)
    monkeypatch.setattr(extract_docker, "MAX_BYTES", 600)

    res = extract_docker.extract(repo, frozenset({"skipme"}))
    blob = json.dumps(res)
    for absent in ("leaked", "vendored", "skipped", "big:1", "link.txt", "secret.txt"):
        assert absent not in blob
    assert "extimage:alpine:3.20" in blob
    assert json.dumps(extract_docker.extract(repo, frozenset({"skipme"}))) == blob


def test_relative_workdir_after_an_unknown_one_stays_unknown(tmp_path):
    # WORKDIR $APP is unknown, so WORKDIR sub is too: resolving it against "/" would make
    # ./run.sh match the file copied to /sub/run.sh, which Docker never runs
    _mk(tmp_path, "run.sh", "echo\n")
    _mk(tmp_path, "Dockerfile", "FROM alpine\nARG APP=/opt/app\nWORKDIR $APP\nWORKDIR sub\n"
                                "COPY run.sh /sub/run.sh\nCMD [\"./run.sh\"]\n")
    _, edges = _index(extract_docker.extract(tmp_path))
    assert not [k for k in edges if k[1] == "runs"]
    # an absolute WORKDIR makes it known again
    _mk(tmp_path, "Dockerfile", "FROM alpine\nWORKDIR $APP\nWORKDIR /sub\n"
                                "COPY run.sh /sub/run.sh\nCMD [\"./run.sh\"]\n")
    _, edges = _index(extract_docker.extract(tmp_path))
    assert ("dockerstage:Dockerfile#0", "runs", "module:run.sh") in edges


def test_copy_wildcards_never_follow_directory_links(tmp_path):
    # Path.glob follows directory symlinks: out of the root, and round a `loop -> .` link with
    # cost growing by the number of links per `*`
    import time
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _mk(outside, "leak.sh", "echo\n")
    _mk(repo, "bin/run.sh", "echo\n")
    _mk(repo, "bin/sub/deep.sh", "echo\n")
    try:
        for n in "abcd":
            os.symlink(".", repo / n, target_is_directory=True)
        os.symlink(outside, repo / "ext", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    _mk(repo, "Dockerfile", "FROM alpine\nCOPY */*.sh /x/\nCOPY */*/*/* /y/\n"
                            "COPY ext/* /z/\nCOPY */*/*/*/*/* /w/\n")
    t = time.monotonic()
    res = extract_docker.extract(repo)
    assert time.monotonic() - t < 5
    copied = {k[2] for k in _index(res)[1] if k[1] == "copies"}
    assert copied == {"module:bin/run.sh"}          # `*/*/*/*` finds nothing real; 5 wildcards: capped
    assert "leak" not in json.dumps(res)


def test_a_shared_environment_alias_cannot_multiply_compose_output(tmp_path):
    env = "x-env: &e {" + ", ".join(f"K{i}: v" for i in range(5000)) + "}\n"
    _mk(tmp_path, "compose.yaml", env + "services:\n" + "".join(
        f"  s{i}: {{image: app, environment: *e}}\n" for i in range(300)))
    res = extract_docker.extract(tmp_path)
    keys = sum(len(n.get("environment_keys", [])) for n in res["nodes"])
    assert keys <= extract_docker.MAX_LISTED_NAMES
    assert len(json.dumps(res)) < 2_000_000
    assert "composeservice:compose.yaml#s0" in {n["id"] for n in res["nodes"]}


def test_glob_listing_is_contained_and_stops_early(tmp_path, monkeypatch):
    # the expansion itself must never list a directory outside the root
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _mk(outside, "x/y.txt", "x\n")
    _mk(repo, "f/a.txt", "x\n")
    try:
        os.symlink(outside, repo / "ext", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    listed = []
    real = os.scandir

    def spy(p="."):
        listed.append(os.path.realpath(p))
        return real(p)
    monkeypatch.setattr(extract_docker.os, "scandir", spy)
    assert extract_docker._glob(repo.resolve(), "", "*/*.txt") == ["f/a.txt"]
    assert not [p for p in listed if p.startswith(os.path.realpath(outside))]
    monkeypatch.setattr(extract_docker, "MAX_GLOB", 1)
    _mk(repo, "f/b.txt", "x\n")
    assert extract_docker._glob(repo.resolve(), "", "f/*.txt") == ["f/a.txt"]
