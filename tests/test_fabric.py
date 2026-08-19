"""Microsoft Fabric extractor: the edges that make a deployment repo navigable."""
import json

from gateway.codegraph import extract_fabric


def _mk(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _workspace(root):
    _mk(root, "LoadSales.Notebook/.platform", json.dumps(
        {"metadata": {"type": "Notebook", "displayName": "LoadSales"},
         "config": {"logicalId": "11111111-1111-1111-1111-111111111111"}}))
    _mk(root, "LoadSales.Notebook/notebook-content.py", "def load(spark):\n    return spark\n")
    _mk(root, "Sales.DataPipeline/.platform", json.dumps(
        {"metadata": {"type": "DataPipeline", "displayName": "Sales"}}))
    _mk(root, "Sales.DataPipeline/pipeline-content.json", json.dumps({"properties": {"activities": [
        {"name": "Copy raw", "type": "Copy", "typeProperties": {}},
        {"name": "Run notebook", "type": "TridentNotebook",
         "dependsOn": [{"activity": "Copy raw"}],
         "typeProperties": {"notebookId": "11111111-1111-1111-1111-111111111111"}},
        {"name": "For each region", "type": "ForEach",
         "typeProperties": {"activities": [
             {"name": "Refresh model", "type": "RefreshDataflow",
              "typeProperties": {"referenceName": "SalesModel"}}]}},
    ]}}))
    _mk(root, "SalesModel.SemanticModel/.platform", json.dumps(
        {"metadata": {"type": "SemanticModel", "displayName": "SalesModel"}}))
    _mk(root, "SalesModel.SemanticModel/definition/tables/Sales.tmdl",
        "table Sales\n\tmeasure 'Total Sales' = SUM(Sales[Amount])\n"
        "table 'Date Table'\n\tmeasure 'YTD' = TOTALYTD([Total Sales])\n")
    _mk(root, "SalesReport.Report/.platform", json.dumps(
        {"metadata": {"type": "Report", "displayName": "SalesReport"}}))
    _mk(root, "SalesReport.Report/definition.pbir", json.dumps(
        {"datasetReference": {"byPath": {"path": "../SalesModel.SemanticModel"}}}))


def _index(frag):
    return ({n["id"] for n in frag["nodes"]},
            {(e["source"], e["relation"], e["target"]) for e in frag["edges"]})


def test_artifacts_activities_and_references(tmp_path):
    _workspace(tmp_path)
    nodes, edges = _index(extract_fabric.extract(tmp_path))

    assert {"fabric:Notebook:LoadSales", "fabric:DataPipeline:Sales",
            "fabric:SemanticModel:SalesModel", "fabric:Report:SalesReport"} <= nodes

    # An activity resolves its target by logical id, which is how Fabric writes it.
    assert ("fabricactivity:Sales/Run notebook", "invokes", "fabric:Notebook:LoadSales") in edges
    # ...and by display name, which older exports write instead.
    assert ("fabricactivity:Sales/Refresh model", "invokes", "fabric:SemanticModel:SalesModel") in edges
    # Order inside the pipeline.
    assert ("fabricactivity:Sales/Run notebook", "depends_on", "fabricactivity:Sales/Copy raw") in edges
    # The notebook's code is a module the python pass parsed: this edge joins the two graphs.
    assert ("fabric:Notebook:LoadSales", "implemented_by",
            "module:LoadSales.Notebook/notebook-content.py") in edges
    assert ("fabric:Report:SalesReport", "reads_model", "fabric:SemanticModel:SalesModel") in edges


def test_nested_activity_does_not_steal_its_childs_reference(tmp_path):
    # A ForEach contains the activity that invokes something; the container itself invokes
    # nothing. Without this the parent claims every reference below it.
    _workspace(tmp_path)
    _, edges = _index(extract_fabric.extract(tmp_path))
    assert not [e for e in edges if e[0] == "fabricactivity:Sales/For each region" and e[1] == "invokes"]
    assert ("fabric:DataPipeline:Sales", "contains", "fabricactivity:Sales/Refresh model") in edges


def test_measures_belong_to_the_table_above_them(tmp_path):
    _workspace(tmp_path)
    nodes, edges = _index(extract_fabric.extract(tmp_path))
    assert {"fabrictable:SalesModel/Sales", "fabrictable:SalesModel/Date Table"} <= nodes
    assert ("fabrictable:SalesModel/Sales", "has_measure",
            "fabricmeasure:SalesModel/Sales/Total Sales") in edges
    assert ("fabrictable:SalesModel/Date Table", "has_measure",
            "fabricmeasure:SalesModel/Date Table/YTD") in edges


def test_repo_without_fabric_artifacts_is_empty(tmp_path):
    _mk(tmp_path, "src/app.py", "x = 1\n")
    assert extract_fabric.extract(tmp_path) == {"nodes": [], "edges": []}
