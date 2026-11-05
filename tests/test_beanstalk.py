"""Elastic Beanstalk extractor: .ebextensions, EB CLI config, deploy-tool defaults, Dockerrun,
platform hooks, Procfile - names and shapes only, never values."""
import json
import os
import time

import pytest

from gateway.codegraph import extract_beanstalk, extract_docker, extract_manifests

SECRET = "kgBeanstalkCanary-4e8b2c71"
TOKEN = "Zx9" + "q7Lm2" * 6            # a token-shaped literal (long letter+digit run)


def _mk(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _index(frag):
    return ({n["id"]: n for n in frag["nodes"]},
            {(e["source"], e["relation"], e["target"]): e for e in frag["edges"]})


def _app(root):
    """A bundle in web/ with every kind of file this pass reads, all synthetic."""
    _mk(root, "web/Web.csproj", "<Project Sdk=\"Microsoft.NET.Sdk.Web\"></Project>\n")
    _mk(root, "web/manage.py", "print('migrate')\n")
    _mk(root, "web/.ebextensions/scripts/setup.sh", "#!/bin/sh\necho setup\n")
    _mk(root, "web/.ebextensions/scripts/win.ps1", "Write-Output 'x'\n")
    _mk(root, "web/bin/start.sh", "#!/bin/sh\nexec ./server\n")
    _mk(root, "web/.ebextensions/01-app.config", f"""
option_settings:
  aws:elasticbeanstalk:application:environment:
    DB_PASSWORD: {SECRET}
    API_URL: https://user:{SECRET}@api.example.com/v1/x?token={SECRET}
  aws:autoscaling:launchconfiguration:
    IamInstanceProfile: {SECRET}-profile
    EC2KeyName: {SECRET}-key
  aws:elasticbeanstalk:environment:proxy:staticfiles:
    /static: static
    /assets/{TOKEN}: assets
  aws:ec2:vpc:
    VPCId: vpc-{SECRET}
    Subnets: subnet-{SECRET}
packages:
  yum:
    htop: []
    jq: "1.6-{SECRET}"
  rpm:
    epel: https://user:{SECRET}@mirror.example.com/epel.rpm
commands:
  01_setup:
    command: "bash .ebextensions/scripts/setup.sh --token {SECRET}"
    env:
      TOKEN: {SECRET}
    cwd: /tmp
    test: "test -f /etc/{SECRET}"
  02_noop:
    command: "echo {SECRET}"
container_commands:
  10_migrate:
    command: "python manage.py migrate --password={SECRET}"
    leader_only: true
  20_start:
    command: ["/var/app/staging/bin/start.sh", "{SECRET}"]
files:
  "/etc/nginx/conf.d/proxy.conf":
    mode: "000644"
    content: |
      upstream {SECRET} {{ }}
  "/opt/{TOKEN}/key.pem":
    source: https://bucket.example.com/{SECRET}.pem
    authentication: S3Auth
services:
  sysvinit:
    nginx:
      enabled: true
Resources:
  AppQueue:
    Type: AWS::SQS::Queue
    Properties:
      QueueName: !Sub "{SECRET}-queue"
  QueuePolicy:
    Type: AWS::SQS::QueuePolicy
    DependsOn: AppQueue
    Properties:
      Queues: [!Ref AppQueue]
  S3Auth:
    Type: AWS::CloudFormation::Authentication
    Properties:
      secretKey: {SECRET}
Outputs:
  QueueUrl:
    Value: !Ref AppQueue
""")
    _mk(root, "web/.ebextensions/02-win.config", json.dumps({
        "option_settings": [
            {"namespace": "aws:elasticbeanstalk:environment", "option_name": "EnvironmentType",
             "value": SECRET},
            {"option_name": "JSON_KEY", "value": SECRET},
        ],
        "commands": {
            "01_ps": {"command": "powershell.exe -ExecutionPolicy Bypass -File "
                                 ".ebextensions\\scripts\\win.ps1 " + SECRET},
        },
    }))
    _mk(root, "web/.ebextensions/notes.txt", "not a config\n")
    _mk(root, "web/aws-beanstalk-tools-defaults.json", json.dumps({
        "Information": ["generated", SECRET],
        "profile": SECRET,
        "region": "eu-west-1",
        "configuration": "Release",
        "application": "shop",
        "environment": "shop-staging",
        "solution-stack": "64bit Amazon Linux 2023 v3.1.0 running .NET 8",
        "instance-profile": SECRET,
        "service-role": SECRET,
        "key-pair": SECRET,
        "s3-bucket": SECRET,
        "vpc-subnets": SECRET,
        "additional-options": f"--secret {SECRET}",
    }))
    _mk(root, "web/Procfile", f"web: ./bin/start.sh --key {SECRET}\nworker: python manage.py work\n# c\n")


def test_ebextensions_config_names_and_scripts(tmp_path):
    _app(tmp_path)
    nodes, edges = _index(extract_beanstalk.extract(tmp_path))
    bundle = "beanstalkbundle:web"
    cfg = "beanstalkconfig:web/.ebextensions/01-app.config"
    assert nodes[bundle]["type"] == "beanstalk_bundle"
    assert (bundle, "contains", cfg) in edges
    c = nodes[cfg]
    assert c["type"] == "beanstalk_config" and c["file_type"] == "beanstalk"
    assert c["environment_keys"] == ["DB_PASSWORD", "API_URL"]
    assert "aws:autoscaling:launchconfiguration/IamInstanceProfile" in c["options"]
    assert "aws:ec2:vpc/VPCId" in c["options"]
    # a URL-path option name keeps its shape only: token-like segments become {}
    assert "aws:elasticbeanstalk:environment:proxy:staticfiles//static" in c["options"]
    assert "aws:elasticbeanstalk:environment:proxy:staticfiles//assets/{}" in c["options"]
    assert c["packages"] == ["yum:htop", "yum:jq", "rpm:epel"]
    assert c["commands"] == ["01_setup", "02_noop"]
    assert c["container_commands"] == ["10_migrate", "20_start"]
    assert c["files"] == ["/etc/nginx/conf.d/proxy.conf", "/opt/{}/key.pem"]
    assert c["services"] == ["sysvinit:nginx"]

    # commands -> the local scripts they invoke, in the node shape the docker pass writes
    setup = "module:web/.ebextensions/scripts/setup.sh"
    e = edges[(cfg, "runs_script", setup)]
    assert e["confidence"] == "INFERRED" and e["section"] == "commands" and e["command"] == "01_setup"
    assert nodes[setup] == extract_docker.file_node("web/.ebextensions/scripts/setup.sh")
    assert nodes[setup]["file_type"] == "bash"
    assert (cfg, "runs_script", "module:web/manage.py") in edges
    # exec form, absolute path under the staging directory -> the bundle file
    assert edges[(cfg, "runs_script", "module:web/bin/start.sh")]["section"] == "container_commands"

    # Resources: logical ids and types; DependsOn between them
    q = "beanstalkresource:web/.ebextensions/01-app.config#AppQueue"
    pol = "beanstalkresource:web/.ebextensions/01-app.config#QueuePolicy"
    assert nodes[q]["resource_type"] == "AWS::SQS::Queue"
    assert (cfg, "defines_resource", q) in edges and (pol, "depends_on", q) in edges
    assert not any("QueueUrl" in n for n in nodes)          # Outputs are not graphed

    # JSON config, list-form option_settings, Windows command line
    win = "beanstalkconfig:web/.ebextensions/02-win.config"
    assert nodes[win]["options"] == ["aws:elasticbeanstalk:environment/EnvironmentType"]
    assert nodes[win]["environment_keys"] == ["JSON_KEY"]
    assert (win, "runs_script", "module:web/.ebextensions/scripts/win.ps1") in edges
    assert not any("notes.txt" in n for n in nodes)


def test_defaults_file_environment_and_project(tmp_path):
    _app(tmp_path)
    nodes, edges = _index(extract_beanstalk.extract(tmp_path))
    d = "awsdeploydefaults:web/aws-beanstalk-tools-defaults.json"
    env = "beanstalkenv:shop/shop-staging"
    assert nodes[d]["tool"] == "beanstalk"
    assert nodes[d]["region"] == "eu-west-1"
    assert nodes[d]["solution_stack"] == "64bit Amazon Linux 2023 v3.1.0 running .NET 8"
    assert "instance-profile" in nodes[d]["option_keys"]            # the key name, not its value
    assert nodes[env]["label"] == "shop-staging" and nodes[env]["application"] == "shop"
    assert ("beanstalkapp:shop", "has_environment", env) in edges
    assert (d, "targets", env) in edges
    assert edges[("beanstalkbundle:web", "deploys_to", env)]["confidence"] == "INFERRED"
    proj = "project:web/Web.csproj"
    assert (d, "configures", proj) in edges
    assert (proj, "deploys_to", env) in edges
    # the very node the manifests pass writes for the project
    assert nodes[proj] == extract_manifests.node_for("web/Web.csproj")
    assert proj in {n["id"] for n in extract_manifests.extract(tmp_path)["nodes"]}


def test_env_id_without_application_and_other_tools(tmp_path):
    _mk(tmp_path, "svc/aws-beanstalk-tools-defaults.qa.json",
        json.dumps({"environment": "svc-qa", "region": f"eu-{SECRET}-1"}))
    _mk(tmp_path, "fn/Fn.csproj", "<Project/>\n")
    _mk(tmp_path, "fn/aws-lambda-tools-defaults.json",
        json.dumps({"function-name": "f", "function-role": SECRET, "region": "us-east-1"}))
    frag = extract_beanstalk.extract(tmp_path)
    nodes, edges = _index(frag)
    assert "beanstalkenv:-/svc-qa" in nodes
    assert "region" not in nodes["awsdeploydefaults:svc/aws-beanstalk-tools-defaults.qa.json"]
    lam = nodes["awsdeploydefaults:fn/aws-lambda-tools-defaults.json"]
    assert lam["tool"] == "lambda" and lam["region"] == "us-east-1"
    assert ("awsdeploydefaults:fn/aws-lambda-tools-defaults.json", "configures", "project:fn/Fn.csproj") in edges
    assert "beanstalkbundle:fn" not in nodes                       # lambda is not a bundle
    assert SECRET not in json.dumps(frag)


def test_eb_cli_config(tmp_path):
    _mk(tmp_path, "api/.elasticbeanstalk/config.yml", f"""
branch-defaults:
  main:
    environment: api-prod
    group_suffix: null
  develop:
    environment: api-dev
environment-defaults:
  api-prod:
    branch: null
    repository: null
global:
  application_name: api
  default_ec2_keyname: {SECRET}
  default_platform: arn:aws:elasticbeanstalk:eu-west-1::platform/Python 3.11 running on 64bit Amazon Linux 2023/4.0.1
  default_region: eu-west-1
  instance_profile: {SECRET}
  profile: {SECRET}
  sc: git
deploy:
  artifact: build/{SECRET}.zip
""")
    frag = extract_beanstalk.extract(tmp_path)
    nodes, edges = _index(frag)
    cli = "beanstalkcli:api/.elasticbeanstalk/config.yml"
    assert nodes[cli]["application"] == "api"
    assert nodes[cli]["region"] == "eu-west-1"
    assert nodes[cli]["platform"] == "Python 3.11 running on 64bit Amazon Linux 2023/4.0.1"
    prod, dev = "beanstalkenv:api/api-prod", "beanstalkenv:api/api-dev"
    assert edges[(cli, "targets", prod)]["branches"] == ["main"]
    assert edges[(cli, "targets", dev)]["branches"] == ["develop"]
    assert edges[("beanstalkbundle:api", "deploys_to", prod)]["confidence"] == "EXTRACTED"
    assert ("beanstalkapp:api", "has_environment", dev) in edges
    assert SECRET not in json.dumps(frag)


def test_dockerrun_v1_and_v2(tmp_path):
    _mk(tmp_path, "one/Dockerrun.aws.json", json.dumps({
        "AWSEBDockerrunVersion": "1",
        "Image": {"Name": "example.com/team/app:1.4", "Update": "true"},
        "Ports": [{"ContainerPort": 8080, "HostPort": 80}],
        "Authentication": {"Bucket": SECRET, "Key": SECRET},
        "Volumes": [{"HostDirectory": f"/{SECRET}", "ContainerDirectory": "/data"}],
        "Entrypoint": f"/run {SECRET}",
    }))
    _mk(tmp_path, "two/Dockerrun.aws.json", json.dumps({
        "AWSEBDockerrunVersion": 2,
        "authentication": {"bucket": SECRET, "key": SECRET},
        "containerDefinitions": [
            {"name": "web", "image": "nginx:1.25", "essential": True,
             "portMappings": [{"hostPort": 80, "containerPort": "80"}],
             "links": ["api:backend"], "environment": [{"name": "MODE", "value": SECRET}]},
            {"name": "api", "image": f"user:{SECRET}@registry.example.com/api:2"},
            {"name": "bad", "image": f"https://{SECRET}.example.com/x"},
        ],
    }))
    frag = extract_beanstalk.extract(tmp_path)
    nodes, edges = _index(frag)
    one = "dockerrun:one/Dockerrun.aws.json"
    assert nodes[one]["version"] == "1" and nodes[one]["ports"] == [8080]
    img = "extimage:example.com/team/app:1.4"
    assert (one, "uses_image", img) in edges
    assert nodes[img]["type"] == "image" and nodes[img]["file_type"] == "docker"
    assert nodes[img]["tag"] == "1.4"
    two = "dockerrun:two/Dockerrun.aws.json"
    web, api = (f"dockerruncontainer:two/Dockerrun.aws.json#{n}" for n in ("web", "api"))
    assert (two, "has_container", web) in edges and (two, "has_container", api) in edges
    assert nodes[web]["ports"] == [80] and nodes[web]["environment_keys"] == ["MODE"]
    assert nodes[web]["essential"] is True
    assert (web, "uses_image", "extimage:nginx:1.25") in edges
    assert (web, "links", api) in edges
    assert not any(s == api and r == "uses_image" for (s, r, _t) in edges)   # userinfo: dropped
    assert ("beanstalkbundle:two", "contains", two) in edges
    assert SECRET not in json.dumps(frag)


def test_platform_hooks(tmp_path):
    _mk(tmp_path, ".platform/hooks/prebuild/01_install.sh", "#!/bin/sh\n")
    _mk(tmp_path, ".platform/hooks/postdeploy/restart", "#!/bin/sh\n")
    _mk(tmp_path, ".platform/confighooks/predeploy/10_cfg.py", "print(1)\n")
    _mk(tmp_path, ".platform/hooks/prebuild/lib/helper.sh", "#!/bin/sh\n")   # not run directly
    _mk(tmp_path, ".platform/hooks/unknown/x.sh", "#!/bin/sh\n")
    _mk(tmp_path, ".platform/nginx/conf.d/x.conf", "server {}\n")
    nodes, edges = _index(extract_beanstalk.extract(tmp_path))
    b = "beanstalkbundle:."
    e = edges[(b, "runs", "module:.platform/hooks/prebuild/01_install.sh")]
    assert e["hook"] == "hooks" and e["phase"] == "prebuild" and e["confidence"] == "EXTRACTED"
    assert (b, "runs", "file:.platform/hooks/postdeploy/restart") in edges
    assert nodes["file:.platform/hooks/postdeploy/restart"]["type"] == "file"
    assert edges[(b, "runs", "module:.platform/confighooks/predeploy/10_cfg.py")]["hook"] == "confighooks"
    assert nodes["module:.platform/confighooks/predeploy/10_cfg.py"]["file_type"] == "python"
    assert not any("helper.sh" in t or "unknown" in t or "nginx" in t for (_s, _r, t) in edges)


def test_procfile_only_in_a_bundle(tmp_path):
    _mk(tmp_path, "heroku/Procfile", "web: ./run.sh\n")
    _mk(tmp_path, "heroku/run.sh", "#!/bin/sh\n")
    _app(tmp_path)
    nodes, edges = _index(extract_beanstalk.extract(tmp_path))
    assert not any(n.startswith(("beanstalkprocess:heroku", "beanstalkbundle:heroku")) for n in nodes)
    web, worker = "beanstalkprocess:web/Procfile#web", "beanstalkprocess:web/Procfile#worker"
    assert nodes[web]["source_location"] == "L1"
    assert ("beanstalkbundle:web", "has_process", web) in edges
    assert ("beanstalkbundle:web", "has_process", worker) in edges
    assert (web, "runs_script", "module:web/bin/start.sh") in edges
    assert (worker, "runs_script", "module:web/manage.py") in edges


def test_no_value_reaches_the_graph(tmp_path):
    _app(tmp_path)
    _mk(tmp_path, ".elasticbeanstalk/config.yml",
        f"global:\n  application_name: root\n  profile: {SECRET}\n  default_region: {SECRET}\n"
        f"branch-defaults:\n  main:\n    environment: {SECRET}-{TOKEN}\n")
    _mk(tmp_path, "Dockerrun.aws.json", json.dumps({
        "AWSEBDockerrunVersion": 2, "containerDefinitions": [
            {"name": "c", "image": f"app:{SECRET}@sha256:zz", "environment": [{"name": "K", "value": SECRET}],
             "mountPoints": [{"sourceVolume": SECRET}]}]}))
    frag = extract_beanstalk.extract(tmp_path)
    dumped = json.dumps(frag)
    assert SECRET not in dumped
    assert TOKEN not in dumped
    assert "example.com" not in dumped


def test_malformed_input_never_raises(tmp_path):
    _mk(tmp_path, "a/.ebextensions/broken.config", "option_settings: [\n  - : :\n\t bad")
    _mk(tmp_path, "a/.ebextensions/list.config", "- 1\n- 2\n")
    _mk(tmp_path, "a/.ebextensions/types.config",
        "option_settings: 5\npackages: [1]\ncommands: [x]\nfiles: 3\nservices: {yum: 1}\n"
        "Resources: {X: 1, Y: {Type: 5, DependsOn: {a: 1}}}\ncontainer_commands: {1: null}\n")
    _mk(tmp_path, "a/.ebextensions/deep.config", "[" * 50_000 + "]" * 50_000)
    _mk(tmp_path, "a/.ebextensions/empty.config", "")
    _mk(tmp_path, "a/.elasticbeanstalk/config.yml", "branch-defaults: [1]\nglobal: 7\n")
    _mk(tmp_path, "a/Dockerrun.aws.json", '{"containerDefinitions": [1, {"name": 5}, {"name": "x", "links": 3}]')
    _mk(tmp_path, "b/Dockerrun.aws.json", '{"containerDefinitions": {"name": "x"}, "Ports": "80"}')
    _mk(tmp_path, "b/aws-beanstalk-tools-defaults.json", "[1, 2]")
    _mk(tmp_path, "c/aws-beanstalk-tools-defaults.json", '{"environment": ["x"], "project-location": "../../.."}')
    big = tmp_path / "d/.ebextensions/big.config"
    big.parent.mkdir(parents=True)
    big.write_text("commands:\n" + "  c: {command: x}\n" * 200_000, encoding="utf-8")   # > 2 MiB
    (tmp_path / "e/.ebextensions").mkdir(parents=True)
    (tmp_path / "e/.ebextensions/bin.config").write_bytes(b"\xff\xfe\x00option_settings\x00")
    frag = extract_beanstalk.extract(tmp_path)
    nodes, _ = _index(frag)
    assert "beanstalkconfig:a/.ebextensions/types.config" in nodes
    assert not any("big.config" in n for n in nodes)
    json.dumps(frag)


def test_yaml_aliases_are_bounded(tmp_path):
    body = "x: &c {command: 'bash run.sh'}\ncommands:\n" + "".join(f"  c{i}: *c\n" for i in range(30_000))
    _mk(tmp_path, ".ebextensions/alias.config", body)
    _mk(tmp_path, "run.sh", "#!/bin/sh\n")
    frag = extract_beanstalk.extract(tmp_path)
    nodes, edges = _index(frag)
    cfg = nodes["beanstalkconfig:.ebextensions/alias.config"]
    assert len(cfg["commands"]) <= extract_beanstalk._MAX_NAMES
    assert len(frag["edges"]) <= extract_beanstalk._MAX_EDGES * 2


def test_option_maps_repeated_by_aliases_are_read_once(tmp_path):
    # one large option map, re-listed through an alias under the same and another namespace
    anchor = "".join(f"  o{i:04d}: 1\n" for i in range(1999))
    _mk(tmp_path, ".ebextensions/opts.config",
        "x: &a\n" + anchor + "option_settings:\n" + "  - aws:x: *a\n" * 20_000 + "  - aws:y: *a\n")
    t = time.monotonic()
    frag = extract_beanstalk.extract(tmp_path)
    assert time.monotonic() - t < 15
    cfg = {n["id"]: n for n in frag["nodes"]}["beanstalkconfig:.ebextensions/opts.config"]
    assert cfg["namespaces"] == ["aws:x", "aws:y"]
    assert "aws:x/o0000" in cfg["options"] and "aws:y/o0000" in cfg["options"]
    assert len(cfg["options"]) <= extract_beanstalk._MAX_NAMES


def test_dotnet_build_output_is_not_a_bundle(tmp_path):
    # dotnet publish / dotnet eb copy .ebextensions into bin/ and obj/ next to the project
    cfg = "commands: {a: {command: x}}\n"
    _mk(tmp_path, "Svc/Svc.csproj", "<Project Sdk=\"Microsoft.NET.Sdk.Web\"></Project>\n")
    _mk(tmp_path, "Svc/.ebextensions/a.config", cfg)
    _mk(tmp_path, "Svc/bin/Release/net8.0/publish/.ebextensions/a.config", cfg)
    _mk(tmp_path, "Svc/obj/Release/net8.0/PubTmp/Out/.ebextensions/a.config", cfg)
    _mk(tmp_path, "tool/bin/.ebextensions/a.config", cfg)           # no project beside: kept
    bundles = sorted(n["id"] for n in extract_beanstalk.extract(tmp_path)["nodes"]
                     if n["id"].startswith("beanstalkbundle:"))
    assert bundles == ["beanstalkbundle:Svc", "beanstalkbundle:tool/bin"]
    kept = extract_beanstalk.extract(tmp_path, include=frozenset({"bin"}))
    assert "beanstalkbundle:Svc/bin/Release/net8.0/publish" in {n["id"] for n in kept["nodes"]}


def test_prune_and_exclude(tmp_path):
    _mk(tmp_path, "node_modules/pkg/.ebextensions/x.config", "commands: {a: {command: x}}\n")
    _mk(tmp_path, ".hidden/.ebextensions/x.config", "commands: {a: {command: x}}\n")
    _mk(tmp_path, "keep/.ebextensions/x.config", "commands: {a: {command: x}}\n")
    _mk(tmp_path, "skipme/.ebextensions/x.config", "commands: {a: {command: x}}\n")
    nodes, _ = _index(extract_beanstalk.extract(tmp_path, frozenset({"skipme"})))
    assert set(n for n in nodes if n.startswith("beanstalkconfig:")) == {"beanstalkconfig:keep/.ebextensions/x.config"}
    nodes, _ = _index(extract_beanstalk.extract(tmp_path, frozenset({".ebextensions"})))
    assert not any(n.startswith("beanstalkconfig:") for n in nodes)


def test_symlink_out_of_root_is_not_read(tmp_path):
    outside = tmp_path / "outside"
    _mk(outside, "evil.config", f"commands:\n  {SECRET}x: {{command: x}}\n")
    _mk(outside, "aws-beanstalk-tools-defaults.json", json.dumps({"environment": "leak-env"}))
    _mk(outside, "secret.sh", "#!/bin/sh\n")
    repo = tmp_path / "repo"
    _mk(repo, ".ebextensions/ok.config", "container_commands:\n  a: {command: 'bash link.sh'}\n")
    try:
        os.symlink(outside / "evil.config", repo / ".ebextensions/evil.config")
        os.symlink(outside / "aws-beanstalk-tools-defaults.json", repo / "aws-beanstalk-tools-defaults.json")
        os.symlink(outside / "secret.sh", repo / "link.sh")
        os.symlink(outside, repo / ".platform", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    frag = extract_beanstalk.extract(repo)
    dumped = json.dumps(frag)
    assert SECRET not in dumped and "leak-env" not in dumped and "link.sh" not in dumped
    assert "beanstalkconfig:.ebextensions/ok.config" in {n["id"] for n in frag["nodes"]}


def test_output_is_deterministic(tmp_path):
    _app(tmp_path)
    _mk(tmp_path, ".platform/hooks/prebuild/a.sh", "#!/bin/sh\n")
    a = json.dumps(extract_beanstalk.extract(tmp_path), sort_keys=True)
    b = json.dumps(extract_beanstalk.extract(tmp_path), sort_keys=True)
    assert a == b


def test_every_edge_is_well_formed(tmp_path):
    _app(tmp_path)
    frag = extract_beanstalk.extract(tmp_path)
    ids = {n["id"] for n in frag["nodes"]}
    for n in frag["nodes"]:
        assert {"id", "label", "type", "file_type", "source_file"} <= set(n)
    for e in frag["edges"]:
        assert e["source"] in ids and e["target"] in ids
        assert e["confidence"] in ("EXTRACTED", "INFERRED")
