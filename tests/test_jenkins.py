"""Jenkins extractor: stages and their order, credentials by id, and what the steps run, build,
push and deploy - names only, never values."""
from __future__ import annotations

import json
import os
import time

import pytest

from gateway.codegraph import extract_docker, extract_jenkins

SECRET = "zq-jenkins-canary-7d41e0b9"
P = "jenkinspipeline:Jenkinsfile"
S = "jenkinsstage:Jenkinsfile#"


def _mk(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _index(frag):
    return ({n["id"]: n for n in frag["nodes"]},
            {(e["source"], e["relation"], e["target"]): e for e in frag["edges"]})


DECLARATIVE = """\
// a leading comment: pipeline { } here is not code
@Library('shared-lib') _
pipeline {
    agent {
        docker {
            image 'example.org/build/toolchain:1.4'
            registryCredentialsId 'registry-pull'
        }
    }
    environment {
        APP_MODE = 'production'
        DEPLOY_KEY = credentials('deploy-key')
    }
    parameters {
        string(name: 'TARGET', defaultValue: 'staging', description: 'where to')
        booleanParam(name: 'DRY_RUN', defaultValue: false)
    }
    triggers {
        cron('H 4 * * 1-5')
        pollSCM('H/15 * * * *')
    }
    tools {
        maven 'maven-3'
        jdk 'jdk17'
    }
    stages {
        stage('Build') {
            steps {
                sh './scripts/build.sh --fast'
                sh(script: "python3 tools/gen.py", returnStdout: true)
            }
        }
        stage('Test') {
            parallel {
                stage('Unit') {
                    steps { sh 'bash scripts/unit.sh' }
                }
                stage('Lint') {
                    agent { dockerfile { filename 'Dockerfile.lint'; dir 'ci' } }
                    steps { sh "make lint" }
                }
            }
        }
        stage('Publish') {
            stages {
                stage('Image') {
                    steps {
                        withCredentials([usernamePassword(credentialsId: 'registry-push',
                                         usernameVariable: 'REG_USER', passwordVariable: 'REG_PASS')]) {
                            sh '''
                                docker build -f docker/app.Dockerfile -t example.org/team/app:2.0 .
                                docker push example.org/team/app:2.0
                            '''
                        }
                    }
                }
                stage('Chart') {
                    steps {
                        sh "helm upgrade --install web ./charts/web -n apps --set image.tag=2.0"
                    }
                }
            }
        }
    }
    post {
        always { sh './scripts/cleanup.sh' }
        failure { echo 'failed' }
    }
}
"""


def _declarative(root):
    _mk(root, "Jenkinsfile", DECLARATIVE)
    for f in ("scripts/build.sh", "scripts/unit.sh", "scripts/cleanup.sh"):
        _mk(root, f, "#!/bin/sh\necho ok\n")
    _mk(root, "tools/gen.py", "print(1)\n")
    _mk(root, "ci/Dockerfile.lint", "FROM alpine:3.20\n")
    _mk(root, "docker/app.Dockerfile", "FROM alpine:3.20\n")
    _mk(root, "charts/web/Chart.yaml", "apiVersion: v2\nname: web\nversion: 0.1.0\n")


def test_declarative_pipeline_structure(tmp_path):
    _declarative(tmp_path)
    nodes, edges = _index(extract_jenkins.extract(tmp_path))
    p = nodes[P]
    assert (p["type"], p["file_type"], p["style"]) == ("jenkins_pipeline", "jenkins", "declarative")
    assert p["environment"] == ["APP_MODE", "DEPLOY_KEY"]
    assert p["parameters"] == ["TARGET", "DRY_RUN"]
    assert p["triggers"] == ["cron", "pollSCM"]
    assert p["tools"] == ["maven:maven-3", "jdk:jdk17"]
    assert p["post"] == ["always", "failure"]
    assert p["agent"] == "docker"
    for s in ("Build", "Test", "Test/Unit", "Test/Lint", "Publish", "Publish/Image", "Publish/Chart"):
        assert nodes[S + s]["type"] == "jenkins_stage", s
    assert nodes[S + "Build"]["source_location"].startswith("L")
    assert nodes[S + "Test/Unit"]["parallel"] is True and "parallel" not in nodes[S + "Build"]
    # nesting and order
    assert (P, "contains", S + "Build") in edges
    assert (S + "Test", "contains", S + "Test/Unit") in edges
    assert (S + "Publish", "contains", S + "Publish/Chart") in edges
    assert edges[(S + "Test", "depends_on", S + "Build")]["confidence"] == "EXTRACTED"
    assert (S + "Publish", "depends_on", S + "Test") in edges
    assert (S + "Publish/Chart", "depends_on", S + "Publish/Image") in edges
    assert not any(r == "depends_on" and {s, t} == {S + "Test/Unit", S + "Test/Lint"}
                   for s, r, t in edges)
    assert not any(r == "depends_on" and s == S + "Publish/Image" for s, r, t in edges)


def test_declarative_edges_join_other_passes(tmp_path):
    _declarative(tmp_path)
    nodes, edges = _index(extract_jenkins.extract(tmp_path))
    # agent image and registry credential at pipeline level
    assert (P, "uses_image", "extimage:example.org/build/toolchain:1.4") in edges
    assert (P, "uses_credential", "jenkinscredential:registry-pull") in edges
    e = edges[(P, "uses_credential", "jenkinscredential:deploy-key")]
    assert (e["via"], e["variable"]) == ("credentials", "DEPLOY_KEY")
    assert nodes["jenkinscredential:deploy-key"] == {
        "id": "jenkinscredential:deploy-key", "label": "deploy-key", "type": "jenkins_credential",
        "file_type": "jenkins", "source_file": "Jenkinsfile"}
    # scripts: the module:<rel> id the tree-sitter / python passes use
    rs = edges[(S + "Build", "runs_script", "module:scripts/build.sh")]
    assert (rs["confidence"], rs["step"]) == ("INFERRED", "sh")
    assert nodes["module:scripts/build.sh"]["file_type"] == "bash"
    assert (S + "Build", "runs_script", "module:tools/gen.py") in edges
    assert (S + "Test/Unit", "runs_script", "module:scripts/unit.sh") in edges
    assert edges[(P, "runs_script", "module:scripts/cleanup.sh")]["post"] == "always"
    # stage agent dockerfile, a docker build and push inside withCredentials
    assert edges[(S + "Test/Lint", "builds", "dockerfile:ci/Dockerfile.lint")]["agent"] is True
    img = S + "Publish/Image"
    assert edges[(img, "uses_credential", "jenkinscredential:registry-push")]["via"] == \
        "usernamePassword"
    assert (img, "builds", "dockerfile:docker/app.Dockerfile") in edges
    assert (img, "pushes", "extimage:example.org/team/app:2.0") in edges
    # helm: the k8s pass's release id, installing the local chart
    rel = "helmrelease:apps/web"
    assert (S + "Publish/Chart", "deploys", rel) in edges
    assert (nodes[rel]["type"], nodes[rel]["namespace"]) == ("helm_release", "apps")
    assert (rel, "installs", "helmchart:charts/web") in edges
    # the same ids the docker pass writes for those files and images
    dnodes, _ = _index(extract_docker.extract(tmp_path))
    for nid in ("dockerfile:docker/app.Dockerfile", "dockerfile:ci/Dockerfile.lint"):
        assert {k: nodes[nid][k] for k in ("type", "file_type", "source_file")} == \
            {k: dnodes[nid][k] for k in ("type", "file_type", "source_file")}
    # edges only reach nodes that carry a type, except the chart the k8s pass owns
    ids = {n["id"] for n in extract_jenkins.extract(tmp_path)["nodes"]}
    assert {t for _, _, t in edges} - ids == {"helmchart:charts/web"}


SCRIPTED = """\
properties([parameters([string(name: 'CHANNEL', defaultValue: 'chat-room')])])

timestamps {
  node('windows') {
    stage('Prepare')
    {
      checkout scm: [$class: 'GitSCM', userRemoteConfigs: [[url: 'git@git.example.com:team/app.git', credentialsId: 'git-ssh']]]
      bat 'call build\\\\prepare.bat'
    }
    stage('Release') {
      powershell '& .\\\\deploy\\\\notify.ps1 -Channel $env:CHANNEL'
      dir('App') {
        bat script: 'dotnet eb deploy-environment -env "orders-api-prod" -app "orders-api" --configuration Release', label: 'eb'
      }
    }
    stage('Verify') {
      bat 'dotnet test App.Tests/App.Tests.csproj --no-build --logger trx'
    }
  }
}
"""


def _scripted(root):
    _mk(root, "ci/release-jenkins.pipeline.txt", SCRIPTED)
    _mk(root, "App/App.csproj", "<Project Sdk=\"Microsoft.NET.Sdk\"></Project>\n")
    _mk(root, "App.Tests/App.Tests.csproj", "<Project Sdk=\"Microsoft.NET.Sdk\"></Project>\n")
    _mk(root, "build/prepare.bat", "@echo off\n")
    _mk(root, "deploy/notify.ps1", "param($Channel)\n")


def test_scripted_pipeline_in_a_jenkins_named_text_file(tmp_path):
    _scripted(tmp_path)
    nodes, edges = _index(extract_jenkins.extract(tmp_path))
    p = "jenkinspipeline:ci/release-jenkins.pipeline.txt"
    s = "jenkinsstage:ci/release-jenkins.pipeline.txt#"
    assert nodes[p]["style"] == "scripted"
    assert nodes[p]["parameters"] == ["CHANNEL"]                  # properties([parameters(...)])
    assert {n for n in nodes if n.startswith(s)} == {s + "Prepare", s + "Release", s + "Verify"}
    # the brace on the next line still opens the stage body
    assert (s + "Prepare", "runs_script", "file:build/prepare.bat") in edges
    assert edges[(s + "Release", "depends_on", s + "Prepare")]["confidence"] == "INFERRED"
    assert (s + "Verify", "depends_on", s + "Release") in edges
    assert edges[(s + "Prepare", "uses_credential", "jenkinscredential:git-ssh")]["via"] == "checkout"
    env = "beanstalkenv:orders-api/orders-api-prod"
    assert edges[(s + "Release", "deploys", env)]["tool"] == "dotnet-eb"
    assert (nodes[env]["type"], nodes[env]["application"]) == ("beanstalk_environment", "orders-api")
    # the application the deploy names: the Beanstalk pass's node, linked to the environment
    assert nodes["beanstalkapp:orders-api"]["type"] == "beanstalk_application"
    assert ("beanstalkapp:orders-api", "has_environment", env) in edges
    # dir('App') makes the project the one deploy-environment publishes
    assert (s + "Release", "builds", "project:App/App.csproj") in edges
    assert nodes["project:App/App.csproj"]["type"] == "dotnet_project"
    assert (s + "Release", "runs_script", "module:deploy/notify.ps1") in edges
    # dotnet test -> the test project
    t = edges[(s + "Verify", "tests", "project:App.Tests/App.Tests.csproj")]
    assert (t["confidence"], t["command"]) == ("INFERRED", "dotnet test")
    dumped = json.dumps(nodes) + json.dumps(list(edges))
    assert "git.example.com" not in dumped and "chat-room" not in dumped and "trx" not in dumped


def test_beanstalk_ids_and_names_match_the_beanstalk_pass(tmp_path):
    # one deployment, one id: the Jenkins pass applies the Beanstalk pass's name rules, also to
    # the names it reads from a defaults file (an application name may hold spaces)
    from gateway.codegraph import extract_beanstalk
    _mk(tmp_path, "svc/Svc.csproj", "<Project Sdk=\"Microsoft.NET.Sdk.Web\"></Project>\n")
    _mk(tmp_path, "svc/aws-beanstalk-tools-defaults.json",
        json.dumps({"application": "Shop Api", "environment": "shop-prod"}))
    tok = "shopapi2024build7x9abcdefz"
    _mk(tmp_path, "Jenkinsfile", "node {\n  stage('D') {\n    dir('svc') { sh 'dotnet eb deploy-environment' }\n"
        "    sh 'dotnet eb deploy-environment -app \"Shop Api\" -env shop-qa'\n"
        f"    sh 'dotnet eb deploy-environment -app {tok} -env shop-dev'\n"
        "    sh 'eb deploy bad_env.name'\n  }\n}\n")
    jn, je = _index(extract_jenkins.extract(tmp_path))
    bn, _ = _index(extract_beanstalk.extract(tmp_path))
    env = "beanstalkenv:Shop Api/shop-prod"
    assert env in jn and env in bn
    keys = ("type", "file_type", "label", "application")
    assert {k: jn[env][k] for k in keys} == {k: bn[env][k] for k in keys}
    assert je[(S + "D", "deploys", env)]["confidence"] == "INFERRED"
    assert ("beanstalkapp:Shop Api", "has_environment", "beanstalkenv:Shop Api/shop-qa") in je
    assert {k: jn["beanstalkapp:Shop Api"][k] for k in ("type", "file_type", "label")} == \
        {k: bn["beanstalkapp:Shop Api"][k] for k in ("type", "file_type", "label")}
    # a token-shaped application name is dropped by both rules; `_` / `.` are no env names
    assert "beanstalkenv:-/shop-dev" in jn and tok not in json.dumps(list(jn.values()))
    assert not any("bad_env" in n for n in jn)


def test_dotnet_eb_defaults_file_names_only(tmp_path):
    _mk(tmp_path, "Jenkinsfile", "node {\n  stage('Deploy') {\n    dir('Svc') {\n"
        "      sh 'dotnet eb deploy-environment -cfg eb-defaults.json'\n"
        "      sh 'dotnet publish Svc.csproj -c Release -o out'\n    }\n  }\n}\n")
    _mk(tmp_path, "Svc/Svc.csproj", "<Project Sdk=\"Microsoft.NET.Sdk\"></Project>\n")
    _mk(tmp_path, "Svc/eb-defaults.json", json.dumps({
        "region": SECRET, "application": "svc-app", "environment": "svc-test",
        "additional-options": f"Value={SECRET}", "proxy-server": f"http://u:{SECRET}@proxy/"}))
    frag = extract_jenkins.extract(tmp_path)
    nodes, edges = _index(frag)
    e = edges[(S + "Deploy", "deploys", "beanstalkenv:svc-app/svc-test")]
    assert e["confidence"] == "INFERRED"
    assert edges[(S + "Deploy", "builds", "project:Svc/Svc.csproj")]["command"] in (
        "dotnet eb deploy-environment", "dotnet publish")
    assert SECRET not in json.dumps(frag)


def test_shell_deploys_pushes_and_working_directories(tmp_path):
    _mk(tmp_path, "Jenkinsfile", """\
pipeline {
  agent any
  stages {
    stage('Ship') {
      steps {
        sh '''
          cd services/api && ./run-tests.sh
          sh "$WORKSPACE/scripts/notify.sh"
          aws ecr put-image --repository-name team/api --image-tag 3.1 --image-manifest x
          aws --region eu-west-1 elasticbeanstalk create-application-version --application-name shop --version-label v1
          aws elasticbeanstalk update-environment --environment-name shop-prod --version-label v1
          eb deploy shop-staging --label v1 --timeout 20
          helm install metrics example-repo/metrics --namespace mon
          helm upgrade ${RELEASE} ./chart
        '''
        sh "docker buildx build --push -t example.org/team/worker:${env.BUILD_ID} -f worker/Dockerfile worker"
        load 'ci/helpers.groovy'
      }
    }
  }
}
""")
    _mk(tmp_path, "services/api/run-tests.sh", "echo\n")
    _mk(tmp_path, "scripts/notify.sh", "echo\n")
    _mk(tmp_path, "worker/Dockerfile", "FROM alpine:3.20\n")
    _mk(tmp_path, "ci/helpers.groovy", "def x() { }\nreturn this\n")
    nodes, edges = _index(extract_jenkins.extract(tmp_path))
    s = S + "Ship"
    assert nodes[P]["agent"] == "any"
    assert (s, "runs_script", "module:services/api/run-tests.sh") in edges
    assert (s, "runs_script", "module:scripts/notify.sh") in edges
    assert edges[(s, "runs_script", "module:ci/helpers.groovy")]["step"] == "load"
    assert edges[(s, "pushes", "extimage:team/api:3.1")]["tool"] == "aws-ecr"
    assert edges[(s, "deploys", "beanstalkenv:shop/shop-prod")]["confidence"] == "INFERRED"
    assert (s, "deploys", "beanstalkenv:-/shop-staging") in edges
    assert (s, "deploys", "helmrelease:mon/metrics") in edges
    assert not any(t.startswith("helmrelease:") and "RELEASE" in t for t in nodes)
    assert (s, "builds", "dockerfile:worker/Dockerfile") in edges
    assert edges[(s, "pushes", "extimage:example.org/team/worker")]["tag_unresolved"] is True
    # the ci/helpers.groovy library file holds no pipeline: it is a load target, not a pipeline
    assert "jenkinspipeline:ci/helpers.groovy" not in nodes


def test_groovy_docker_api_and_credentials(tmp_path):
    _mk(tmp_path, "build.jenkinsfile", """\
node('linux') {
  stage('Image') {
    def img = docker.build("example.org/team/svc:1.0", "-f ops/svc.Dockerfile .")
    docker.withRegistry('https://registry.example.org', 'registry-creds') {
      img.push('latest')
      img.push()
    }
    docker.image('postgres:16').inside('-e PGPASSWORD=x') {
      sh 'scripts/migrate.sh'
    }
    sshagent(['deploy-ssh', 'backup-ssh']) { sh 'true' }
    withAWS(credentials: 'aws-deployer', region: 'eu-west-1') { sh 'true' }
    withCredentials([string(credentialsId: 'api-token', variable: 'TOKEN'),
                     [$class: 'AmazonWebServicesCredentialsBinding', credentialsId: 'aws-keys']]) {
      sh 'true'
    }
  }
}
""")
    _mk(tmp_path, "ops/svc.Dockerfile", "FROM alpine:3.20\n")
    _mk(tmp_path, "scripts/migrate.sh", "echo\n")
    nodes, edges = _index(extract_jenkins.extract(tmp_path))
    s = "jenkinsstage:build.jenkinsfile#Image"
    assert (s, "builds", "dockerfile:ops/svc.Dockerfile") in edges
    assert (s, "pushes", "extimage:example.org/team/svc:latest") in edges
    assert (s, "pushes", "extimage:example.org/team/svc:1.0") in edges
    assert (s, "uses_image", "extimage:postgres:16") in edges
    assert (s, "runs_script", "module:scripts/migrate.sh") in edges
    creds = {t.split(":", 1)[1]: e["via"] for (src, r, t), e in edges.items()
             if r == "uses_credential"}
    assert creds == {"registry-creds": "withRegistry", "deploy-ssh": "sshagent",
                     "backup-ssh": "sshagent", "aws-deployer": "withAWS", "api-token": "string",
                     "aws-keys": "withCredentials"}
    assert "registry.example.org" not in json.dumps(list(nodes.values()))


def test_groovy_lexing_tolerance(tmp_path):
    _mk(tmp_path, "Jenkinsfile.release", """\
#!/usr/bin/env groovy
/* a block comment with a stray } brace
   and stage('Ghost') { } inside */
def branches = [:]
pipeline {
  agent none
  stages {
    stage("Prep ${params.TARGET}") {
      when { expression { env.BRANCH_NAME ==~ /release\\/.*|https?:\\/\\/x/ } }
      steps {
        script {
          def msg = "braces { in a ${ [a: 1].collect { k, v -> "${k}}" }.join(',') } string"
          def y = '''
            } } } not code
          '''
          sh \"\"\"
            echo "${msg}" // not a comment inside the shell text
            ./tools/prep.sh
          \"\"\"
        }
      }
    }
    stage('After') { steps { sh './tools/after.sh' } }
  }
}
""")
    _mk(tmp_path, "tools/prep.sh", "echo\n")
    _mk(tmp_path, "tools/after.sh", "echo\n")
    nodes, edges = _index(extract_jenkins.extract(tmp_path))
    s = "jenkinsstage:Jenkinsfile.release#"
    assert {n for n in nodes if n.startswith(s)} == {s + "Prep ${}", s + "After"}
    assert (s + "Prep ${}", "runs_script", "module:tools/prep.sh") in edges
    assert (s + "After", "runs_script", "module:tools/after.sh") in edges
    assert (s + "After", "depends_on", s + "Prep ${}") in edges
    assert nodes["jenkinspipeline:Jenkinsfile.release"]["agent"] == "none"


@pytest.mark.parametrize("rel,text", [
    ("lib/Helper.groovy", "class Helper {\n  def run() { node { sh 'x' } }\n}\n"),
    ("docs/jenkins-notes.md", "Use a pipeline { } block and node { } blocks.\n"),
    # a doc named for Jenkins with a real, fenced example pipeline is still a doc
    ("docs/jenkins-setup.md", "# Build\n\n```groovy\npipeline {\n  agent any\n  stages {\n"
                              "    stage('Build') { steps { sh './build.sh' } }\n  }\n}\n```\n"),
    ("docs/jenkins.rst", "Example::\n\n    node {\n      stage('x') { sh 'true' }\n    }\n"),
    ("Jenkinsfile", "// pipeline {\n//   stages { }\n// }\n/* node { } */\n"),
    ("Jenkinsfile", "def s = 'pipeline { agent any }'\nprintln s\n"),
    ("jenkins.yaml", "jenkins:\n  systemMessage: node {\n"),
    ("src/build.gradle", "pipeline {\n}\n"),      # not a pipeline file name
])
def test_content_gate(tmp_path, rel, text):
    _mk(tmp_path, rel, text)
    assert extract_jenkins.extract(tmp_path) == {"nodes": [], "edges": []}


def test_wrapped_and_library_entry_points(tmp_path):
    _mk(tmp_path, "vars/standardBuild.groovy",
        "def call(Map cfg) {\n  pipeline {\n    agent any\n    stages { stage('B') { steps { sh 'true' } } }\n  }\n}\n")
    _mk(tmp_path, "ci/Jenkinsfile",
        "timestamps {\n  podTemplate(yaml: '') {\n    node(POD_LABEL) {\n      stage('Run') { sh 'true' }\n    }\n  }\n}\n")
    nodes, _ = _index(extract_jenkins.extract(tmp_path))
    assert nodes["jenkinspipeline:vars/standardBuild.groovy"]["style"] == "declarative"
    assert "jenkinsstage:vars/standardBuild.groovy#B" in nodes
    assert nodes["jenkinspipeline:ci/Jenkinsfile"]["style"] == "scripted"
    assert "jenkinsstage:ci/Jenkinsfile#Run" in nodes


def test_secret_values_never_reach_the_graph(tmp_path):
    _mk(tmp_path, "Jenkinsfile", f"""\
// token {SECRET}
pipeline {{
  agent {{
    docker {{
      image 'example.org/tool:1'
      args '-e TOKEN={SECRET}'
      registryUrl 'https://user:{SECRET}@registry.example.org'
    }}
  }}
  environment {{
    API_TOKEN = '{SECRET}'
    API_URL = "https://user:{SECRET}@api.example.org/v1/{SECRET}/items?key={SECRET}"
    FROM_CRED = credentials('api-cred')
  }}
  parameters {{
    string(name: 'REGION', defaultValue: '{SECRET}', description: '{SECRET}')
    password(name: 'DB_PASSWORD', defaultValue: '{SECRET}')
    choice(name: 'ENV', choices: ['{SECRET}', 'b'])
  }}
  triggers {{
    cron('{SECRET}')
    GenericTrigger(token: '{SECRET}', genericVariables: [[key: 'ref', value: '{SECRET}']])
  }}
  options {{ buildDiscarder(logRotator(numToKeepStr: '{SECRET}')) }}
  stages {{
    stage('Deploy') {{
      agent {{ label '{SECRET}' }}
      environment {{ STAGE_SECRET = "{SECRET}" }}
      steps {{
        sh "curl -H 'Authorization: Bearer {SECRET}' -u user:{SECRET} https://h/{SECRET}?q={SECRET}"
        sh '''
          export DB_PASSWORD={SECRET}
          docker build --build-arg TOKEN={SECRET} -t app:1 .
          helm upgrade --install web ./chart --set password={SECRET} --password {SECRET}
          aws elasticbeanstalk update-environment --environment-name env-a --option-settings Namespace=x,OptionName=y,Value={SECRET}
          dotnet eb deploy-environment -app app-a -env env-b --additional-options Value={SECRET}
          helm upgrade --kube-tls-server-name {SECRET} rel ./chart
          helm upgrade --keyring {SECRET} rel2 ./chart
          podman push --tls-verify {SECRET} example.org/team/img:1
          podman push --sign-passphrase-file {SECRET} example.org/team/img:2
        '''
        withCredentials([usernamePassword(credentialsId: 'db', usernameVariable: 'U', passwordVariable: 'P')]) {{
          bat "deploy.cmd --password {SECRET}"
        }}
        git url: 'https://user:{SECRET}@git.example.org/repo.git', credentialsId: 'git-cred'
        httpRequest url: 'https://api.example.org/{SECRET}', customHeaders: [[name: 'X-Key', value: '{SECRET}']],
                    requestBody: '{SECRET}'
        input message: '{SECRET}'
        build job: 'downstream', parameters: [string(name: 'X', value: '{SECRET}')]
        docker.withRegistry('https://{SECRET}@registry.example.org', 'reg-cred') {{ sh 'true' }}
        docker.image('user:{SECRET}@registry.example.org/x').inside {{ sh 'true' }}
      }}
    }}
  }}
}}
""")
    _mk(tmp_path, "Dockerfile", "FROM alpine:3.20\n")
    _mk(tmp_path, "chart/Chart.yaml", "apiVersion: v2\nname: c\nversion: 1.0.0\n")
    frag = extract_jenkins.extract(tmp_path)
    dumped = json.dumps(frag)
    assert SECRET not in dumped
    assert "user:" not in dumped and "example.org/v1" not in dumped
    nodes, edges = _index(frag)
    # names survive: environment and parameter names, credential ids, targets
    assert nodes[P]["environment"] == ["API_TOKEN", "API_URL", "FROM_CRED"]
    assert nodes[P]["parameters"] == ["REGION", "DB_PASSWORD", "ENV"]
    assert nodes[P]["triggers"] == ["cron", "GenericTrigger"]
    for cid in ("api-cred", "db", "git-cred", "reg-cred"):
        assert f"jenkinscredential:{cid}" in nodes
    assert "helmrelease:-/web" in nodes and "beanstalkenv:app-a/env-b" in nodes
    assert "beanstalkenv:-/env-a" in nodes
    # a value-taking flag's argument is never read as the release or image
    assert {"helmrelease:-/rel", "helmrelease:-/rel2"} <= set(nodes)
    assert {"extimage:example.org/team/img:1", "extimage:example.org/team/img:2"} <= set(nodes)


def test_symlinks_out_of_root_are_not_read(tmp_path):
    root, outside = tmp_path / "repo", tmp_path / "outside"
    _mk(outside, "Jenkinsfile", f"pipeline {{ stages {{ stage('{SECRET}') {{ }} }} }}\n")
    _mk(outside, "evil.sh", "echo\n")
    _mk(root, "Jenkinsfile", "node { stage('A') { sh './link.sh' } }\n")
    try:
        os.symlink(outside / "Jenkinsfile", root / "linked-jenkins.groovy")
        os.symlink(outside / "evil.sh", root / "link.sh")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    frag = extract_jenkins.extract(root)
    dumped = json.dumps(frag)
    assert SECRET not in dumped and "link.sh" not in dumped and "linked-jenkins" not in dumped
    assert S + "A" in _index(frag)[0]


def test_pruned_and_excluded_directories(tmp_path):
    body = "node { stage('A') { sh 'true' } }\n"
    _mk(tmp_path, "node_modules/pkg/Jenkinsfile", body)
    _mk(tmp_path, ".ci/Jenkinsfile", body)
    _mk(tmp_path, "legacy/Jenkinsfile", body)
    assert extract_jenkins.extract(tmp_path, frozenset({"legacy"}))["nodes"] == []
    nodes, _ = _index(extract_jenkins.extract(tmp_path, include=frozenset({".ci"})))
    assert set(n for n in nodes if n.startswith("jenkinspipeline:")) == {
        "jenkinspipeline:.ci/Jenkinsfile", "jenkinspipeline:legacy/Jenkinsfile"}


def test_malformed_input_never_raises(tmp_path):
    cases = {
        "a/Jenkinsfile": "pipeline {\n  stages {\n    stage('x') {\n      steps { sh 'unclosed",
        "b/Jenkinsfile": "node { " + "{" * 5000 + " stage('deep') { } " + "}" * 10,
        "c/Jenkinsfile": "node { sh \"\"\"never closed ${ " + "x" * 1000,
        "d/Jenkinsfile": "}}}} node ))) { ]]] stage(( { sh( }",
        "e/Jenkinsfile": "node { stage('a') { sh(script: ) ; bat label: ; docker.build( } }",
        "f/Jenkinsfile": "\x00binary pipeline { }",
        "g/Jenkinsfile": "node { stage('ok') { sh './x.sh' } }\n",
    }
    for rel, text in cases.items():
        _mk(tmp_path, rel, text)
    nodes, _ = _index(extract_jenkins.extract(tmp_path))
    assert "jenkinsstage:g/Jenkinsfile#ok" in nodes
    assert "jenkinspipeline:f/Jenkinsfile" not in nodes


def test_hostile_input_costs_linear_time(tmp_path):
    _mk(tmp_path, "a/Jenkinsfile", "node {\n" + "x = ( / ( / " * 40_000 + "\n}\n")
    _mk(tmp_path, "b/Jenkinsfile", "node { sh \"" + "${" * 100_000 + "\" }\n")
    _mk(tmp_path, "c/Jenkinsfile", "node {\n" + "sh(sh(sh(" * 20_000 + "\n}\n")
    _mk(tmp_path, "d/Jenkinsfile", "node {\n" + "stage('s') {\n" * 3000 + "}\n" * 3000 + "}\n")
    _mk(tmp_path, "e/Jenkinsfile", "node {\n sh '''" + "cd a && ./b.sh\n" * 20_000 + "''' }\n")
    # unclosed calls whose argument lists are searched: load(, properties(parameters(, x.push(
    _mk(tmp_path, "f/Jenkinsfile", "node {\n" + "load(" * 100_000 + "\n}\n")
    _mk(tmp_path, "g/Jenkinsfile", "node {\n properties(" + "parameters(" * 100_000 + "\n}\n")
    _mk(tmp_path, "h/Jenkinsfile", "node {\n x = docker.image('a/b')\n" + "x.push(" * 100_000 + "\n}\n")
    _mk(tmp_path, "i/Jenkinsfile", "node {\n properties(" + "pipelineTriggers(" * 100_000 + "\n}\n")
    t = time.monotonic()
    nodes, _ = _index(extract_jenkins.extract(tmp_path))
    assert time.monotonic() - t < 20
    assert "jenkinspipeline:a/Jenkinsfile" in nodes
    assert {f"jenkinspipeline:{d}/Jenkinsfile" for d in "fghi"} <= set(nodes)


def test_filesystem_lookups_are_capped_per_file(tmp_path):
    # every distinct path costs a stat: a file of them may only spend _MAX_PROBES
    n = extract_jenkins._MAX_PROBES * 3
    _mk(tmp_path, "Jenkinsfile", "node {\n sh '''\n" + "".join(f"./x{i}.sh\n" for i in range(n)) + "''' }\n")
    _mk(tmp_path, "x0.sh", "echo\n")
    p = extract_jenkins._Pass(tmp_path)
    p.run_file(tmp_path / "Jenkinsfile", "Jenkinsfile")
    assert len(p.kinds) <= extract_jenkins._MAX_PROBES
    assert (P, "runs_script", "module:x0.sh") in _index({"nodes": [], "edges": p.edges})[1]


def test_oversized_file_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_jenkins, "_MAX_BYTES", 100)
    _mk(tmp_path, "Jenkinsfile", "node { stage('A') { sh 'true' } }\n" + "// pad\n" * 50)
    assert extract_jenkins.extract(tmp_path)["nodes"] == []


def test_node_budget_caps_one_file(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_jenkins, "_MAX_NODES", 5)
    _mk(tmp_path, "Jenkinsfile", "node {\n" + "".join(f"stage('s{i}') {{ }}\n" for i in range(50)) + "}\n")
    _mk(tmp_path, "b/Jenkinsfile", "node { stage('one') { } }\n")
    nodes, _ = _index(extract_jenkins.extract(tmp_path))
    assert len([n for n in nodes if n.startswith(S)]) == 4
    assert "jenkinsstage:b/Jenkinsfile#one" in nodes


def test_an_unforeseen_error_costs_only_that_file(tmp_path, monkeypatch):
    _mk(tmp_path, "a/Jenkinsfile", "node { stage('boom') { sh './x.sh' } }\n")
    _mk(tmp_path, "b/Jenkinsfile", "node { stage('fine') { sh 'true' } }\n")
    real = extract_jenkins._Pass.stage

    def stage(self, sname, ctx, line):
        if sname == "boom":
            raise RuntimeError("unforeseen")
        return real(self, sname, ctx, line)
    monkeypatch.setattr(extract_jenkins._Pass, "stage", stage)
    frag = extract_jenkins.extract(tmp_path)
    ids = {n["id"] for n in frag["nodes"]}
    assert not any("a/Jenkinsfile" in i for i in ids)
    assert "jenkinsstage:b/Jenkinsfile#fine" in ids
    assert all("a/Jenkinsfile" not in e["source"] for e in frag["edges"])


def test_deterministic(tmp_path):
    _declarative(tmp_path)
    _scripted(tmp_path)
    first = json.dumps(extract_jenkins.extract(tmp_path))
    assert json.dumps(extract_jenkins.extract(tmp_path)) == first


def test_merged_graph_joins_other_passes(tmp_path, monkeypatch):
    # in the registered order (jenkins, then beanstalk, http and aspnet), every target the
    # pipeline names is a node another pass (or this one) typed, and no value reaches the graph
    pytest.importorskip("networkx")
    from gateway.codegraph import build, build_graph, treesitter
    monkeypatch.setattr(treesitter, "AVAILABLE", False)          # [graph-slim]
    order = [name for name, _ in build.REPO_PASSES]
    assert order.count("jenkins") == 1
    assert order.index("jenkins") < order.index("beanstalk") < order.index("http") < order.index("aspnet")
    _declarative(tmp_path)
    _scripted(tmp_path)
    # an environment the Beanstalk pass owns, deployed from a Jenkinsfile line further down
    _mk(tmp_path, "App/aws-beanstalk-tools-defaults.json",
        json.dumps({"application": "orders-api", "environment": "orders-api-prod"}))
    _mk(tmp_path, "Jenkinsfile.secrets",
        f"pipeline {{ environment {{ T = '{SECRET}' }} stages {{ stage('x') {{ steps {{ "
        f"sh 'curl -u u:{SECRET} https://h/{SECRET}' }} }} }} }}\n")
    data = build_graph(tmp_path)
    assert data["graph"]["passes"]["jenkins"] > 0
    assert SECRET not in json.dumps(data)
    nodes = {n["id"]: n for n in data["nodes"]}
    assert nodes["helmchart:charts/web"]["type"] == "helm_chart"
    assert nodes["dockerfile:docker/app.Dockerfile"]["type"] == "dockerfile"
    assert nodes["project:App/App.csproj"]["type"] == "dotnet_project"
    assert nodes["module:scripts/build.sh"]["type"] == "module"
    assert not [i for i, n in nodes.items() if "type" not in n]
    # the shared environment: the Beanstalk pass's file, and no line from the Jenkins file
    env = nodes["beanstalkenv:orders-api/orders-api-prod"]
    assert env["source_file"] == "App/aws-beanstalk-tools-defaults.json"
    assert "source_location" not in env
    # every source_location belongs to the source_file beside it: a line of that file
    for nid, n in nodes.items():
        loc, src = n.get("source_location"), n.get("source_file")
        if isinstance(loc, str) and loc.startswith("L") and src:
            lines = (tmp_path / src).read_text(encoding="utf-8").count("\n") + 1
            assert int(loc[1:]) <= lines, nid


def test_is_pipeline_name():
    for n in ("Jenkinsfile", "jenkinsfile", "Jenkinsfile.prod", "app.jenkinsfile", "x.groovy",
              "release.jenkins.txt", "JENKINS.conf"):
        assert extract_jenkins.is_pipeline_name(n), n
    for n in ("Makefile", "build.gradle", "pipeline.yml", "main.py", "jenkins-setup.md",
              "JENKINS.rst", "jenkins.adoc", "jenkins-guide.html"):
        assert not extract_jenkins.is_pipeline_name(n), n
