"""Appsmith extractor: which widget runs which query, and what each query talks to."""
import json
import os

import pytest

from gateway.codegraph import extract_appsmith

SECRET = "zz-planted-s3cr3t-7f3a9c"


def _mk(root, rel, obj):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
    return p


def _query(page, name, method, path, *, ds="Backend", body="", params=None, headers=None,
           on_load=False):
    return {
        "id": f"{page}_{name}", "pluginId": "restapi-plugin", "pluginType": "API",
        "unpublishedAction": {
            "name": name, "pageId": page,
            "runBehaviour": "ON_PAGE_LOAD" if on_load else "MANUAL",
            "datasource": {"id": ds, "name": ds, "pluginId": "restapi-plugin"},
            "actionConfiguration": {
                "httpMethod": method, "path": path, "body": body,
                "headers": headers or [], "queryParameters": params or []},
        },
    }


def _widget(name, wtype, wid, parent="0", **props):
    trig = [{"key": k} for k in props if k.startswith("on")]
    return {"widgetName": name, "type": wtype, "widgetId": wid, "parentId": parent,
            "dynamicTriggerPathList": trig, **props}


def _app(root, base=""):
    b = f"{base}/" if base else ""
    _mk(root, f"{b}metadata.json", {"artifactJsonType": "APPLICATION", "fileFormatVersion": 5})
    _mk(root, f"{b}application.json", {"name": "Shop", "pages": [
        {"id": "Home", "isDefault": True}, {"id": "Orders", "isDefault": False}]})
    _mk(root, f"{b}datasources/Backend.json", {
        "name": "Backend", "pluginId": "restapi-plugin",
        "datasourceConfiguration": {"url": f"https://{SECRET}.example.com",
                                    "authentication": {"password": SECRET}}})
    pg = f"{b}pages/Home"
    _mk(root, f"{pg}/Home.json", {"unpublishedPage": {"name": "Home", "layouts": [
        {"dsl": {"widgetName": "MainContainer", "type": "CANVAS_WIDGET", "widgetId": "0"}}]}})
    _mk(root, f"{pg}/queries/Get_Items/metadata.json", _query(
        "Home", "Get_Items", "GET", f"/api/items?token={SECRET}",
        params=[{"key": "page", "value": "{{Table_Items.pageNo}}"},
                {"key": "apikey", "value": SECRET}],
        headers=[{"key": "Authorization", "value": f"Bearer {SECRET}"}], on_load=True))
    _mk(root, f"{pg}/queries/Get_Items/Get_Items.txt", "")
    _mk(root, f"{pg}/queries/Save_Item/metadata.json", _query(
        "Home", "Save_Item", "PUT", "/api/items/{{Table_Items.selectedRow.id}}\n",
        body='{"name": "{{Input_Name.text}}", "pin": "' + SECRET + '"}'))
    _mk(root, f"{pg}/queries/Save_Item/Save_Item.txt",
        '{"name": "{{Input_Name.text}}", "pin": "' + SECRET + '"}')
    _mk(root, f"{pg}/queries/Ping/metadata.json", _query(
        "Home", "Ping", "GET", f"https://user:{SECRET}@{SECRET}.example.com/health#x", ds="Inline"))
    _mk(root, f"{pg}/widgets/Table_Items.json", _widget(
        "Table_Items", "TABLE_WIDGET_V2", "t1",
        tableData="{{Get_Items.data.items}}",
        onPageChange="{{Get_Items.run();}}",
        primaryColumns={"edit": {"onClick": "{{showModal(Dialog_Item.name);}}",
                                 "computedValue": "{{Table_Items.processedTableData.map(r => r)}}"}},
        dynamicBindingPathList=[{"key": "tableData"}]))
    _mk(root, f"{pg}/widgets/Dialog_Item/Dialog_Item.json", _widget(
        "Dialog_Item", "MODAL_WIDGET", "m1",
        children=[{"widgetName": "Canvas1", "type": "CANVAS_WIDGET", "widgetId": "c1",
                   "parentId": "m1"}]))
    _mk(root, f"{pg}/widgets/Dialog_Item/Input_Name.json", _widget(
        "Input_Name", "INPUT_WIDGET_V2", "i1", parent="c1", defaultText=SECRET))
    _mk(root, f"{pg}/widgets/Dialog_Item/Button_Save.json", _widget(
        "Button_Save", "BUTTON_WIDGET", "b1", parent="c1",
        isDisabled="{{Input_Name.text.length < 1}}",
        onClick="{{Save_Item.run().then(() => { Get_Items.run(); closeModal(Dialog_Item.name); "
                "navigateTo('Orders'); });}}",
        text="Save"))
    _mk(root, f"{pg}/widgets/Text_Total.json", _widget(
        "Text_Total", "TEXT_WIDGET", "x1", text=f"Total: {{{{Helpers.total()}}}} {SECRET}"))
    _mk(root, f"{pg}/jsobjects/Helpers/metadata.json", {
        "unpublishedCollection": {"name": "Helpers", "pageId": "Home", "pluginType": "JS"}})
    _mk(root, f"{pg}/jsobjects/Helpers/Helpers.js",
        "export default {\n  key: '" + SECRET + "',\n"
        "  total() { return Get_Items.data.length + Input_Name.text.length },\n"
        "  async refresh() { await Get_Items.run(); }\n}\n")
    _mk(root, f"{b}pages/Orders/Orders.json", {"unpublishedPage": {"name": "Orders"}})
    # Same query name on another page: a different node, never confused with Home's.
    _mk(root, f"{b}pages/Orders/queries/Get_Items/metadata.json", _query(
        "Orders", "Get_Items", "GET", "/api/orders"))


def _index(r):
    nodes = {n["id"]: n for n in r["nodes"]}
    rels = {(e["source"], e["relation"], e["target"]) for e in r["edges"]}
    return nodes, rels


A = "Shop"
W = f"appsmithwidget:{A}/Home/"
Q = f"appsmithquery:{A}/Home/"


def test_nodes_and_attributes(tmp_path):
    _app(tmp_path)
    nodes, _ = _index(extract_appsmith.extract(tmp_path))
    assert nodes[f"appsmith:{A}"]["type"] == "appsmith_app"
    assert nodes[f"appsmithpage:{A}/Home"]["default"] is True
    assert "default" not in nodes[f"appsmithpage:{A}/Orders"]
    q = nodes[Q + "Get_Items"]
    assert (q["plugin_type"], q["http_method"], q["path"], q["on_page_load"]) == \
        ("API", "GET", "/api/items", True)
    assert q["source_file"] == "pages/Home/queries/Get_Items/metadata.json"
    assert nodes[Q + "Save_Item"]["path"] == "/api/items/{}"
    assert nodes[Q + "Ping"]["path"] == "/health"        # scheme, credentials, host, fragment gone
    assert nodes[f"appsmithdatasource:{A}/Backend"]["plugin"] == "restapi-plugin"
    assert nodes[W + "Button_Save"]["widget_type"] == "BUTTON_WIDGET"
    assert nodes[f"appsmithjs:{A}/Home/Helpers"]["type"] == "appsmith_jsobject"
    assert f"appsmithquery:{A}/Orders/Get_Items" in nodes
    assert not any("Canvas" in n or "MainContainer" in n for n in nodes)  # layout plumbing


def test_structure_edges(tmp_path):
    _app(tmp_path)
    _, rels = _index(extract_appsmith.extract(tmp_path))
    assert (f"appsmith:{A}", "contains", f"appsmithpage:{A}/Home") in rels
    assert (f"appsmith:{A}", "contains", f"appsmithdatasource:{A}/Backend") in rels
    assert (f"appsmithpage:{A}/Home", "contains", Q + "Get_Items") in rels
    assert (f"appsmithpage:{A}/Home", "contains", W + "Table_Items") in rels
    # nested through the modal's canvas, not hung off the page
    assert (W + "Dialog_Item", "contains", W + "Button_Save") in rels
    assert (f"appsmithpage:{A}/Home", "contains", W + "Button_Save") not in rels
    assert (Q + "Get_Items", "uses_datasource", f"appsmithdatasource:{A}/Backend") in rels
    assert (f"appsmithjs:{A}/Home/Helpers", "implemented_by",
            "module:pages/Home/jsobjects/Helpers/Helpers.js") in rels


def test_jsobject_module_node_is_emitted_by_the_pass(tmp_path):
    # implemented_by must not rely on the tree-sitter pass for its target: under [graph-slim]
    # (or --languages without javascript) that would be an untyped placeholder
    _app(tmp_path)
    nodes, _ = _index(extract_appsmith.extract(tmp_path))
    rel = "pages/Home/jsobjects/Helpers/Helpers.js"
    assert nodes[f"module:{rel}"] == {"id": f"module:{rel}", "label": rel, "type": "module",
                                      "file_type": "javascript", "source_file": rel}


def test_binding_edges(tmp_path):
    _app(tmp_path)
    r = extract_appsmith.extract(tmp_path)
    _, rels = _index(r)
    js = f"appsmithjs:{A}/Home/Helpers"
    assert (W + "Button_Save", "triggers", Q + "Save_Item") in rels
    assert (W + "Button_Save", "triggers", Q + "Get_Items") in rels
    assert (W + "Table_Items", "triggers", Q + "Get_Items") in rels
    assert (W + "Table_Items", "reads", Q + "Get_Items") in rels
    assert (W + "Table_Items", "references", W + "Dialog_Item") in rels
    assert (W + "Button_Save", "references", W + "Input_Name") in rels
    assert (W + "Button_Save", "navigates_to", f"appsmithpage:{A}/Orders") in rels
    assert (W + "Text_Total", "references", js) in rels      # a value binding, not an event
    assert (W + "Text_Total", "triggers", js) not in rels
    assert (Q + "Save_Item", "references", W + "Input_Name") in rels
    assert (Q + "Get_Items", "references", W + "Table_Items") in rels
    assert (js, "triggers", Q + "Get_Items") in rels
    assert (js, "reads", Q + "Get_Items") in rels
    assert (js, "references", W + "Input_Name") in rels
    # a widget mentioning itself is not an edge; names never leak across pages
    assert not any(s == t for s, _, t in rels)
    assert not any("/Orders/" in t for s, rel, t in rels if rel != "contains")
    for e in r["edges"]:
        assert e["confidence"] == ("INFERRED" if e["relation"] == "references" else "EXTRACTED")
    for n in r["nodes"]:
        assert {"id", "label", "type", "file_type", "source_file"} <= set(n)
        # the JS object's .js file is the one module node, in the tree-sitter pass's shape
        assert n["file_type"] == ("javascript" if n["id"].startswith("module:") else "appsmith")


def test_no_values_leak(tmp_path):
    _app(tmp_path)
    # a token carried in the path itself, and a scheme-less `user:pw@host/...` value
    _mk(tmp_path, "pages/Home/queries/Hook/metadata.json", _query(
        "Home", "Hook", "POST", f"/hooks/{SECRET}/send"))
    _mk(tmp_path, "pages/Home/queries/Bare/metadata.json", _query(
        "Home", "Bare", "GET", f"user:{SECRET}@api.example.com/v1/items"))
    _mk(tmp_path, "pages/Home/queries/Bot/metadata.json", _query(
        "Home", "Bot", "GET", "/bot123456:zzPlantedBotToken/sendMessage"))
    r = extract_appsmith.extract(tmp_path)
    nodes, _ = _index(r)
    assert nodes[Q + "Hook"]["path"] == "/hooks/{}/send"       # the shape survives
    assert nodes[Q + "Bare"]["path"] == "/v1/items"
    assert nodes[Q + "Bot"]["path"] == "/{}/sendMessage"
    blob = json.dumps(r)
    assert SECRET not in blob and "zzPlantedBotToken" not in blob
    for value in ("example.com", "Bearer", "Authorization", "apikey", "token=", "Total:",
                  "export default", "application/json"):
        assert value not in blob


def test_app_in_subdirectory_and_detection(tmp_path):
    _app(tmp_path, "apps/shop")
    # look-alikes: no marker, or no pages/ directory
    _mk(tmp_path, "other/metadata.json", {"name": "x"})
    _mk(tmp_path, "other/application.json", {"name": "Fake"})
    (tmp_path / "other/pages").mkdir()
    _mk(tmp_path, "third/metadata.json", {"fileFormatVersion": 5})
    _mk(tmp_path, "third/application.json", {"name": "NoPages"})
    nodes, _ = _index(extract_appsmith.extract(tmp_path))
    assert nodes[f"appsmith:{A}"]["source_file"] == "apps/shop/application.json"
    assert nodes[Q + "Get_Items"]["source_file"] == "apps/shop/pages/Home/queries/Get_Items/metadata.json"
    assert not any("Fake" in n or "NoPages" in n for n in nodes)


def test_name_fallback_and_exclude(tmp_path):
    _mk(tmp_path, "ui/metadata.json", {"artifactJsonType": "APPLICATION"})
    _mk(tmp_path, "ui/application.json", {"pages": []})
    _mk(tmp_path, "ui/pages/Main/Main.json", {"unpublishedPage": {"name": "Main"}})
    _mk(tmp_path, "skipme/metadata.json", {"artifactJsonType": "APPLICATION"})
    _mk(tmp_path, "skipme/application.json", {"name": "Hidden"})
    (tmp_path / "skipme/pages").mkdir()
    nodes, _ = _index(extract_appsmith.extract(tmp_path, exclude=frozenset({"skipme"})))
    assert "appsmith:ui" in nodes and "appsmithpage:ui/Main" in nodes
    assert "appsmith:Hidden" not in nodes


def test_older_flat_layout_and_embedded_dsl(tmp_path):
    _mk(tmp_path, "metadata.json", {"fileFormatVersion": 3})
    _mk(tmp_path, "application.json", {"name": "Old"})
    _mk(tmp_path, "pages/P/P.json", {"unpublishedPage": {"name": "P", "layouts": [{"dsl": {
        "widgetName": "MainContainer", "type": "CANVAS_WIDGET", "widgetId": "0", "children": [
            {"widgetName": "Btn", "type": "BUTTON_WIDGET", "widgetId": "b", "parentId": "0",
             "onClick": "{{Load.run()}}"}]}}]}})
    _mk(tmp_path, "pages/P/queries/Load.json", _query("P", "Load", "POST", "/load"))
    _mk(tmp_path, "pages/P/jsobjects/Util.js", "export default { go() { return Load.data } }")
    r = extract_appsmith.extract(tmp_path)
    _, rels = _index(r)
    assert ("appsmithwidget:Old/P/Btn", "triggers", "appsmithquery:Old/P/Load") in rels
    assert ("appsmithjs:Old/P/Util", "reads", "appsmithquery:Old/P/Load") in rels
    assert ("appsmithpage:Old/P", "contains", "appsmithwidget:Old/P/Btn") in rels


def test_malformed_and_oversized_files_are_skipped(tmp_path, monkeypatch):
    _app(tmp_path)
    _mk(tmp_path, "pages/Home/widgets/Broken.json", "{not json")
    _mk(tmp_path, "pages/Home/queries/Bad/metadata.json", "[1, 2")
    _mk(tmp_path, "datasources/Weird.json", "[]")
    monkeypatch.setattr(extract_appsmith, "_MAX_BYTES", 400)
    r = extract_appsmith.extract(tmp_path)    # must not raise
    nodes, _ = _index(r)
    assert f"appsmith:{A}" in nodes
    assert W + "Table_Items" not in nodes      # its file is over the patched cap


def test_wrong_shaped_json_is_skipped_not_fatal(tmp_path):
    # valid JSON of the wrong type, at every place the pass iterates or hashes a value
    _app(tmp_path)
    _mk(tmp_path, "application.json", {"name": "Shop", "pages": [{"id": ["x"], "isDefault": True}]})
    _mk(tmp_path, "pages/Orders/Orders.json", {"unpublishedPage": {"name": "Orders", "layouts": 3}})
    _mk(tmp_path, "pages/Home/widgets/Odd.json", {
        "widgetName": "Odd", "type": "TEXT_WIDGET", "widgetId": "o1", "parentId": ["x"],
        "dynamicTriggerPathList": [{"key": ["a"]}, 5], "onClick": "{{Get_Items.run()}}"})
    nodes, rels = _index(extract_appsmith.extract(tmp_path))
    assert W + "Odd" in nodes and (W + "Odd", "triggers", Q + "Get_Items") in rels
    assert f"appsmithpage:{A}/Orders" in nodes
    _mk(tmp_path, "application.json", {"name": "Shop", "pages": 5})
    assert f"appsmithpage:{A}/Home" in _index(extract_appsmith.extract(tmp_path))[0]


def test_deeply_nested_json_is_skipped_not_fatal(tmp_path):
    # json.loads raises RecursionError, not ValueError, on deep nesting; a widget just inside
    # the parser's limit must not overflow the pass's own walk either
    _app(tmp_path)
    _mk(tmp_path, "pages/Home/widgets/Deep.json", "[" * 100_000 + "]" * 100_000)
    _mk(tmp_path, "pages/Home/widgets/Nest.json",
        '{"widgetName": "Nest", "type": "TEXT_WIDGET", "x": ' + "[" * 800 + "]" * 800 + "}")
    nodes, _ = _index(extract_appsmith.extract(tmp_path))
    assert W + "Table_Items" in nodes and W + "Nest" in nodes
    _mk(tmp_path, "application.json", "[" * 100_000 + "]" * 100_000)
    app = tmp_path.resolve().name               # no readable name: the directory names the app
    assert f"appsmithpage:{app}/Home" in _index(extract_appsmith.extract(tmp_path))[0]


def test_an_app_that_trips_the_pass_is_dropped_whole(tmp_path, monkeypatch):
    # the backstop: whatever the export, one app never fails the pass, and leaves no half-app
    _app(tmp_path, "a")
    _mk(tmp_path, "b/metadata.json", {"artifactJsonType": "APPLICATION"})
    _mk(tmp_path, "b/application.json", {"name": "Other"})
    _mk(tmp_path, "b/pages/P/P.json", {"unpublishedPage": {"name": "P"}})
    real = extract_appsmith._action

    def boom(meta):
        if "unpublishedAction" in meta:      # only app "a" has queries
            raise RuntimeError("unforeseen")
        return real(meta)
    monkeypatch.setattr(extract_appsmith, "_action", boom)
    r = extract_appsmith.extract(tmp_path)
    ids = {n["id"] for n in r["nodes"]}
    assert ids == {"appsmith:Other", "appsmithpage:Other/P"}
    assert all(e["source"] in ids and e["target"] in ids for e in r["edges"])


def test_unclosed_bindings_cost_linear_time(tmp_path):
    # a lazy `{{.*?}}` regex is quadratic on unclosed `{{`; 400 KB of them must stay fast
    import time
    _app(tmp_path)
    _mk(tmp_path, "pages/Home/widgets/Braces.json", {
        "widgetName": "Braces", "type": "TEXT_WIDGET", "text": "x" + "{{" * 200_000})
    _mk(tmp_path, "pages/Home/queries/Q2/metadata.json", _query("Home", "Q2", "GET", "/a/" + "{{" * 200_000))
    t = time.monotonic()
    nodes, _ = _index(extract_appsmith.extract(tmp_path))
    assert time.monotonic() - t < 10
    assert W + "Braces" in nodes


def test_deterministic(tmp_path):
    _app(tmp_path)
    assert json.dumps(extract_appsmith.extract(tmp_path)) == \
        json.dumps(extract_appsmith.extract(tmp_path))


def test_symlink_out_of_root_is_not_followed(tmp_path):
    outside = tmp_path / "outside"
    _mk(outside, "Leak.json", _widget("Leak", "TEXT_WIDGET", "l1"))
    _mk(outside, "LeakDir/Leak2.json", _widget("Leak2", "TEXT_WIDGET", "l2"))
    root = tmp_path / "repo"
    _app(root)
    try:
        os.symlink(outside / "Leak.json", root / "pages/Home/widgets/Leak.json")
        os.symlink(outside / "LeakDir", root / "pages/Home/widgets/LeakDir",
                   target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    nodes, _ = _index(extract_appsmith.extract(root))
    assert not any("Leak" in n for n in nodes)
    assert W + "Table_Items" in nodes


def test_no_app_no_output(tmp_path):
    _mk(tmp_path, "src/app.json", {"name": "x"})
    assert extract_appsmith.extract(tmp_path) == {"nodes": [], "edges": []}
