# Code graph

The code graph is a static map of a repository: what it defines, what it imports and calls, and
how its infrastructure, CI and app configuration point at each other. It is built from syntax
only. Nothing is executed, evaluated, rendered or fetched, and no LLM is involved.

This page is the maintainer reference. The README has the short version.

## Install profiles

| Extra | What you get |
|---|---|
| `[graph]` | Recommended. The graph tools, Python via the stdlib `ast`, about 30 tree-sitter grammars (JS/TS/TSX, Go, Rust, Java, C#, C/C++, Ruby, PHP, bash, PowerShell, Terraform/HCL, Kotlin, Swift, Scala, SQL and more), and every dedicated pass below. |
| `[graph-slim]` | Python and the dedicated passes only (stdlib and PyYAML, no parser pack). Other languages get no definitions, though a pass still emits the `module:<rel>` node of a script it references. |
| `[graph-all]` | Alias of `[graph]`, kept so older pins resolve. |
| `[convert]` | Document conversion, not part of the graph. Listed here because `[all]` combines it with `[graph]`. |

## Build and query

```bash
knowledge-gateway-graph /path/to/repo -o /path/to/vault/.graph/myrepo.json
```

In a local-mode session the `graph_build` tool does the same and writes `.graph/<name>.json` in
the vault. A shared server does not offer `graph_build`, so it cannot be made to scan host paths.
Query with `graph_query`, `graph_neighbors`, `god_nodes`, `graph_shortest_path` and
`graph_stats`; `list_graphs` shows what is available.

Hidden directories (`.git`, `.venv`, caches) and build or vendor output (`node_modules`, `venv`,
`__pycache__`, `dist`, `build`, `coverage`, `htmlcov`, `target`, `vendor`) are always skipped.
`bin/` and `obj/` are not in that rule: only the `aspnet`, `beanstalk` and `manifests` passes
leave .NET build output out, so the tree-sitter pass still graphs generated C# there. Pass
`--exclude bin obj` to drop every directory with those names from the whole build. `--exclude <name>` skips more directory
names and `--include <name>` keeps one the rule would skip, for example a first-party `vendor/`.
Matching is by directory basename, not by path or glob. The `graph_build` tool takes the same
`exclude` and `include` lists.

Each build writes a `<name>.meta.yaml` sidecar with the source revision, the build time and the
snapshot's SHA-256 (no absolute paths). `list_graphs` reports the revision and build time, and
`graph_stats` adds `snapshot_matches`, which is false when the snapshot changed after the sidecar
was written. A graph is a snapshot; this is how an answer says how old it is.

## Passes

Python and the tree-sitter languages produce definitions, imports and calls per file. The
dedicated passes read the files a grammar cannot turn into structure. They share three rules:

- **Names, never values.** No variable, parameter, environment or secret value, default, URL,
  header or body reaches the graph. Every pass except `fabric` has a test that plants a unique
  secret-looking value everywhere a value can sit and asserts it never appears in the output.
  This rule covers the dedicated passes only: the Python and tree-sitter passes keep import
  specifiers as written (up to 120 characters), so a URL import keeps its host and query string.
- **One id per file.** A pass that references a file another pass owns uses that pass's id
  (`module:<rel>`, `playbook:<rel>`, `dockerfile:<rel>`, `manifest:<rel>`, `project:<rel>`), so a
  CI job, the script it runs and the image it builds form one path rather than three islands.
- **Structure only.** No template evaluation, network, registry lookup or subprocess. A
  malformed file is skipped rather than failing the build, and heuristic edges are marked
  `INFERRED`. The passes also skip files over 2 MiB and never follow a symlink out of the root,
  with one exception: `fabric` reads its JSON and TMDL files directly, with no size cap and no
  symlink containment, so build only Fabric trees you trust.

| Pass | Reads | Emits | Never stores |
|---|---|---|---|
| `ansible` | roles, tasks, handlers, playbooks anywhere, INI/YAML inventories, `group_vars`/`host_vars` | `role:`, `playbook:`, `invgroup:`, `invhost:`, `vars:`; includes, `notify`, filter plugins, `targets`, `renders_template`, `copies_file` | variable names and values, `!vault` payloads, inline host vars, dynamic inventory |
| `fabric` | `.platform`, pipeline JSON, TMDL, `definition.pbir`, notebook content | `fabric:<Type>:<name>`, activities with `depends_on`/`invokes`, tables and measures, `reads_model`, `implemented_by` to the notebook's `module:` | notebook-to-table lineage (would need Spark heuristics) |
| `appsmith` | git-synced export (`metadata.json` marker): pages, widgets, queries, datasources, JS objects | `appsmith*:` nodes, `navigates_to`, `uses_datasource`, `triggers`, `reads`, query `route_shape` | datasource config and credentials, headers, bodies, query strings, widget text, JS code |
| `bicep` | `*.bicep`, `*.bicepparam`, `az deployment`/`New-Az*Deployment` in scripts | params, outputs, resources, modules, `uses_module`, `depends_on`, `sets_param`, `reads_secret` by name, `deploys` | defaults, assignments, property values, descriptions, tags |
| `docker` | Dockerfiles, compose files, `docker`/`podman build/push` in scripts | `dockerfile:`, stages, `based_on`, `copies`, `runs`, compose services, `depends_on`, `mounts`, `builds`, `pushes` | `ENV`/`ARG`/environment values, `env_file` contents, labels, `RUN` text |
| `ghactions` | `.github/workflows/*.yml` (read although `.github` is pruned; `--exclude .github` turns it off) | workflows, jobs, `needs`, `uses_action`, `calls_workflow`, `uses_secret`/`uses_var` by name, `runs_script` | `env:`, `with:`, matrix and input values; no expression is evaluated |
| `k8s` | charts, templates (scanned, never rendered), values files, manifests, kustomizations, `helm install/upgrade` in scripts | `helmchart:`, `helmres:`, `helmvalues:`, `helmrelease:`, `k8sres:`, `selects`, `routes_to`, `uses_secret`/`uses_configmap`, role bindings | Secret/ConfigMap data, env values, `--set`, values below the top-level key |
| `manifests` | `pyproject.toml`, `requirements*.txt`, `package.json`, Galaxy requirements, `*.csproj`, `*.sln(x)` | `manifest:`, `project:`, `solution:`, `depends_on` to `extpackage:<eco>:<name>`, `references_project`, Galaxy `provides` a role | index and git URLs, MSBuild property values, lock files |
| `jenkins` | `Jenkinsfile` and Groovy files holding `pipeline {}`/`node {}` | pipelines, stages and their order, `uses_credential` by id, `uses_image`, `builds`, `tests`, `deploys` to Helm releases or Beanstalk environments | parameter and environment values, credential secrets, shell text, URLs |
| `beanstalk` | `.ebextensions`, `.elasticbeanstalk/config.yml`, deploy defaults, `.platform` hooks, `Procfile`, `Dockerrun.aws.json` | bundles, option names, resources, `beanstalkapp:`, `beanstalkenv:`, hooks, processes, containers | option values, command text, file contents, Dockerrun auth |
| `http` | `*.http`, `*.rest` request files | `httpfile:`, `httprequest:` with method, `route_shape`, header and variable names, `uses_response`, `body_from` | file-variable, header and body values, query strings, environment files |
| `aspnet` | `*.cs` controllers and minimal APIs, the enclosing `*.csproj` | `endpoint:<METHOD> <route shape>` with `auth`, `handled_by`, `aspnetapp:`, `exposes`, `optional_segments` | policy and role names, constants, `appsettings*.json`, conventional routes, Razor Pages |

A route shape keeps only the path: scheme, host, port, query string and a leading base-URL
binding are dropped, and every binding or non-literal segment becomes `{}`. A file variable that
holds a URL contributes one `{}` per segment, so no part of its value reaches the graph.

## Post-merge links

Some joins need both ends at once, which no single pass sees. They run after every fragment is
merged and before communities are computed, read node attributes only, and add `INFERRED` edges
between existing nodes, never new nodes. There is one today:

- `calls_api` links an `appsmithquery:` or `httprequest:` node to the `endpoint:` with the same
  method (or `ANY`) whose route shape it matches. The `match` attribute says how: `exact`,
  `pattern` (a `{}` stands for one segment) or `suffix` (the client path ends the endpoint route).
  At least one literal segment must match, literals compare case-insensitively, and a client with
  more than four equally good candidates gets no edge.

## Metadata

- `graph.passes` maps each dedicated pass to the number of nodes it emitted: `0` means it ran
  and found nothing, `-1` means it raised, its output was dropped and the build went on.
- `graph.links` maps each post-merge link to the number of node pairs it joined, with the same
  `-1` rule.
- The graph keeps one edge per pair of nodes. When a pair is related in more than one way (a
  stage that copies a script and then runs it), the edge lists every relation in `relations`,
  and `graph_neighbors` returns that list.

## Determinism

The same tree builds the same graph. Directory walks and node lists are sorted, the pass order is
fixed in `gateway/codegraph/build.py` (when two passes emit the same node id, the later pass's
attributes win), and post-merge links return edges in sorted order.

## Known limits

- It is static analysis. Dynamic imports, reflection, templating and runtime configuration are
  invisible, so treat an absent edge as "not seen", not "not there".
- Clients and endpoints join only inside one graph. A UI and the API it calls, built as separate
  graphs, do not link.
- Not modelled: notebook-to-table lineage, Helm or Bicep rendering, remote bases and actions,
  dynamic Ansible inventories, conventional ASP.NET routes, Razor Pages, gRPC, SignalR, Functions.
- Graph files live in the vault's `.graph/` and are path-checked to stay inside it. Query tools
  are read-only; `graph_build` replaces the selected snapshot on purpose.
