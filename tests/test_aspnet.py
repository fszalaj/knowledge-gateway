"""ASP.NET Core extractor: controller and minimal-API endpoints, and the clients that call them."""
import json
import os
import time
import tracemalloc

import pytest

from gateway.codegraph import extract_appsmith, extract_aspnet

SECRET = "zz9-planted-aspnet-s3cr3t-c41f"


def _mk(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _index(r):
    nodes = {n["id"]: n for n in r["nodes"]}
    rels = {(e["source"], e["relation"], e["target"]) for e in r["edges"]}
    return nodes, rels


CSPROJ = '<Project Sdk="Microsoft.NET.Sdk.Web">\n  <PropertyGroup>\n' \
         '    <TargetFramework>net8.0</TargetFramework>\n  </PropertyGroup>\n</Project>\n'

WIDGETS = """using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Authorization;

namespace Shop.Api.Controllers
{
    // [HttpGet("commented-out")] must not count
    [Route("api/[controller]")]
    [ApiController]
    [Authorize]
    public class WidgetsController : ControllerBase
    {
        private readonly IWidgetStore _store;

        public WidgetsController(IWidgetStore store) { _store = store; }

        [HttpGet]
        [AllowAnonymous]
        public async Task<IActionResult> ListAsync([FromQuery] int page = 1)
        {
            var s = "[HttpPost(\\"not-an-attribute\\")]";
            return Ok(await _store.ListAsync(page));
        }

        [HttpGet("{id:int}")]
        public IActionResult Get(int id) => Ok(_store.Get(id));

        [HttpPost]
        public IActionResult Create([FromBody] WidgetDto dto)
        {
            if (dto == null) { return BadRequest(); }
            return Ok();
        }

        [HttpPut("{id}")]
        [HttpPatch("{id}")]
        public IActionResult Update(int id, [FromBody] WidgetDto dto) => NoContent();

        [HttpDelete("{id}")]
        public IActionResult Delete(int id) => NoContent();

        [HttpGet("[action]/{name}")]
        public IActionResult SearchAsync(string name) => Ok();

        [Route("~/status")]
        [HttpGet]
        public IActionResult Status() => Ok();

        [HttpGet("/health/live")]
        public IActionResult Live() => Ok();

        [Route("legacy")]
        public IActionResult Legacy() => Ok();

        [NonAction]
        [HttpGet("hidden")]
        public IActionResult Hidden() => Ok();

        [HttpGet("private")]
        private IActionResult NotAnAction() => Ok();

        public IActionResult Conventional() => Ok();
    }
}
"""


def _repo(root):
    _mk(root, "src/Shop.Api/Shop.Api.csproj", CSPROJ)
    _mk(root, "src/Shop.Api/Controllers/WidgetsController.cs", WIDGETS)
    _mk(root, "src/Shop.Api/Program.cs", """var builder = WebApplication.CreateBuilder(args);
var app = builder.Build();

app.MapGet("/", () => "ok");
app.MapGet("/ping/{n:int}", (int n) => n).AllowAnonymous();
app.MapPost("/orders", CreateOrder).RequireAuthorization();

var api = app.MapGroup("/api/v2").RequireAuthorization();
var orders = api.MapGroup("orders");
orders.MapGet("/", Handlers.ListOrders);
orders.MapDelete("{id}", (int id) => Results.NoContent());
orders.MapMethods("/{id}/state", new[] { "PUT", HttpMethods.Patch }, Handlers.SetState);
app.MapGroup("/public").MapGet("/info", [AllowAnonymous] () => "x");
app.Map("/any", () => "x");
app.MapHealthChecks("/healthz");
app.MapGet($"/interp/{app}", () => "x");
// app.MapGet("/commented", () => "x");
var s = "app.MapGet(\\"/in-a-string\\", () => 1)";
app.MapControllers();
app.Run();
""")


def test_controller_endpoints(tmp_path):
    _repo(tmp_path)
    nodes, rels = _index(extract_aspnet.extract(tmp_path))
    eps = {i for i in nodes if i.startswith("endpoint:")}
    for want in ("GET /api/Widgets", "GET /api/Widgets/{}", "POST /api/Widgets",
                 "PUT /api/Widgets/{}", "PATCH /api/Widgets/{}", "DELETE /api/Widgets/{}",
                 "GET /api/Widgets/Search/{}", "GET /status", "GET /health/live",
                 "ANY /api/Widgets/legacy"):
        assert f"endpoint:{want}" in eps, want
    blob = " ".join(eps)
    for absent in ("hidden", "private", "commented", "not-an-attribute", "Conventional"):
        assert absent not in blob
    g = nodes["endpoint:GET /api/Widgets/{}"]
    assert g["type"] == "endpoint" and g["file_type"] == "aspnet"
    assert (g["http_method"], g["route"], g["kind"]) == ("GET", "/api/Widgets/{}", "controller")
    assert (g["controller"], g["action"]) == ("WidgetsController", "Get")
    assert g["source_file"] == "src/Shop.Api/Controllers/WidgetsController.cs"
    assert g["source_location"] == "L25"
    assert g["auth"] == "required"                                    # class [Authorize]
    assert nodes["endpoint:GET /api/Widgets"]["auth"] == "anonymous"  # method [AllowAnonymous]
    assert nodes["endpoint:GET /api/Widgets"]["action"] == "ListAsync"


def test_minimal_api_endpoints(tmp_path):
    _repo(tmp_path)
    nodes, _ = _index(extract_aspnet.extract(tmp_path))
    eps = {i[len("endpoint:"):]: n for i, n in nodes.items() if i.startswith("endpoint:")}
    for want in ("GET /", "GET /ping/{}", "POST /orders", "GET /api/v2/orders",
                 "DELETE /api/v2/orders/{}", "PUT /api/v2/orders/{}/state",
                 "PATCH /api/v2/orders/{}/state", "GET /public/info", "ANY /any", "ANY /healthz"):
        assert want in eps, want
    assert not [k for k in eps if "interp" in k or "commented" in k or "in-a-string" in k]
    assert eps["GET /ping/{}"]["auth"] == "anonymous"
    assert eps["POST /orders"]["auth"] == "required"
    assert eps["POST /orders"]["action"] == "CreateOrder"
    assert eps["GET /api/v2/orders"]["auth"] == "required"            # inherited from the group
    assert eps["GET /api/v2/orders"]["action"] == "ListOrders"
    assert eps["GET /public/info"]["auth"] == "anonymous"             # [AllowAnonymous] lambda
    assert "auth" not in eps["GET /"]
    assert eps["GET /"]["kind"] == "minimal" and eps["GET /"]["source_location"] == "L4"


def test_handled_by_exposes_and_typed_module(tmp_path):
    _repo(tmp_path)
    nodes, rels = _index(extract_aspnet.extract(tmp_path))
    mod = "module:src/Shop.Api/Controllers/WidgetsController.cs"
    assert ("endpoint:GET /api/Widgets/{}", "handled_by", mod) in rels
    assert ("endpoint:GET /ping/{}", "handled_by", "module:src/Shop.Api/Program.cs") in rels
    # the exact node the tree-sitter pass writes for a .cs file
    assert nodes[mod] == {"id": mod, "label": mod[len("module:"):], "type": "module",
                          "file_type": "csharp", "source_file": mod[len("module:"):]}
    app = "aspnetapp:src/Shop.Api"
    assert nodes[app]["type"] == "aspnet_app" and nodes[app]["label"] == "Shop.Api"
    assert (app, "exposes", "endpoint:GET /api/Widgets/{}") in rels
    assert (app, "exposes", "endpoint:GET /ping/{}") in rels
    assert (app, "defined_by", "project:src/Shop.Api/Shop.Api.csproj") in rels
    assert nodes["project:src/Shop.Api/Shop.Api.csproj"]["type"] == "dotnet_project"
    r = extract_aspnet.extract(tmp_path)
    assert all(e["confidence"] == "EXTRACTED" for e in r["edges"])
    # every edge end is a node the pass typed itself
    assert all(e["source"] in nodes and e["target"] in nodes for e in r["edges"])


def test_no_csproj_no_app_node(tmp_path):
    _mk(tmp_path, "Api/OrdersController.cs",
        '[ApiController]\n[Route("orders")]\npublic class OrdersController : ControllerBase\n'
        '{\n    [HttpGet] public IActionResult List() => Ok();\n}\n')
    nodes, rels = _index(extract_aspnet.extract(tmp_path))
    assert "endpoint:GET /orders" in nodes
    assert not [i for i in nodes if i.startswith("aspnetapp:")]
    assert not [r for r in rels if r[1] == "exposes"]
    # a project at the repository root is "."
    _mk(tmp_path, "Root.csproj", CSPROJ)
    nodes, rels = _index(extract_aspnet.extract(tmp_path))
    assert ("aspnetapp:.", "exposes", "endpoint:GET /orders") in rels
    assert ("aspnetapp:.", "defined_by", "project:Root.csproj") in rels


def test_nearest_project_owns_the_file(tmp_path):
    _mk(tmp_path, "All.csproj", CSPROJ)
    _mk(tmp_path, "sub/Sub.csproj", CSPROJ)
    _mk(tmp_path, "sub/C.cs", 'app.MapGet("/sub", () => 1);\n')
    _mk(tmp_path, "top/C.cs", 'app.MapGet("/top", () => 1);\n')
    _, rels = _index(extract_aspnet.extract(tmp_path))
    assert ("aspnetapp:sub", "exposes", "endpoint:GET /sub") in rels
    assert ("aspnetapp:.", "exposes", "endpoint:GET /top") in rels
    assert ("aspnetapp:.", "exposes", "endpoint:GET /sub") not in rels


def test_area_version_consts_and_inherited_routes(tmp_path):
    _mk(tmp_path, "Routes.cs", """namespace Shop;
public static class Routes
{
    public const string Base = "api/" + "catalog";
    public const string Items = Base + "/items", Other = nameof(Other);
    public const string Dup = "one";
}
public static class More { public const string Dup = "two"; }
""")
    _mk(tmp_path, "Controllers.cs", """
[Area("Admin")]
[Route("[area]/[controller]")]
public class UsersController : Controller
{
    [HttpGet("{id}")] public IActionResult Get(string id) => View();
}

[ApiController]
[ApiVersion("1.0")]
[ApiVersion("2")]
[Route("api/v{version:apiVersion}/[controller]")]
public class ReportsController : ControllerBase
{
    [HttpGet] public IActionResult List() => Ok();
    [HttpGet("{id}")] [MapToApiVersion("3.0")] public IActionResult Get(int id) => Ok();
}

[ApiVersion("1.5")]
[Route("api/v{version:apiVersion}/beta")]
public class BetaController : ControllerBase
{
    [HttpGet] public IActionResult List() => Ok();
}

[Route(Routes.Items)]
public class ItemsController : ControllerBase
{
    [HttpGet] public IActionResult List() => Ok();
    [HttpGet(Routes.Other)] public IActionResult Other() => Ok();
}

[Route(Dup)]
public class DupController : ControllerBase { [HttpGet] public IActionResult List() => Ok(); }

[Route(SomewhereElse.Unknown)]
public class UnknownController : ControllerBase { [HttpGet("x")] public IActionResult X() => Ok(); }

[ApiController]
[Route("api/[controller]")]
[Authorize]
public abstract class ApiBaseController : ControllerBase
{
    [HttpGet("base-only")] public IActionResult BaseOnly() => Ok();
}

public class InvoicesController : ApiBaseController
{
    [HttpGet] public IActionResult List() => Ok();
}

[NonController]
[Route("never")]
public class NeverController : ControllerBase { [HttpGet] public IActionResult X() => Ok(); }

public interface IThingController { [HttpGet("iface")] IActionResult X(); }
""")
    nodes, _ = _index(extract_aspnet.extract(tmp_path))
    eps = {i[len("endpoint:"):] for i in nodes if i.startswith("endpoint:")}
    assert "GET /Admin/Users/{}" in eps
    assert {"GET /api/v1/Reports", "GET /api/v2/Reports", "GET /api/v3/Reports/{}"} <= eps
    assert "GET /api/v1/Reports/{}" not in eps                   # MapToApiVersion narrows it
    assert "GET /api/{}/beta" in eps                             # 1.5 is not a plain major
    assert {"GET /api/catalog/items", "GET /api/catalog/items/Other"} <= eps
    assert "GET /api/Invoices" in eps                            # [Route] inherited
    assert nodes["endpoint:GET /api/Invoices"]["auth"] == "required"
    joined = " ".join(eps)
    for absent in ("one", "two", "Unknown", "base-only", "never", "iface", "/x"):
        assert absent not in joined, absent


def test_route_shape_rule():
    rs = extract_aspnet.route_shape
    assert rs("api/[x]/{id:int}/{**slug}/{name?}") == "/api/{}/{}/{}/{}"
    assert rs("~/a/b/") == "/a/b"
    assert rs("") == "/"
    assert rs("files/{p:regex(^a/b$)}/x") == "/files/{}/x"
    assert rs("hooks/T0K3N1234567/send") == "/hooks/{}/send"
    assert rs("a/" + "x" * 30) == "/a/{}"
    assert rs("v{version:apiVersion}/a", "2") == "/v2/a"
    assert rs("v{version:apiVersion}/a") == "/{}/a"
    assert rs(None) is None


def test_no_values_leak(tmp_path):
    _mk(tmp_path, "App.csproj", CSPROJ.replace("</PropertyGroup>",
                                               f"<UserSecretsId>{SECRET}</UserSecretsId></PropertyGroup>"))
    _mk(tmp_path, "appsettings.json", json.dumps({"Jwt": {"Secret": SECRET}}))
    _mk(tmp_path, "Consts.cs", f"""
public static class Keys {{ public const string ApiKey = "{SECRET}"; public const string Hook = "hooks/{SECRET}"; }}
""")
    _mk(tmp_path, "LeakController.cs", f"""
// token {SECRET}
/* {SECRET} */
[ApiController]
[Route("api/{SECRET}/[controller]")]
[Authorize(Policy = "{SECRET}", Roles = Keys.ApiKey)]
public class LeakController : ControllerBase
{{
    private const string Inline = "{SECRET}";
    [HttpGet("{{id}}/{SECRET}?key={SECRET}")]
    [ProducesResponseType(typeof(string), 200, Description = "{SECRET}")]
    public IActionResult Get(string id)
    {{
        Request.Headers["X-Api-Key"] = "{SECRET}";
        var body = $"{{{{\\"password\\": \\"{SECRET}\\"}}}}";
        var url = "https://user:{SECRET}@host.example.com/x?token={SECRET}";
        return Ok(@"{SECRET}");
    }}
    [HttpPost(Keys.Hook)] public IActionResult Hook() => Ok();
}}
""")
    _mk(tmp_path, "Program.cs", f"""
app.MapGet("/svc/{SECRET}/x?k={SECRET}", () => "{SECRET}").WithName("{SECRET}");
app.MapGroup("/{SECRET}").MapPost("/y", () => \"\"\"{SECRET}\"\"\");
app.MapPut(Keys.ApiKey, () => 1);
""")
    r = extract_aspnet.extract(tmp_path)
    nodes, _ = _index(r)
    assert "endpoint:GET /api/{}/Leak/{}/{}" in nodes
    assert "endpoint:POST /api/{}/Leak/hooks/{}" in nodes
    assert "endpoint:GET /svc/{}/{}" in nodes and "endpoint:POST /{}/y" in nodes
    assert "endpoint:PUT /{}" in nodes
    assert SECRET not in json.dumps(r)


def test_malformed_input_never_raises(tmp_path, monkeypatch):
    _repo(tmp_path)
    _mk(tmp_path, "bad/Unterminated.cs", 'var x = @"never closed\n[Route("a")] class { ( [ {\n')
    _mk(tmp_path, "bad/Raw.cs", 'var x = """ never closed ' + "\n" * 5)
    _mk(tmp_path, "bad/Braces.cs", "}" * 5000 + "{" * 5000 + "[Route(" * 1000)
    _mk(tmp_path, "bad/Map.cs", "app.MapGet(" * 2000 + "app.MapGroup(" * 2000)
    (tmp_path / "bad/Binary.cs").write_bytes(bytes(range(256)) * 50)
    _mk(tmp_path, "bad/Interp.cs", '$"{ "nested " } {{ $@"x{y}" ' * 500)
    nodes, _ = _index(extract_aspnet.extract(tmp_path))   # must not raise
    assert "endpoint:GET /api/Widgets/{}" in nodes
    monkeypatch.setattr(extract_aspnet, "MAX_BYTES", 100)
    nodes, _ = _index(extract_aspnet.extract(tmp_path))
    assert not [i for i in nodes if i.startswith("endpoint:")]


def test_one_file_that_trips_the_scanner_costs_that_file(tmp_path, monkeypatch):
    _repo(tmp_path)
    real = extract_aspnet._scan_minimal

    def boom(toks, match, consts):
        if any(t[1] == "MapGroup" for t in toks):
            raise RuntimeError("unforeseen")
        return real(toks, match, consts)
    monkeypatch.setattr(extract_aspnet, "_scan_minimal", boom)
    nodes, _ = _index(extract_aspnet.extract(tmp_path))
    assert "endpoint:GET /api/Widgets/{}" in nodes
    assert not [i for i in nodes if "ping" in i]
    assert "module:src/Shop.Api/Program.cs" not in nodes


def test_pathological_input_is_linear(tmp_path):
    _mk(tmp_path, "A.cs", '"' * 300_000)
    _mk(tmp_path, "B.cs", "[" * 200_000 + "Route(" * 50_000)
    _mk(tmp_path, "C.cs", '@"' + '""' * 200_000)
    _mk(tmp_path, "D.cs", "/*" * 200_000 + "a.Map(" * 50_000 + "<" * 100_000)
    _mk(tmp_path, "E.cs", "class A : B<" * 20_000 + "{ public void M(" * 20_000)
    # nested, balanced calls: no Map call may rescan the arguments of the calls it encloses
    _mk(tmp_path, "F.cs", 'a.MapGet("/x", [Authorize] ' * 20_000 + "1" + ")" * 20_000 + ";")
    _mk(tmp_path, "G.cs", "var g = a.MapGroup(" * 20_000 + '"/p"' + ")" * 20_000 + ";")
    t = time.monotonic()
    extract_aspnet.extract(tmp_path)
    assert time.monotonic() - t < 20


def test_mapgroup_prefixes_stay_bounded(tmp_path):
    # every group keeps its parent alive: an unbounded prefix grew memory with the square of
    # the number of `g = g.MapGroup(...)` lines. A const near MAX_STR, reassigned and chained.
    lit = "a" * 1000
    _mk(tmp_path, "R.cs", f'static class R {{ public const string A = "{lit}"; public const string S = "s"; }}\n')
    _mk(tmp_path, "P.cs", "var g = app.MapGroup(R.A);\n" + "g = g.MapGroup(R.A);\n" * 3000
        + 'g.MapGet("/x", H);\n')
    _mk(tmp_path, "Q.cs", "app" + ".MapGroup(R.A)" * 3000 + '.MapGet("/y", H);\n')
    _mk(tmp_path, "S.cs", "var s = app.MapGroup(R.S);\n" + "s = s.MapGroup(R.S);\n" * 3000
        + 's.MapGet("/z", H);\n')
    _mk(tmp_path, "T.cs", 'var t = app.MapGroup("/t");\nt = t.MapGroup("u");\nt.MapGet("/v", H);\n')
    tracemalloc.start()
    t = time.monotonic()
    nodes, _ = _index(extract_aspnet.extract(tmp_path))
    elapsed = time.monotonic() - t
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert elapsed < 20 and peak < 200 * 1024 * 1024
    eps = {i for i in nodes if i.startswith("endpoint:")}
    assert "endpoint:GET /t/u/v" in eps                 # a short chain still resolves
    assert not [e for e in eps if "/x" in e or "/y" in e or "/z" in e]   # too long / too deep
    assert all(len(e) < extract_aspnet.MAX_ROUTE + 20 for e in eps)


def test_map_branch_tuple_returns_numeric_versions(tmp_path):
    _mk(tmp_path, "Web/Web.csproj", CSPROJ)
    _mk(tmp_path, "Web/Program.cs", """var app = builder.Build();
app.Map("/branch", b => b.Run(async c => await c.Response.WriteAsync("x")));
app.Map("/branch2", (inner) => { inner.UseRouting(); });
app.Map("/delegate", async ctx => await ctx.Response.WriteAsync("x"));
app.Map("/typed", (HttpContext ctx) => "x");
app.Run();
""")
    _mk(tmp_path, "Web/ThingsController.cs", """[ApiController]
[Route("api/v{version:apiVersion}/things")]
[ApiVersion(2.0)]
[ApiVersion(3)]
public class ThingsController : ControllerBase
{
    [HttpGet("pair")]
    public (int, string) Pair() => (1, "a");
    [HttpGet("named")]
    public async Task<(int Count, string Name)> NamedAsync() => (1, "a");
    [HttpGet("maybe")]
    public static (int, int)? Nope() => null;
    [HttpGet("one")]
    [MapToApiVersion(3.0)]
    public int One() => 1;
    [HttpGet("generic")]
    public T Get<T>(int x) where T : class => default;
}
""")
    eps = {i[len("endpoint:"):] for i in _index(extract_aspnet.extract(tmp_path))[0]
           if i.startswith("endpoint:")}
    assert "ANY /delegate" in eps and "ANY /typed" in eps
    assert not [e for e in eps if "branch" in e]       # IApplicationBuilder.Map
    assert {"GET /api/v2/things/pair", "GET /api/v3/things/pair",
            "GET /api/v2/things/named", "GET /api/v3/things/one",
            "GET /api/v2/things/generic"} <= eps
    assert "GET /api/v2/things/one" not in eps         # MapToApiVersion(3.0) narrows it
    assert not [e for e in eps if "maybe" in e]        # static: not an action


def test_optional_trailing_segments_link_clients_that_omit_them(tmp_path):
    _mk(tmp_path, "Program.cs", 'app.MapGet("/items/{id?}", (int? id) => 1);\n'
                                'app.MapGet("/files/{**path}", (string path) => 1);\n'
                                'app.MapGet("/pages/{n=1}/x", (int n) => 1);\n')
    nodes, _ = _index(extract_aspnet.extract(tmp_path))
    assert nodes["endpoint:GET /items/{}"]["optional_segments"] == 1
    assert nodes["endpoint:GET /files/{}"]["optional_segments"] == 1
    assert "optional_segments" not in nodes["endpoint:GET /pages/{}/x"]   # not trailing
    clients = [{"id": f"httprequest:a.http#{i}", "method": "GET", "route_shape": p}
               for i, p in enumerate(("/items", "/items/{}", "/files", "/pages"), 1)]
    got = {(e["source"], e["target"]): e["match"]
           for e in extract_aspnet.link_calls(list(nodes.values()) + clients)}
    assert got[("httprequest:a.http#1", "endpoint:GET /items/{}")] == "exact"
    assert got[("httprequest:a.http#2", "endpoint:GET /items/{}")] == "exact"
    assert got[("httprequest:a.http#3", "endpoint:GET /files/{}")] == "exact"
    assert not [s for s, _ in got if s == "httprequest:a.http#4"]   # a middle segment is required
    assert extract_aspnet._optional_tail("a/{id:int?}/{*rest}") == 2
    assert extract_aspnet._optional_tail("a/{p:regex(^x?$)}") == 0


def test_pruning_symlinks_and_determinism(tmp_path):
    outside = tmp_path / "outside"
    _mk(outside, "LeakController.cs", '[Route("leak")] public class LeakController : ControllerBase '
                                      '{ [HttpGet] public IActionResult X() => Ok(); }')
    repo = tmp_path / "repo"
    _repo(repo)
    for d in ("bin", "obj", ".hidden", "skipme", "node_modules"):
        _mk(repo, f"src/{d}/Gen.cs", f'app.MapGet("/{d.strip(".")}", () => 1);\n')
    try:
        os.symlink(outside / "LeakController.cs", repo / "src/Link.cs")
        os.symlink(outside, repo / "src/linkdir", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    r = extract_aspnet.extract(repo, frozenset({"skipme"}))
    blob = json.dumps(r)
    ids = {n["id"] for n in r["nodes"]}
    assert "leak" not in blob.lower() and "Link.cs" not in blob
    for absent in ("bin", "obj", "hidden", "skipme", "node_modules"):
        assert f"endpoint:GET /{absent}" not in ids, absent
    assert "endpoint:GET /api/Widgets/{}" in ids
    assert json.dumps(extract_aspnet.extract(repo, frozenset({"skipme"}))) == blob
    kept = extract_aspnet.extract(repo, frozenset(), frozenset({"bin"}))
    assert "endpoint:GET /bin" in {n["id"] for n in kept["nodes"]}


def test_no_csharp_no_output(tmp_path):
    _mk(tmp_path, "app.py", "print('x')\n")
    assert extract_aspnet.extract(tmp_path) == {"nodes": [], "edges": []}


# --------------------------------------------------------------------------------------------
# post-merge hook

def _appsmith_app(root):
    def q(page, name, method, path):
        return json.dumps({"pluginId": "restapi-plugin", "pluginType": "API", "unpublishedAction": {
            "name": name, "pageId": page, "datasource": {"id": "Api", "name": "Api"},
            "actionConfiguration": {"httpMethod": method, "path": path,
                                    "headers": [{"key": "Authorization", "value": SECRET}]}}})
    _mk(root, "ui/metadata.json", json.dumps({"artifactJsonType": "APPLICATION"}))
    _mk(root, "ui/application.json", json.dumps({"name": "Ui"}))
    _mk(root, "ui/pages/P/P.json", json.dumps({"unpublishedPage": {"name": "P"}}))
    _mk(root, "ui/pages/P/queries/GetWidget/metadata.json",
        q("P", "GetWidget", "GET", "/api/widgets/{{Table1.selectedRow.id}}"))
    _mk(root, "ui/pages/P/queries/ListWidgets/metadata.json",
        q("P", "ListWidgets", "GET", f"/api/Widgets?token={SECRET}"))
    _mk(root, "ui/pages/P/queries/PostWidget/metadata.json", q("P", "PostWidget", "POST", "/Widgets"))
    _mk(root, "ui/pages/P/queries/Search/metadata.json",
        q("P", "Search", "GET", "{{appsmith.store.base}}/Widgets/Search/abc"))
    _mk(root, "ui/pages/P/queries/WrongVerb/metadata.json", q("P", "WrongVerb", "DELETE", "/status"))
    _mk(root, "ui/pages/P/queries/Nothing/metadata.json", q("P", "Nothing", "GET", "/nowhere/at/all"))


def _merged(root, extra=()):
    nodes = {}
    for frag in (extract_aspnet.extract(root), extract_appsmith.extract(root)):
        for n in frag["nodes"]:
            nodes.setdefault(n["id"], {}).update(n)
    return list(nodes.values()) + list(extra)


def test_link_calls_joins_clients_to_endpoints(tmp_path):
    _repo(tmp_path)
    _appsmith_app(tmp_path)
    http = [
        {"id": "httprequest:api.http#1", "type": "http_request", "method": "GET", "path": "/ping/{}"},
        {"id": "httprequest:api.http#2", "type": "http_request", "http_method": "put",
         "path": "{}/api/v2/orders/{}/state"},
        {"id": "httprequest:api.http#3", "type": "http_request", "method": "GET", "path": "/"},
        {"id": "httprequest:api.http#4", "type": "http_request", "method": "GET"},
        {"id": "httprequest:api.http#5", "type": "http_request", "method": "GET",
         "path": "/api/widgets/abc"},
        {"id": "notaclient:x", "method": "GET", "path": "/ping/{}"},
    ]
    out = extract_aspnet.link_calls(_merged(tmp_path, http))
    got = {(e["source"], e["target"]): e["match"] for e in out}
    assert all(e["relation"] == "calls_api" and e["confidence"] == "INFERRED" for e in out)
    Q = "appsmithquery:Ui/P/"
    assert got[(Q + "GetWidget", "endpoint:GET /api/Widgets/{}")] == "exact"   # case-insensitive
    assert got[("httprequest:api.http#5", "endpoint:GET /api/Widgets/{}")] == "pattern"
    assert got[(Q + "ListWidgets", "endpoint:GET /api/Widgets")] == "exact"
    assert got[(Q + "PostWidget", "endpoint:POST /api/Widgets")] == "suffix"
    # a base-URL binding: `{}` stands for the missing `api` segment
    assert got[(Q + "Search", "endpoint:GET /api/Widgets/Search/{}")] == "pattern"
    assert got[("httprequest:api.http#1", "endpoint:GET /ping/{}")] == "exact"
    assert got[("httprequest:api.http#2", "endpoint:PUT /api/v2/orders/{}/state")] == "suffix"
    sources = {s for s, _ in got}
    for none in (Q + "WrongVerb", Q + "Nothing", "httprequest:api.http#3", "httprequest:api.http#4",
                 "notaclient:x"):
        assert none not in sources, none
    # each client lands on its best match only
    assert [t for s, t in got if s == Q + "GetWidget"] == ["endpoint:GET /api/Widgets/{}"]
    assert extract_aspnet.link_calls(_merged(tmp_path, http)) == out
    assert SECRET not in json.dumps(out)


def test_link_calls_any_ambiguity_and_bad_nodes():
    ep = lambda m, r: {"id": f"endpoint:{m} {r}", "type": "endpoint", "http_method": m, "route": r}
    client = {"id": "httprequest:x", "method": "GET", "path": "/a/{}"}
    nodes = [ep("ANY", "/a/b"), client, None, {"id": 5}, {"id": "endpoint:GET /z"}]
    assert [e["target"] for e in extract_aspnet.link_calls(nodes)] == ["endpoint:ANY /a/b"]
    # a verb-specific endpoint wins over ANY
    nodes.append(ep("GET", "/a/c"))
    assert [e["target"] for e in extract_aspnet.link_calls(nodes)] == ["endpoint:GET /a/c"]
    many = [ep("GET", f"/a/{w}") for w in ("b", "c", "d", "e", "f")]
    assert extract_aspnet.link_calls(many + [client]) == []           # too ambiguous
    assert extract_aspnet.link_calls([]) == []
