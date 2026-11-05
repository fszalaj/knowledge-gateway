"""HTTP request-file extractor: which routes a repository's .http/.rest files call, never values."""
import json
import os
import time

import pytest

from gateway.codegraph import extract_appsmith, extract_http

SECRET = "zz-planted-s3cr3t-4b81e0"


def _mk(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _index(r):
    nodes = {n["id"]: n for n in r["nodes"]}
    rels = {(e["source"], e["relation"], e["target"]) for e in r["edges"]}
    return nodes, rels


SAMPLE = """\
@host = http://localhost:8080
@base = {{host}}/api/v2
@contentType = application/json

### list the items
# @name listItems
GET {{base}}/items?page=1&size=20 HTTP/1.1
Accept: {{contentType}}
Authorization: Bearer {{token}}

###

// @name createItem
POST {{base}}/items
Content-Type: {{contentType}}
X-Request-Id: {{$guid}}

{
  "owner": "{{listItems.response.body.$.items[0].owner}}",
  "count": 3
}

###
# no name: keyed by position
PUT https://api.example.org/orders/{{orderId}}/lines/42?expand=all
    &trace=1
If-Match: *

###
https://api.example.org/health/
"""


def test_requests_methods_and_route_shapes(tmp_path):
    _mk(tmp_path, "api/items.http", SAMPLE)
    nodes, rels = _index(extract_http.extract(tmp_path))
    f = nodes["httpfile:api/items.http"]
    assert (f["type"], f["file_type"], f["source_file"]) == ("http_file", "http", "api/items.http")
    assert f["variables"] == ["base", "contentType", "host"]
    assert f["request_count"] == 4
    lst = nodes["httprequest:api/items.http#listItems"]
    # the base variable's own path segments are holes: its value never reaches the graph
    assert (lst["method"], lst["route_shape"], lst["name"]) == ("GET", "/{}/{}/items", "listItems")
    assert lst["source_location"] == "L7" and lst["base_variable"] == "base"
    assert lst["headers"] == ["Accept", "Authorization"] and lst["uses_auth"] is True
    assert lst["variables"] == ["base", "contentType", "token"]
    assert "has_body" not in lst
    cre = nodes["httprequest:api/items.http#createItem"]
    assert (cre["method"], cre["route_shape"], cre["has_body"]) == ("POST", "/{}/{}/items", True)
    assert "uses_auth" not in cre and "$guid" in cre["variables"]
    put = nodes["httprequest:api/items.http#3"]
    assert (put["method"], put["route_shape"]) == ("PUT", "/orders/{}/lines/{}")
    assert put["label"] == "PUT /orders/{}/lines/{}" and put["headers"] == ["If-Match"]
    health = nodes["httprequest:api/items.http#4"]
    assert (health["method"], health["route_shape"]) == ("GET", "/health")
    assert ("httpfile:api/items.http", "has_request", "httprequest:api/items.http#3") in rels
    assert ("httprequest:api/items.http#createItem", "uses_response",
            "httprequest:api/items.http#listItems") in rels


@pytest.mark.parametrize("url, shape", [
    ("{{host}}/api/items", "/api/items"),              # a base binding is the host
    ("{{host}}:{{port}}/api/items/", "/api/items"),
    ("{{host}}", "/"),
    ("http://user:pw@h.example.org:9000/v1/a;x=1?q=2#f", "/v1/a"),
    ("localhost:5000/api/x", "/api/x"),
    ("/relative/path", "/relative/path"),
    ("{{host}}/users/{{id}}/a1b2c3d4e5/Ok", "/users/{}/{}/Ok"),
    ("{{host}}//double//slash/", "/double/slash"),
    ("{{host}}/a/{{x}}{{y}}/b", "/a/{}/b"),
    ("{{host}}/p/{{ q }}?x={{v}}", "/p/{}"),
    # the query string and fragment go first, whatever they hold
    ("/api/login?next=https://example.invalid/after", "/api/login"),
    ("/api/items#frag=https://example.invalid/after", "/api/items"),
    ("{{host}}?redirect=/after/x", "/"),
    ("localhost?x=a/after", "/"),
    ("example.invalid?k=v/after", "/"),
    ("https://h.example.org?sig=ab/cd/ef", "/"),
    # without a scheme the first segment is the host, dotted or not
    ("apiserver/items/list", "/items/list"),
    ("items", "/"),
])
def test_route_shape_rule(url, shape):
    assert extract_http._route(url, {})[0] == shape


def test_base_variable_substitution_is_shape_only():
    fv = {"base": "{{root}}/svc", "root": "https://h.example.org/gw", "bare": "h.example.org",
          "loop": "{{loop}}/x", "tok": SECRET, "q": "https://h.example.org/api/?v=1#f",
          "port": "{{root}}:8080/{{svc}}/v1"}
    # one hole per segment the variable contributes - never its text
    assert extract_http._route("{{base}}/items", fv) == ("/{}/{}/items", "base")
    assert extract_http._route("{{bare}}/items", fv) == ("/items", "bare")   # a host, dropped
    assert extract_http._route("{{tok}}/items", fv) == ("/items", "tok")     # not a URL: dropped
    assert extract_http._route("{{loop}}/y", fv)[0].endswith("/{}/y")        # bounded, no hang
    # a base carrying a query or fragment still lets the request's own path through
    assert extract_http._route("{{q}}/items", fv) == ("/{}/items", "q")
    assert extract_http._route("{{port}}/items", fv)[0] == "/{}/{}/{}/items"


def test_appsmith_queries_carry_the_same_route_shape(tmp_path):
    # the shared rule: an Appsmith query and a request file name the same route the same way
    assert extract_appsmith.route_shape("api//items/{}/") == "/api/items/{}"
    assert extract_appsmith.route_shape("/") == "/"
    assert extract_appsmith.route_shape(None) is None
    meta = {"artifactJsonType": "APPLICATION"}
    _mk(tmp_path, "ui/metadata.json", json.dumps(meta))
    _mk(tmp_path, "ui/application.json", json.dumps({"name": "Shop"}))
    _mk(tmp_path, "ui/pages/Home/Home.json", json.dumps({"unpublishedPage": {"name": "Home"}}))
    _mk(tmp_path, "ui/pages/Home/queries/Get/metadata.json", json.dumps({
        "pluginType": "API", "unpublishedAction": {"name": "Get", "actionConfiguration": {
            "httpMethod": "GET", "path": "api/items/{{Table1.selectedRow.id}}/"}}}))
    _mk(tmp_path, "calls.http", "GET {{host}}/api/items/{{id}}\n")
    q = _index(extract_appsmith.extract(tmp_path))[0]["appsmithquery:Shop/Home/Get"]
    req = _index(extract_http.extract(tmp_path))[0]["httprequest:calls.http#1"]
    assert q["path"] == "api/items/{}/"                    # unchanged attribute
    assert q["route_shape"] == req["route_shape"] == "/api/items/{}"


def test_names_ids_and_fallbacks(tmp_path):
    _mk(tmp_path, "a.rest", "# @name dup\nGET /one\n###\n# @name dup\nGET /two\n"
                            "###\n# @name 7\nGET /three\n###\n# @name=eq\nget /four\n"
                            "###\n# just a comment\n###\nDelete the item later\n")
    nodes, _ = _index(extract_http.extract(tmp_path))
    ids = sorted(n for n in nodes if n.startswith("httprequest:"))
    assert ids == ["httprequest:a.rest#2", "httprequest:a.rest#3",
                   "httprequest:a.rest#dup", "httprequest:a.rest#eq"]
    assert nodes["httprequest:a.rest#dup"]["route_shape"] == "/one"   # first holder keeps it
    assert nodes["httprequest:a.rest#2"]["route_shape"] == "/two"     # repeated name: position
    assert nodes["httprequest:a.rest#3"]["name"] == "7"               # numeric name: position
    assert nodes["httprequest:a.rest#eq"]["method"] == "GET"          # lowercase method
    assert nodes["httpfile:a.rest"]["request_count"] == 4   # a prose line is not a request


def test_local_files_scripts_imports_and_runs(tmp_path):
    _mk(tmp_path, "req/payload.json", "{}")
    _mk(tmp_path, "req/check.js", "client.test('ok', () => {});")
    _mk(tmp_path, "req/pre.js", "")
    _mk(tmp_path, "req/common.http", "# @name login\nPOST {{host}}/auth/login\n")
    _mk(tmp_path, "req/main.http", "import ./common.http\n"
                                   "run #login (@user=" + SECRET + ")\n"
                                   "run ./common.http\n"
                                   "###\n< ./pre.js\n# @name upload\nPOST {{host}}/files\n"
                                   "Content-Type: application/json\n\n< ./payload.json\n\n"
                                   "> ./check.js\n>> ./out/" + SECRET + ".json\n"
                                   "> {%\n  client.global.set('" + SECRET + "', 1)\n%}\n"
                                   "###\nGET {{host}}/after/script\n"
                                   "### missing targets\n< ./nope.js\nGET /x\n\n< ../../etc/passwd\n")
    r = extract_http.extract(tmp_path)
    nodes, rels = _index(r)
    up = "httprequest:req/main.http#upload"
    assert (up, "body_from", "file:req/payload.json") in rels
    assert (up, "runs", "module:req/check.js") in rels
    assert (up, "runs", "module:req/pre.js") in rels
    phases = {(e["target"], e.get("phase")) for e in r["edges"] if e["source"] == up}
    assert ("module:req/pre.js", "pre_request") in phases and ("module:req/check.js", "response") in phases
    assert nodes["module:req/check.js"]["file_type"] == "javascript"
    assert ("httpfile:req/main.http", "imports", "httpfile:req/common.http") in rels
    assert ("httpfile:req/main.http", "runs", "httprequest:req/common.http#login") in rels
    assert ("httpfile:req/main.http", "runs", "httpfile:req/common.http") in rels
    assert nodes["httprequest:req/main.http#2"]["route_shape"] == "/after/script"  # script skipped
    targets = {e["target"] for e in r["edges"]}
    assert not any("nope" in t or "passwd" in t or "out/" in t for t in targets)
    assert all(e["source"] in nodes and e["target"] in nodes for e in r["edges"])
    assert SECRET not in json.dumps(r)


def test_no_values_leak(tmp_path):
    # the canary: a unique secret in every place a value can sit; none may reach the graph
    s = SECRET
    _mk(tmp_path, "svc/secrets.http", f"""\
@apiKey = {s}
@password = {s}-pw
@url = https://admin:{s}@{s}.example.net:8443/v1/{s}/orders
# a comment mentioning {s}
### title {s}
# @name withSecrets
POST {{{{url}}}}/items?api_key={s}&sig={s}
Authorization: Basic admin {s}
X-Api-Key: {{{{apiKey}}}}
Cookie: session={s}
X-Custom: {s}

{{"password": "{s}", "nested": {{"token": "{s}"}}}}

###
GET https://user:{s}@host-{s}.example.net/hooks/{s}/send#{s}
Ocp-Apim-Subscription-Key: {s}

###
curl -H "Authorization: {s}" https://{s}.example.net/x

###
GET /api/login?next=https://example.net/{s}x/qsvalue
###
GET /api/items#frag=https://example.net/fragvalue
###
GET {{{{host}}}}?redirect=/qsredirect/x
###
GET localhost?x=a/qslocal
###
GET example.net?k=v/qsdotted
###
@plain = https://example.net/tenantvalue
GET {{{{plain}}}}/items
""")
    _mk(tmp_path, "http-client.private.env.json", json.dumps({"dev": {"token": s}}))
    r = extract_http.extract(tmp_path)
    nodes, _ = _index(r)
    w = nodes["httprequest:svc/secrets.http#withSecrets"]
    assert w["route_shape"] == "/{}/{}/{}/items" and w["uses_auth"] is True
    assert w["headers"] == ["Authorization", "X-Api-Key", "Cookie", "X-Custom"]
    assert nodes["httprequest:svc/secrets.http#2"]["route_shape"] == "/hooks/{}/send"
    assert nodes["httprequest:svc/secrets.http#2"]["uses_auth"] is True
    assert len([n for n in nodes if n.startswith("httprequest:")]) == 8   # curl is not graphed
    blob = json.dumps(r)
    assert s not in blob and s[3:15] not in blob
    for value in ("example.net", "admin", "Basic", "Bearer", "api_key", "sig=", "8443",
                  "session=", "title", "comment", "qsvalue", "fragvalue", "qsredirect",
                  "qslocal", "qsdotted", "tenantvalue"):
        assert value not in blob, value


def test_auth_header_detection():
    for name in ("Authorization", "x-api-key", "X-Auth-Token", "Proxy-Authorization",
                 "Ocp-Apim-Subscription-Key", "X-Functions-Key", "Cookie", "X-Session-Id"):
        assert extract_http._is_auth_header(name), name
    for name in ("Accept", "Content-Type", "If-Match", "X-Request-Id", "User-Agent"):
        assert not extract_http._is_auth_header(name), name


def test_exclude_hidden_and_skip_dirs(tmp_path):
    _mk(tmp_path, "keep/a.http", "GET /a\n")
    _mk(tmp_path, "node_modules/pkg/b.http", "GET /b\n")
    _mk(tmp_path, ".vscode/c.http", "GET /c\n")
    _mk(tmp_path, "skipme/d.http", "GET /d\n")
    _mk(tmp_path, "keep/readme.txt", "GET /e\n")
    nodes, _ = _index(extract_http.extract(tmp_path, exclude=frozenset({"skipme"})))
    assert {n["source_file"] for n in nodes.values()} == {"keep/a.http"}
    nodes, _ = _index(extract_http.extract(tmp_path, include=frozenset({".vscode"})))
    assert "httpfile:.vscode/c.http" in nodes


def test_malformed_oversized_and_binary_input(tmp_path, monkeypatch):
    _mk(tmp_path, "ok.http", "GET /ok\n")
    _mk(tmp_path, "junk.http", "\x00\x01 GET\n### \n@=\n@ = x\n# @name\nPOST\n{{\n}}}}{{\n> {%\n")
    (tmp_path / "bin.rest").write_bytes(bytes(range(256)) * 20)
    _mk(tmp_path, "big.http", "GET /big\n" + "#" * 600)
    monkeypatch.setattr(extract_http, "MAX_BYTES", 500)
    r = extract_http.extract(tmp_path)          # must not raise
    nodes, _ = _index(r)
    assert "httprequest:ok.http#1" in nodes and "httpfile:big.http" not in nodes
    assert "httpfile:junk.http" in nodes


def test_a_file_that_trips_the_pass_is_dropped_whole(tmp_path, monkeypatch):
    _mk(tmp_path, "a.http", "GET /a\n")
    _mk(tmp_path, "b.http", "GET /boom\n")
    real = extract_http._route

    def boom(url, fv):
        if "boom" in url:
            raise RuntimeError("unforeseen")
        return real(url, fv)
    monkeypatch.setattr(extract_http, "_route", boom)
    r = extract_http.extract(tmp_path)
    assert {n["id"] for n in r["nodes"]} == {"httpfile:a.http", "httprequest:a.http#1"}
    assert all(e["source"] != "httpfile:b.http" for e in r["edges"])


def test_request_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_http, "MAX_REQUESTS", 3)
    _mk(tmp_path, "many.http", "###\n".join(f"GET /r{chr(97 + i)}\n" for i in range(10)))
    nodes, _ = _index(extract_http.extract(tmp_path))
    assert len([n for n in nodes if n.startswith("httprequest:")]) == 3


def test_linear_time_on_hostile_input(tmp_path):
    # unclosed bindings, endless headers and a giant URL must stay linear
    text = ("GET /a/" + "{{" * 150_000 + "\n" + "X-H" * 40_000 + ":" + " " * 100_000
            + "\n###\n" + "GET " + "{{x}}" * 60_000 + "\nA: " + "{{" * 100_000 + "\n"
            + "###\n@" + "v" * 200_000 + "\n###\n# @name " + "n" * 200_000 + "\n"
            + "GET {{" + "a." * 100_000 + "response}}\n")
    assert len(text) < extract_http.MAX_BYTES
    _mk(tmp_path, "h.http", text)
    t = time.monotonic()
    nodes, _ = _index(extract_http.extract(tmp_path))
    assert time.monotonic() - t < 10
    assert {"httprequest:h.http#1", "httprequest:h.http#2", "httprequest:h.http#3"} <= set(nodes)


def test_filesystem_lookups_are_bounded(tmp_path, monkeypatch):
    # thousands of `< ./x` lines: each path is looked up once, at most MAX_PROBES per file
    _mk(tmp_path, "b0.json", "{}")
    lines = "".join(f"< ./b{i % 3000}.json\n" for i in range(9000))   # MAX_LOCAL per request
    many = "".join(f"###\nPOST /r{chr(97 + n % 26)}\n\n"
                   + "".join(f"< ./c{n}-{i}.json\n" for i in range(40)) for n in range(100))
    _mk(tmp_path, "a.http", "POST /x\n\n" + lines + "###\nPOST /y\n\n" + lines + many)
    calls = []
    real = extract_http._kind

    def counting(root, rel):
        calls.append(rel)
        return real(root, rel)
    monkeypatch.setattr(extract_http, "_kind", counting)
    r = extract_http.extract(tmp_path)
    assert len(calls) <= extract_http.MAX_PROBES and len(set(calls)) == len(calls)
    assert ("httprequest:a.http#1", "body_from", "file:b0.json") in _index(r)[1]


def test_deterministic(tmp_path):
    _mk(tmp_path, "x/items.http", SAMPLE)
    _mk(tmp_path, "y/z.rest", "GET /z\n")
    assert json.dumps(extract_http.extract(tmp_path)) == json.dumps(extract_http.extract(tmp_path))


def test_symlink_out_of_root_is_not_followed(tmp_path):
    outside = tmp_path / "outside"
    _mk(outside, "leak.http", "GET /leak\n")
    _mk(outside, "dir/leak2.http", "GET /leak2\n")
    _mk(outside, "secret.json", "{}")
    root = tmp_path / "repo"
    _mk(root, "a.http", "POST /a\n\n< ./body.json\n")
    try:
        os.symlink(outside / "leak.http", root / "leak.http")
        os.symlink(outside / "dir", root / "dir", target_is_directory=True)
        os.symlink(outside / "secret.json", root / "body.json")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    r = extract_http.extract(root)
    assert not any("leak" in n["id"] or "body.json" in n["id"] for n in r["nodes"])
    assert "httprequest:a.http#1" in {n["id"] for n in r["nodes"]}


def test_no_request_files_no_output(tmp_path):
    _mk(tmp_path, "src/app.py", "print('GET /x')\n")
    assert extract_http.extract(tmp_path) == {"nodes": [], "edges": []}
