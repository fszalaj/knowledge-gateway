"""Ansible pass coverage: playbooks anywhere, import_playbook, template/copy sources,
INI + YAML inventories, group_vars/host_vars and play targets. No tree-sitter needed."""
from __future__ import annotations

import json

import pytest

from gateway.codegraph import extract_ansible

SECRET = "zq-planted-secret-7f3a9c41"


def _write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _rels(frag):
    return {(e["source"], e["relation"], e["target"]) for e in frag["edges"]}


def _ids(frag):
    return {n["id"] for n in frag["nodes"]}


def _repo(root):
    # inventories
    _write(root / "inventory/hosts",
           "ungrouped-box\n"
           "[web]\n"
           f"web-1 ansible_host=192.0.2.10 api_token={SECRET}\n"
           "web-2:2222 ansible_host=192.0.2.11\n"
           "[db]\n"
           "db-1  # primary\n"
           "[app:children]\n"
           "web\n"
           "db\n"
           "[app:vars]\n"
           f"db_password={SECRET}\n")
    _write(root / "inventory/cloud.yml",
           "all:\n"
           "  vars:\n"
           f"    cloud_key: {SECRET}\n"
           "  children:\n"
           "    cache:\n"
           "      hosts:\n"
           "        cache-1:\n"
           f"          ansible_password: {SECRET}\n"
           "        cache-2: {}\n")
    # vars: names only, never the content
    _write(root / "group_vars/web.yml", f"web_secret: {SECRET}\n")
    _write(root / "group_vars/db/main.yml", f"db_pass: {SECRET}\n")
    _write(root / "host_vars/web-1.yaml", f"host_pass: {SECRET}\n")
    # a role with a template and a copied file
    _write(root / "roles/web/tasks/main.yml",
           "- name: render config\n"
           "  ansible.builtin.template:\n"
           "    src: app.conf.j2\n"
           "    dest: /etc/app.conf\n"
           "- name: ship script\n"
           "  copy:\n"
           "    src: run.sh\n"
           "    dest: /usr/local/bin/run.sh\n"
           "- name: ship helper\n"
           "  copy: src=helper.py dest=/opt/helper.py\n"
           "- name: inline content\n"
           "  copy:\n"
           "    dest: /etc/token\n"
           f"    content: \"{SECRET}\"\n"
           "- name: remote copy\n"
           "  copy:\n"
           "    src: /tmp/a\n"
           "    dest: /tmp/b\n"
           "    remote_src: true\n")
    _write(root / "roles/web/templates/app.conf.j2", f"key={SECRET}\n")
    _write(root / "roles/web/files/run.sh", "echo hi\n")
    _write(root / "roles/web/files/helper.py", "def h():\n    return 1\n")
    # a playbook nested in a subdirectory, .yaml extension, with vars and a local template
    _write(root / "playbooks/deploy.yaml",
           "- name: deploy\n"
           "  hosts: web:&app:!db\n"
           "  vars:\n"
           f"    deploy_token: {SECRET}\n"
           "  roles:\n"
           "    - web\n"
           "  tasks:\n"
           "    - name: local template\n"
           "      template:\n"
           "        src: motd.j2\n"
           "        dest: /etc/motd\n"
           "- hosts: cache-1\n"
           "  tasks: []\n"
           "- hosts: unknown_group\n"
           "  tasks: []\n"
           "- hosts: \"{{ target }}\"\n"
           "  tasks: []\n")
    _write(root / "playbooks/templates/motd.j2", "hello\n")
    # a root playbook that only imports others
    _write(root / "site.yml",
           "- import_playbook: playbooks/deploy.yaml\n"
           "- ansible.builtin.import_playbook: ./playbooks/../other.yml\n")
    _write(root / "other.yml", "- hosts: all\n  tasks: []\n")


def test_playbooks_found_anywhere_and_import_playbook(tmp_path):
    _repo(tmp_path)
    frag = extract_ansible.extract(tmp_path)
    ids, rels = _ids(frag), _rels(frag)
    for pb in ("playbook:site.yml", "playbook:other.yml", "playbook:playbooks/deploy.yaml"):
        assert pb in ids
    assert ("playbook:site.yml", "imports_playbook", "playbook:playbooks/deploy.yaml") in rels
    assert ("playbook:site.yml", "imports_playbook", "playbook:other.yml") in rels
    assert ("playbook:playbooks/deploy.yaml", "uses_role", "role:web") in rels


def test_playbook_rules_exclude_role_internals_vars_and_ci(tmp_path):
    play = "- hosts: all\n  tasks: []\n"
    _write(tmp_path / "roles/web/tasks/main.yml", "- name: t\n  debug: msg=x\n")
    _write(tmp_path / "roles/web/tasks/looks_like_play.yml", play)
    _write(tmp_path / "roles/web/defaults/main.yml", play)
    _write(tmp_path / "roles/web/files/play.yml", play)
    _write(tmp_path / "group_vars/all.yml", play)
    _write(tmp_path / "host_vars/h.yml", play)
    _write(tmp_path / ".github/workflows/ci.yml", play)
    _write(tmp_path / "tasklist.yml", "- name: only a task\n  debug: msg=x\n")
    _write(tmp_path / "not_a_list.yml", "hosts: all\n")
    _write(tmp_path / "real.yml", play)
    for keep in (frozenset(), frozenset({".github"})):
        pbs = {i for i in _ids(extract_ansible.extract(tmp_path, include=keep))
               if i.startswith("playbook:")}
        assert pbs == {"playbook:real.yml"}


def test_template_and_copy_resolve_like_ansible(tmp_path):
    _repo(tmp_path)
    frag = extract_ansible.extract(tmp_path)
    ids, rels = _ids(frag), _rels(frag)
    by_rel = {}
    for s, r, t in rels:
        by_rel.setdefault(r, set()).add((s, t))
    rendered = {t for _, t in by_rel.get("renders_template", set())}
    copied = {t for _, t in by_rel.get("copies_file", set())}
    assert rendered == {"template:roles/web/templates/app.conf.j2",
                        "template:playbooks/templates/motd.j2"}
    # a script another pass graphs is pointed at by that pass's module id
    assert "module:roles/web/files/helper.py" in copied
    assert "module:roles/web/files/run.sh" in copied
    assert len(copied) == 2          # inline content and remote_src copies have no local source
    # the task is a node, owned by its tasks file / playbook
    task_ids = {s for s, _ in by_rel["renders_template"]}
    assert task_ids <= ids
    assert any(s == "tasksfile:roles/web/tasks/main.yml" and r == "has_task" and t in task_ids
               for s, r, t in rels)
    assert any(s == "playbook:playbooks/deploy.yaml" and r == "has_task" and t in task_ids
               for s, r, t in rels)
    assert all(t in ids for t in rendered | copied)


def test_inventories_groups_hosts_children(tmp_path):
    _repo(tmp_path)
    frag = extract_ansible.extract(tmp_path)
    ids, rels = _ids(frag), _rels(frag)
    assert {"invgroup:web", "invgroup:db", "invgroup:app", "invgroup:cache", "invgroup:all"} <= ids
    assert {"invhost:web-1", "invhost:web-2", "invhost:db-1", "invhost:ungrouped-box",
            "invhost:cache-1", "invhost:cache-2"} <= ids
    assert ("invhost:web-1", "member_of", "invgroup:web") in rels
    assert ("invhost:web-2", "member_of", "invgroup:web") in rels     # :port stripped
    assert ("invhost:db-1", "member_of", "invgroup:db") in rels
    assert ("invgroup:web", "child_of", "invgroup:app") in rels
    assert ("invgroup:db", "child_of", "invgroup:app") in rels
    assert ("invgroup:cache", "child_of", "invgroup:all") in rels
    assert ("invhost:cache-1", "member_of", "invgroup:cache") in rels
    # `[app:vars]` is not a group of its own and its keys are not hosts
    assert "invgroup:app:vars" not in ids and "invhost:db_password" not in ids
    assert not any(i.startswith("invhost:") and "=" in i for i in ids)


def test_vars_dirs_configure_groups_and_hosts(tmp_path):
    _repo(tmp_path)
    rels = _rels(extract_ansible.extract(tmp_path))
    assert ("vars:group_vars/web.yml", "configures", "invgroup:web") in rels
    assert ("vars:group_vars/db", "configures", "invgroup:db") in rels
    assert ("vars:host_vars/web-1.yaml", "configures", "invhost:web-1") in rels


def test_play_targets(tmp_path):
    _repo(tmp_path)
    frag = extract_ansible.extract(tmp_path)
    targets = {(e["target"], e["confidence"]) for e in frag["edges"]
               if e["relation"] == "targets" and e["source"] == "playbook:playbooks/deploy.yaml"}
    assert ("invgroup:web", "EXTRACTED") in targets
    assert ("invgroup:app", "EXTRACTED") in targets
    assert ("invhost:cache-1", "EXTRACTED") in targets
    assert ("invgroup:unknown_group", "INFERRED") in targets
    assert not any(t == "invgroup:db" for t, _ in targets)          # `!db` excludes, not targets
    assert not any("{{" in t for t, _ in targets)
    assert ("playbook:other.yml", "targets", "invgroup:all") in _rels(frag)


def test_play_level_handlers_are_typed_notify_targets(tmp_path):
    _write(tmp_path / "site.yml",
           "- hosts: web\n  tasks:\n    - name: write config\n      ping:\n"
           "      notify: restart web\n"
           "  handlers:\n    - name: restart web\n      ping:\n")
    frag = extract_ansible.extract(tmp_path)
    nodes = {n["id"]: n for n in frag["nodes"]}
    assert any(r == "notifies" and t == "handler:restart web" for _, r, t in _rels(frag))
    h = nodes["handler:restart web"]
    assert (h["type"], h["source_file"]) == ("handler", "site.yml")


def test_config_ini_is_not_an_inventory(tmp_path):
    _write(tmp_path / "tox.ini", "[tox]\nenvlist = py311\n[testenv]\ndeps =\n    pytest\n")
    _write(tmp_path / "setup.ini", "[metadata]\nname = app\n")
    _write(tmp_path / "hosts", "127.0.0.1 localhost\n")            # /etc/hosts shape, no sections
    ids = _ids(extract_ansible.extract(tmp_path))
    assert not any(i.startswith(("invgroup:", "invhost:")) for i in ids)


def test_non_inventory_yaml_mapping_is_ignored(tmp_path):
    _write(tmp_path / "values.yml", "ingress:\n  hosts:\n    - example.com\n  enabled: true\n")
    ids = _ids(extract_ansible.extract(tmp_path))
    assert not any(i.startswith(("invgroup:", "invhost:")) for i in ids)


def test_no_values_reach_the_graph(tmp_path):
    _repo(tmp_path)
    _write(tmp_path / "vaulted.yml",
           "- hosts: web\n  vars:\n    pw: !vault |\n      $ANSIBLE_VAULT;1.1;AES256\n"
           f"      {SECRET}\n  tasks: []\n")
    frag = extract_ansible.extract(tmp_path)
    blob = json.dumps(frag)
    assert SECRET not in blob
    assert "192.0.2.10" not in blob
    assert "playbook:vaulted.yml" in _ids(frag)       # a !vault tag does not hide the playbook


def test_malformed_and_escaping_files_are_skipped(tmp_path):
    _write(tmp_path / "broken.yml", "- hosts: [unclosed\n")
    _write(tmp_path / "ok.yml", "- hosts: all\n  tasks: []\n")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    _write(outside / "evil.yml", "- hosts: all\n  tasks: []\n")
    _write(outside / "secret.j2", SECRET)
    try:
        (tmp_path / "linked.yml").symlink_to(outside / "evil.yml")
        _write(tmp_path / "pb/templates/.keep", "")
        (tmp_path / "pb/templates/escape.j2").symlink_to(outside / "secret.j2")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    _write(tmp_path / "pb/play.yml",
           "- hosts: all\n  tasks:\n    - template:\n        src: escape.j2\n        dest: /x\n"
           "    - template:\n        src: ../../../etc/passwd\n        dest: /y\n")
    frag = extract_ansible.extract(tmp_path)
    ids = _ids(frag)
    assert "playbook:ok.yml" in ids and "playbook:pb/play.yml" in ids
    assert "playbook:broken.yml" not in ids
    assert "playbook:linked.yml" not in ids
    assert not any(e["relation"] == "renders_template" for e in frag["edges"])


def test_output_is_deterministic(tmp_path):
    _repo(tmp_path)
    a = json.dumps(extract_ansible.extract(tmp_path), sort_keys=True)
    b = json.dumps(extract_ansible.extract(tmp_path), sort_keys=True)
    assert a == b


def test_oversized_file_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_ansible, "MAX_BYTES", 64)
    _write(tmp_path / "big.yml", "- hosts: all\n  tasks: []\n" + "# pad\n" * 40)
    _write(tmp_path / "small.yml", "- hosts: all\n")
    ids = _ids(extract_ansible.extract(tmp_path))
    assert "playbook:small.yml" in ids and "playbook:big.yml" not in ids


def test_existing_root_playbook_ids_stay_stable(tmp_path):
    _write(tmp_path / "playbook_site.yml", "- hosts: all\n  roles:\n    - web\n")
    _write(tmp_path / "roles/web/tasks/main.yml", "- name: t\n  debug: msg=x\n")
    frag = extract_ansible.extract(tmp_path)
    assert ("playbook:playbook_site.yml", "uses_role", "role:web") in _rels(frag)
    # one walk, one node: the old double glob processed root playbooks twice
    assert sum(1 for n in frag["nodes"] if n["id"] == "playbook:playbook_site.yml") == 1
    assert len(frag["edges"]) == len({(e["source"], e["relation"], e["target"])
                                      for e in frag["edges"]})


def test_unknown_extension_falls_back_to_a_file_node(tmp_path, monkeypatch):
    # Without the tree-sitter extension table only .py joins another pass; the rest are files.
    monkeypatch.setattr(extract_ansible, "_TS_EXTS", {})
    _repo(tmp_path)
    copied = {e["target"] for e in extract_ansible.extract(tmp_path)["edges"]
              if e["relation"] == "copies_file"}
    assert "module:roles/web/files/helper.py" in copied           # .py is always the python pass
    assert "file:roles/web/files/run.sh" in copied


def test_copy_of_a_directory_and_a_pruned_path(tmp_path):
    _write(tmp_path / "files/conf.d/a.conf", "x\n")
    _write(tmp_path / "build/gen.py", "x = 1\n")
    _write(tmp_path / "play.yml",
           "- hosts: all\n  tasks:\n"
           "    - copy:\n        src: conf.d/\n        dest: /etc/conf.d/\n"
           "    - copy:\n        src: build/gen.py\n        dest: /opt/gen.py\n")
    copied = {e["target"] for e in extract_ansible.extract(tmp_path)["edges"]
              if e["relation"] == "copies_file"}
    # a pruned directory is never graphed by the python pass, so no module: id there
    assert copied == {"file:files/conf.d", "file:build/gen.py"}


@pytest.mark.parametrize("body", [
    "  tasks:\n    - &t\n      name: x\n      block:\n        - *t\n",          # block contains itself
    "  tasks:\n    - &t\n      name: x\n      debug: {msg: *t}\n",              # task contains itself
    "  tasks: &ts\n    - name: y\n      block: *ts\n",                          # list contains itself
])
def test_recursive_yaml_aliases_are_bounded(tmp_path, body):
    _write(tmp_path / "filter_plugins/f.py",
           "def a(x):\n    return x\nclass FilterModule:\n    def filters(self):\n"
           "        return {'a': a}\n")
    _write(tmp_path / "site.yml", "- hosts: all\n" + body)
    _write(tmp_path / "ok.yml", "- hosts: all\n  tasks: []\n")
    ids = _ids(extract_ansible.extract(tmp_path))          # must not raise RecursionError
    assert {"playbook:site.yml", "playbook:ok.yml"} <= ids


def test_alias_chain_costs_bounded_time(tmp_path):
    # each level repeats the previous one nine times: 9**8 strings from about 600 bytes
    import time
    _write(tmp_path / "filter_plugins/f.py",
           "def a(x):\n    return x\nclass FilterModule:\n    def filters(self):\n"
           "        return {'a': a}\n")
    lines = ["l0: &l0 [" + ", ".join(["'{{ x | a }}'"] * 9) + "]"]
    lines += [f"l{d}: &l{d} [" + ", ".join([f"*l{d - 1}"] * 9) + "]" for d in range(1, 9)]
    _write(tmp_path / "site.yml", "- hosts: all\n  vars:\n" + "".join(f"    {x}\n" for x in lines)
           + "  tasks:\n    - name: t\n      debug: {msg: *l8}\n"
           + "    - name: u\n      block: [{name: v, debug: {msg: *l8}}]\n")
    t = time.monotonic()
    frag = extract_ansible.extract(tmp_path)
    assert time.monotonic() - t < 10
    assert ("task:site.yml:0.tasks.0:t", "calls_filter", "filter:a") in _rels(frag)


def test_deeply_nested_yaml_is_skipped_without_crashing_the_process(tmp_path):
    # libyaml's CSafeLoader overflows the C stack (SIGSEGV) on deep nesting; the pass must use
    # the pure-Python loader, whose RecursionError skips the file. Run in a subprocess: a
    # segfault would otherwise take the test runner down with it.
    import subprocess
    import sys
    from pathlib import Path
    _write(tmp_path / "deploy/values.yaml", "a: " + "[" * 100_000)
    _write(tmp_path / "ok.yml", "- hosts: all\n  tasks: []\n")
    code = ("import json,sys;from pathlib import Path;from gateway.codegraph import extract_ansible as a;"
            "print(json.dumps(sorted(n['id'] for n in a.extract(Path(sys.argv[1]))['nodes'])))")
    p = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True,
                       cwd=Path(__file__).resolve().parents[1], timeout=120)
    assert p.returncode == 0, p.stderr[-500:]
    assert "playbook:ok.yml" in json.loads(p.stdout)


def test_filter_edges_are_ordered_across_processes(tmp_path):
    # a set of filter names iterates in hash order, which changes with PYTHONHASHSEED
    import os
    import subprocess
    import sys
    from pathlib import Path
    _write(tmp_path / "filter_plugins/f.py",
           "def a(x):\n    return x\nclass FilterModule:\n    def filters(self):\n"
           "        return {'alpha': a, 'beta': a, 'gamma': a, 'delta': a}\n")
    _write(tmp_path / "site.yml", "- hosts: all\n  tasks:\n    - name: t\n"
           "      debug: {msg: '{{ x | alpha | beta | gamma | delta }}'}\n")
    code = ("import json,sys;from pathlib import Path;from gateway.codegraph import extract_ansible as a;"
            "print(json.dumps([e['target'] for e in a.extract(Path(sys.argv[1]))['edges'] "
            "if e['relation'] == 'calls_filter']))")
    outs = {subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True,
                           text=True, check=True, cwd=Path(__file__).resolve().parents[1],
                           env={**os.environ, "PYTHONHASHSEED": s}).stdout for s in ("1", "2", "3", "4")}
    assert outs == {json.dumps(["filter:alpha", "filter:beta", "filter:delta", "filter:gamma"]) + "\n"}


def test_same_position_tasks_in_different_plays_and_sections_stay_apart(tmp_path):
    # every play and section restarts at index 0, so the id carries the play and section
    _write(tmp_path / "templates/b.j2", "x\n")
    _write(tmp_path / "site.yml",
           "- hosts: all\n  tasks:\n    - name: cfg\n      template: {src: b.j2, dest: /b}\n"
           "  handlers:\n    - name: cfg\n      notify: h\n"
           "- hosts: all\n  tasks:\n    - name: cfg\n      notify: h\n")
    frag = extract_ansible.extract(tmp_path)
    rels = _rels(frag)
    assert ("task:site.yml:0.tasks.0:cfg", "renders_template", "template:templates/b.j2") in rels
    assert ("task:site.yml:0.handlers.0:cfg", "notifies", "handler:h") in rels
    assert ("task:site.yml:1.tasks.0:cfg", "notifies", "handler:h") in rels
    assert ("task:site.yml:0.tasks.0:cfg", "notifies", "handler:h") not in rels
    assert next(n for n in frag["nodes"] if n["id"] == "task:site.yml:1.tasks.0:cfg")[
        "source_location"] == "#1.tasks.0"
