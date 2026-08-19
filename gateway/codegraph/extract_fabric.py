"""Microsoft Fabric extractor - the relationships a generic AST tool cannot see.

A Fabric repository is mostly JSON and TMDL, and a generic JSON pass would emit a node per
key: thousands of nodes, no answers. This is the targeted pass instead. It reads the shapes
Fabric actually writes when a workspace is git-integrated:

  <Name>.<Type>/.platform                  artifact identity (type, display name, logical id)
  <Name>.DataPipeline/pipeline-content.json  activities, their order, what they invoke
  <Name>.Notebook/notebook-content.py        code - parsed by the python pass, linked here
  <Name>.SemanticModel/definition/**.tmdl    tables, measures, relationships
  <Name>.Report/definition.pbir              which semantic model the report reads

Emitted nodes: `fabric:<Type>:<name>` per artifact, `fabricactivity:<pipeline>/<activity>`,
`fabrictable:<model>/<table>`, `fabricmeasure:<model>/<table>/<measure>`.
Emitted edges: contains, depends_on (activity order), invokes (activity -> artifact),
reads_model (report -> semantic model), has_table / has_measure, and implemented_by
(artifact -> the `module:` node the python pass already produced for its code file).

AST/structure only: no LLM, no network, no evaluation of anything.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .extract_ansible import skip_dir

# Artifact folder suffixes Fabric writes. The suffix IS the type, so this list is data: it is the
# ItemType enum of the Fabric REST API, which is the `{display name}.{public facing type}` folder
# name's second half. Kept alphabetical so it can be diffed against that list - a type missing
# here is silent, the artifact simply never appears.
TYPES = (
    "AnomalyDetector", "ApacheAirflowJob", "AppBackend", "AzureDatabricksStorage", "CopyJob",
    "CosmosDBDatabase", "Dashboard", "DataAgent", "DataBuildToolJob", "DataPipeline",
    "Dataflow", "Datamart", "DigitalTwinBuilder", "DigitalTwinBuilderFlow", "Environment",
    "EventSchemaSet", "Eventhouse", "Eventstream", "GraphModel", "GraphQLApi", "GraphQuerySet",
    "KQLDashboard", "KQLDatabase", "KQLQueryset", "Lakehouse", "MLExperiment", "MLModel", "Map",
    "MirroredAzureDatabricksCatalog", "MirroredCatalog", "MirroredDatabase",
    "MirroredWarehouse", "MountedDataFactory", "Notebook", "Ontology", "OperationsAgent",
    "OrgApp", "OrgAppAudience", "PaginatedReport", "Plan", "Reflex", "Report", "SQLDatabase",
    "SQLEndpoint", "SemanticModel", "SnowflakeDatabase", "SparkJobDefinition",
    "UserDataFunction", "VariableLibrary", "Warehouse", "WarehouseSnapshot",
)
# A bare suffix is not enough evidence: plenty of repositories hold a `world.Map` or a `rollout.Plan`
# that Fabric never wrote. Every real item directory carries a system file - .platform since v2, the
# two files it replaced before that - so that file is what makes the directory an artifact.
_V2_SYSTEM = ".platform"
_V1_METADATA = "item.metadata.json"
_V1_CONFIG = "item.config.json"
# Control-flow activities hold their children under a per-branch key, not a single one. The key
# names the branch, so this is data too: If writes ifTrue/ifFalse, Switch writes defaultActivities
# plus one plain `activities` per case, ForEach and Until write `activities`. A key missing here
# is invisible twice over - the children are never emitted, and the parent inherits their
# references, so the graph shows the container invoking what its branch actually invokes.
_ACTIVITY_CONTAINERS = ("activities", "ifTrueActivities", "ifFalseActivities", "defaultActivities")
# Activity type -> the reference field naming what it runs. Fabric writes the id most of the
# time and the name in older exports, so both are accepted and resolved the same way.
_INVOKE_KEYS = ("notebookId", "notebook", "pipelineId", "pipeline", "dataflowId", "dataflowName",
                "workspaceId", "artifactId", "referenceName")
_CODE_FILES = ("notebook-content.py", "notebook-content.ipynb", "SparkJobDefinitionV1.json")
_TMDL_TABLE = re.compile(r"^\s*table\s+('([^']+)'|(\S+))", re.M)
_TMDL_MEASURE = re.compile(r"^\s*measure\s+('([^']+)'|(\S+))", re.M)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def _identity(d: Path):
    """Display name and logical id, from whichever system-file version wrote this directory."""
    meta = _read_json(d / _V2_SYSTEM)
    if meta is None:
        meta = {"metadata": _read_json(d / _V1_METADATA) or {},
                "config": _read_json(d / _V1_CONFIG) or {}}
    display = (meta.get("metadata") or {}).get("displayName") or d.name.rsplit(".", 1)[0]
    return display, (meta.get("config") or {}).get("logicalId")


def _artifact_dirs(root: Path, excl: frozenset[str], keep: frozenset[str]):
    """Every directory whose name ends in a known Fabric type suffix."""
    for dirpath, dirnames, _ in root.walk() if hasattr(root, "walk") else _walk(root):
        dirnames[:] = [d for d in dirnames if not skip_dir(d, excl, keep)]
        for d in list(dirnames):
            name = d
            if "." in name and name.rsplit(".", 1)[1] in TYPES:
                p = Path(dirpath) / d
                if (p / _V2_SYSTEM).exists() or (p / _V1_METADATA).exists():
                    yield p


def _walk(root: Path):
    import os
    for dirpath, dirnames, filenames in os.walk(root):
        yield Path(dirpath), dirnames, filenames


def _activities(obj):
    """Fabric nests activities under properties.activities, and again inside every control-flow
    activity - each under its own branch key, see _ACTIVITY_CONTAINERS."""
    if isinstance(obj, dict):
        nested = [v for k, v in obj.items() if k in _ACTIVITY_CONTAINERS and isinstance(v, list)]
        for acts in nested:
            for a in acts:
                if isinstance(a, dict):
                    yield a
                    yield from _activities(a)
        for v in obj.values():
            if isinstance(v, (dict, list)) and not any(v is n for n in nested):
                yield from _activities(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _activities(v)


def _refs(act: dict):
    """Every id/name this activity points at, wherever Fabric happened to put it."""
    found = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                # Nested activities are activities in their own right and were already emitted
                # with their own references; without this a container claims to invoke whatever
                # its children invoke. Every branch key counts, not just the plain one.
                if k in _ACTIVITY_CONTAINERS:
                    continue
                if k in _INVOKE_KEYS and isinstance(v, str) and v:
                    found.append(v)
                elif isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(act.get("typeProperties", {}))
    walk({k: v for k, v in act.items() if k not in ("typeProperties", "dependsOn")})
    return found


def extract(root: Path, exclude: frozenset[str] = frozenset(),
            include: frozenset[str] = frozenset()) -> dict:
    root = Path(root)
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    by_ref: dict[str, str] = {}     # logical id AND display name -> node id

    def add(nid, **attrs):
        nodes.setdefault(nid, {"id": nid, **attrs})
        return nid

    def edge(src, dst, rel, confidence="EXTRACTED"):
        edges.append({"source": src, "target": dst, "relation": rel, "confidence": confidence})

    artifacts = sorted(_artifact_dirs(root, exclude, include))

    # Pass 1: identity. Every reference below resolves against this, so it must exist first.
    for d in artifacts:
        rel = d.relative_to(root).as_posix()
        atype = d.name.rsplit(".", 1)[1]
        display, logical = _identity(d)
        nid = add(f"fabric:{atype}:{display}", label=display, type=atype.lower(),
                  file_type="fabric", source_file=rel, logical_id=logical)
        by_ref[display] = nid
        if logical:
            by_ref[logical] = nid

        # The python pass already parsed notebook-content.py into a module: node. Linking to it
        # is what turns two disconnected graphs into one.
        for code in _CODE_FILES:
            f = d / code
            if f.exists():
                edge(nid, f"module:{f.relative_to(root).as_posix()}", "implemented_by")

    # Pass 2: structure and references.
    for d in artifacts:
        rel = d.relative_to(root).as_posix()
        atype = d.name.rsplit(".", 1)[1]
        display, _ = _identity(d)
        nid = f"fabric:{atype}:{display}"
        model = display  # child ids read `fabrictable:<model>/<table>`, not the full node id

        if atype == "DataPipeline":
            content = _read_json(d / "pipeline-content.json") or {}
            for act in _activities(content):
                name = act.get("name")
                if not name:
                    continue
                aid = add(f"fabricactivity:{model}/{name}", label=name,
                          type=(act.get("type") or "activity").lower(), file_type="fabric",
                          source_file=f"{rel}/pipeline-content.json")
                edge(nid, aid, "contains")
                for dep in act.get("dependsOn") or []:
                    prev = dep.get("activity") if isinstance(dep, dict) else None
                    if prev:
                        edge(aid, f"fabricactivity:{model}/{prev}", "depends_on")
                for ref in _refs(act):
                    target = by_ref.get(ref)
                    if target and target != nid:
                        edge(aid, target, "invokes")

        elif atype == "Report":
            pbir = _read_json(d / "definition.pbir") or _read_json(d / "definition" / "report.json") or {}
            ds = pbir.get("datasetReference") or {}
            ref = (ds.get("byPath") or {}).get("path") or (ds.get("byConnection") or {}).get("connectionString")
            if isinstance(ref, str):
                # byPath is relative, like "../Sales.SemanticModel"
                stem = ref.rstrip("/").rsplit("/", 1)[-1]
                target = by_ref.get(stem.rsplit(".", 1)[0]) or by_ref.get(stem)
                if target:
                    edge(nid, target, "reads_model")

        elif atype == "SemanticModel":
            defs = d / "definition"
            for tmdl in sorted(defs.rglob("*.tmdl")) if defs.is_dir() else []:
                text = tmdl.read_text(encoding="utf-8", errors="replace")
                trel = tmdl.relative_to(root).as_posix()
                # A measure belongs to the table declared above it, so slice the file into
                # table blocks first rather than guessing per match.
                tables = list(_TMDL_TABLE.finditer(text))
                for i, m in enumerate(tables):
                    tname = m.group(2) or m.group(3)
                    end = tables[i + 1].start() if i + 1 < len(tables) else len(text)
                    tid = add(f"fabrictable:{model}/{tname}", label=tname,
                              type="table", file_type="fabric", source_file=trel)
                    edge(nid, tid, "has_table")
                    for mm in _TMDL_MEASURE.finditer(text[m.end():end]):
                        mname = mm.group(2) or mm.group(3)
                        mid = add(f"fabricmeasure:{model}/{tname}/{mname}", label=mname,
                                  type="measure", file_type="fabric", source_file=trel)
                        edge(tid, mid, "has_measure")

    return {"nodes": list(nodes.values()), "edges": edges}
