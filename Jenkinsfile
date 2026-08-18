@Library('deepthink-pipeline-alerts') _

pipeline {
  agent any

  options {
    timestamps()
    disableConcurrentBuilds()
    skipDefaultCheckout(true)
    timeout(time: 180, unit: 'MINUTES')
    buildDiscarder(logRotator(numToKeepStr: '10', artifactNumToKeepStr: '5'))
  }

  parameters {
    string(name: 'REGISTRY', defaultValue: 'deepthink-registry-vpc.us-east-1.cr.aliyuncs.com', description: '镜像仓库主机名')
  }

  environment {
    DOCKER_BUILDKIT = '1'
    IMAGE_NAME = 'openmontage'
    DEPLOY_NAMESPACE = 'openmontage-test'
    DEPLOY_OVERLAY = 'deploy/kubernetes/overlays/test'
    DINGTALK_ROBOT = '测试环境钉钉CICD通知告警机器人'
  }

  stages {
    stage('Checkout') {
      steps {
        checkout scm
        script {
          env.SOURCE_BRANCH = sh(
            script: '''
              set -eu
              branch="${gitlabSourceBranch:-${BRANCH_NAME:-${GIT_BRANCH:-}}}"
              if [ -z "${branch}" ]; then
                branch="$(git symbolic-ref --short -q HEAD || true)"
              fi
              if [ -z "${branch}" ] && git show-ref --verify --quiet refs/remotes/origin/test && \
                 [ "$(git rev-parse HEAD)" = "$(git rev-parse refs/remotes/origin/test)" ]; then
                branch=test
              fi
              branch="${branch#refs/remotes/origin/}"
              branch="${branch#refs/heads/}"
              branch="${branch#origin/}"
              printf '%s' "${branch}"
            ''',
            returnStdout: true
          ).trim()
          if (env.SOURCE_BRANCH != 'test') {
            error("Unsupported source branch '${env.SOURCE_BRANCH}'; expected 'test'")
          }

          def registryHost = params.REGISTRY?.trim()
          if (!registryHost || !(registryHost ==~ /[A-Za-z0-9.-]+(?::[0-9]+)?/)) {
            error('REGISTRY must be a registry host without a path')
          }
          env.REGISTRY_HOST = registryHost
          env.RUNTIME_IMAGE = "${registryHost}/test/${env.IMAGE_NAME}"
          env.EFFECTIVE_TAG = env.BUILD_NUMBER
          env.CI_IMAGE = "${env.IMAGE_NAME}-ci:${env.BUILD_NUMBER}"
          env.GIT_COMMIT = sh(script: 'git rev-parse HEAD', returnStdout: true).trim()
          env.BUILD_DATE_UTC = sh(script: 'date -u +%Y-%m-%dT%H:%M:%SZ', returnStdout: true).trim()
          env.KUBE_CONTEXT = sh(
            script: '''
              set -eu
              config_file="${KUBECONFIG:-${HOME}/.kube/config}"
              test -r "${config_file}"
              awk '/^current-context:[[:space:]]*/ { sub(/^current-context:[[:space:]]*/, ""); print; exit }' "${config_file}"
            ''',
            returnStdout: true
          ).trim()
          if (!env.KUBE_CONTEXT) {
            error('Unable to resolve the Jenkins agent Kubernetes context')
          }
          echo "Building ${env.RUNTIME_IMAGE}:${env.EFFECTIVE_TAG} from ${env.GIT_COMMIT}"
        }
      }
    }

    stage('Prerequisites') {
      steps {
        sh '''
          set -eu
          docker version >/dev/null
          kubectl --context="${KUBE_CONTEXT}" apply -f "${DEPLOY_OVERLAY}/namespace.yaml" >/dev/null
          kubectl --context="${KUBE_CONTEXT}" get namespace "${DEPLOY_NAMESPACE}" >/dev/null
          kubectl --context="${KUBE_CONTEXT}" -n "${DEPLOY_NAMESPACE}" get secret openmontage-oss >/dev/null
        '''
      }
    }

    stage('Build CI Image') {
      steps {
        sh '''
          set -eu
          docker build --pull --target test --build-context test-suite=tests \
            --build-context contracts=contracts \
            -t "${CI_IMAGE}" .
        '''
      }
    }

    stage('Tests') {
      steps {
        sh '''
          set -eu
          mkdir -p test-results
          container="openmontage-tests-${BUILD_NUMBER}"
          docker rm -f "${container}" >/dev/null 2>&1 || true
          docker create --name "${container}" "${CI_IMAGE}" \
            python -m pytest tests -q -p no:cacheprovider --junitxml=/tmp/junit.xml >/dev/null
          set +e
          docker start -a "${container}"
          test_status=$?
          set -e
          docker cp "${container}:/tmp/junit.xml" test-results/junit.xml >/dev/null 2>&1 || true
          docker rm -f "${container}" >/dev/null 2>&1 || true
          exit "${test_status}"
        '''
      }
      post {
        always {
          junit testResults: 'test-results/*.xml', allowEmptyResults: true
        }
      }
    }

    stage('Build Runtime Image') {
      steps {
        sh '''
          set -eu
          docker build --pull --target runtime \
            --build-arg BUILD_DATE="${BUILD_DATE_UTC}" \
            --build-arg VCS_REF="${GIT_COMMIT}" \
            --build-arg VERSION="${EFFECTIVE_TAG}" \
            -t "${RUNTIME_IMAGE}:${EFFECTIVE_TAG}" .
          docker inspect "${RUNTIME_IMAGE}:${EFFECTIVE_TAG}" > image-inspect.json
        '''
      }
    }

    stage('Runtime Smoke Test') {
      steps {
        sh '''
          set -eu
          container="openmontage-runtime-${BUILD_NUMBER}"
          cleanup() {
            docker rm -f "${container}" >/dev/null 2>&1 || true
          }
          trap cleanup EXIT HUP INT TERM
          cleanup
          docker run -d --name "${container}" --read-only \
            --tmpfs /tmp:rw,noexec,nosuid,size=1g,uid=1000,gid=1000 \
            --tmpfs /workspace/projects:rw,nosuid,size=1g,uid=1000,gid=1000 \
            --tmpfs /workspace/.backlot:rw,nosuid,size=1g,uid=1000,gid=1000 \
            --tmpfs /home/node/.cache:rw,nosuid,size=1g,uid=1000,gid=1000 \
            --tmpfs /home/node/.npm:rw,nosuid,size=256m,uid=1000,gid=1000 \
            "${RUNTIME_IMAGE}:${EFFECTIVE_TAG}" >/dev/null
          healthy=false
          for attempt in $(seq 1 60); do
            if docker exec "${container}" curl --fail --silent http://127.0.0.1:4750/api/health >/dev/null 2>&1; then
              healthy=true
              break
            fi
            sleep 2
          done
          if [ "${healthy}" != true ]; then
            docker logs "${container}" || true
            exit 1
          fi
          docker exec "${container}" python -c \
            'import json, urllib.request; data=json.load(urllib.request.urlopen("http://127.0.0.1:4750/api/health")); assert data == {"ok": True, "app": "backlot"}'
        '''
      }
    }

    stage('Validate Manifest') {
      steps {
        sh '''
          set -eu
          kubectl --context="${KUBE_CONTEXT}" kustomize "${DEPLOY_OVERLAY}" \
            | sed "s|registry.example.invalid/openmontage:test|${RUNTIME_IMAGE}:${EFFECTIVE_TAG}|g" \
            > openmontage-test.rendered.yaml
          kubectl --context="${KUBE_CONTEXT}" -n "${DEPLOY_NAMESPACE}" apply \
            --dry-run=server -f openmontage-test.rendered.yaml >/dev/null
        '''
      }
    }

    stage('Push Image') {
      steps {
        sh '''
          set -eu
          docker push "${RUNTIME_IMAGE}:${EFFECTIVE_TAG}"
        '''
      }
    }

    stage('Deploy') {
      steps {
        sh '''
          set -eu
          kubectl --context="${KUBE_CONTEXT}" -n "${DEPLOY_NAMESPACE}" apply \
            -f openmontage-test.rendered.yaml
          for deployment in $(kubectl --context="${KUBE_CONTEXT}" -n "${DEPLOY_NAMESPACE}" get deployment \
            -l app.kubernetes.io/part-of=openmontage -o name); do
            kubectl --context="${KUBE_CONTEXT}" -n "${DEPLOY_NAMESPACE}" rollout status \
              "${deployment}" --timeout=30m
          done
        '''
      }
    }

    stage('Cluster Smoke Test') {
      steps {
        sh '''
          set -eu
          kubectl --context="${KUBE_CONTEXT}" -n "${DEPLOY_NAMESPACE}" get \
            deployment,pod,service -l app.kubernetes.io/part-of=openmontage -o wide
          test "$(kubectl --context="${KUBE_CONTEXT}" -n "${DEPLOY_NAMESPACE}" get \
            deployment -l app.kubernetes.io/part-of=openmontage -o jsonpath='{.items[*].status.conditions[?(@.type=="Available")].status}' \
            | tr ' ' '\n' | grep -c '^True$')" -ge 4
        '''
      }
    }
  }

  post {
    failure {
      script {
        if (env.KUBE_CONTEXT) {
          sh '''
            kubectl --context="${KUBE_CONTEXT}" -n "${DEPLOY_NAMESPACE}" get deployment,pod,service,endpointslice \
              -l app.kubernetes.io/part-of=openmontage -o wide || true
            kubectl --context="${KUBE_CONTEXT}" -n "${DEPLOY_NAMESPACE}" get deployment \
              -l app.kubernetes.io/part-of=openmontage -o name || true
          '''
        }
      }
    }
    always {
      archiveArtifacts artifacts: 'image-inspect.json,openmontage-test.rendered.yaml', allowEmptyArchive: true, fingerprint: true
      dingtalkAlert(
        robot: env.DINGTALK_ROBOT,
        environment: '测试环境',
        service: 'openmontage',
        branch: env.SOURCE_BRANCH ?: 'unknown',
        details: [
          '命名空间': env.DEPLOY_NAMESPACE,
          '镜像': env.EFFECTIVE_TAG ? "${env.RUNTIME_IMAGE}:${env.EFFECTIVE_TAG}" : '未生成'
        ]
      )
    }
    cleanup {
      sh '''
        docker rm -f "openmontage-tests-${BUILD_NUMBER}" "openmontage-runtime-${BUILD_NUMBER}" \
          >/dev/null 2>&1 || true
        docker image rm -f "${CI_IMAGE:-}" >/dev/null 2>&1 || true
        if [ -n "${RUNTIME_IMAGE:-}" ] && [ -n "${EFFECTIVE_TAG:-}" ]; then
          docker image rm -f "${RUNTIME_IMAGE}:${EFFECTIVE_TAG}" >/dev/null 2>&1 || true
        fi
      '''
    }
  }
}
