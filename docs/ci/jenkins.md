# Publishing from Jenkins

The same two paths as everywhere else — upload the report when the suite
finishes, or stream while it runs — expressed as a declarative pipeline. Nothing
here needs a Jenkins plugin; `curl` and the `test-hub` CLI are the whole
integration.

## Before you start

Create the token on a machine that has Test Hub installed:

```shell
$ test-hub token create --label jenkins
```

Store the **token** as a *Secret text* credential (Manage Jenkins → Credentials)
with the id `test-hub-token`. Put the **hash** in the server's
`TEST_HUB_API_TOKEN_HASHES`. Never put the token in the Jenkinsfile: a
Jenkinsfile is in version control, and Jenkins will helpfully print it.

## Upload a report when the suite finishes

```groovy
pipeline {
  agent any

  environment {
    TEST_HUB_URL = 'https://test-hub.internal'
    // Binds the credential to an environment variable for the whole pipeline.
    // Jenkins masks it in the console output; it is still worth never echoing it.
    TEST_HUB_TOKEN = credentials('test-hub-token')
  }

  stages {
    stage('Test') {
      steps {
        // Do not let a failing suite abort the pipeline before the results are
        // published — that loses exactly the run you wanted to look at. Record
        // the failure and carry on to `post`.
        sh 'pytest --junitxml=results.xml || true'
      }
    }
  }

  post {
    // `always`, not `success`: the interesting runs are the failing ones.
    always {
      sh '''
        set -eu
        curl --fail-with-body -sS -X POST "$TEST_HUB_URL/api/v1/imports" \
          -H "Authorization: Bearer $TEST_HUB_TOKEN" \
          -F "file=@results.xml" \
          -F "project=payments-api" \
          -F "team=payments" \
          -F "component=checkout" \
          -F "type=component" \
          -F "environment=ci" \
          -F "branch=${BRANCH_NAME:-$GIT_BRANCH}" \
          -F "commit=${GIT_COMMIT}" \
          -F "ci_provider=jenkins" \
          -F "pipeline_id=${BUILD_NUMBER}" \
          -F "pipeline_url=${BUILD_URL}"
      '''
      junit 'results.xml'   // keep Jenkins' own view as well, if you use it
    }
  }
}
```

`--fail-with-body` is not optional. Without it `curl` exits 0 on a 4xx, the stage
goes green, and nothing was published. With it you get the server's
`{"error": ..., "message": ...}` in the console and a red stage.

`set -eu` matters for the same reason: a `sh` step reports the exit status of the
*last* command, so without `-e` a failing `curl` in the middle of a script is
invisible.

If Python is on the agent, the CLI reads better and does the same thing:

```groovy
      sh '''
        set -eu
        pip install --quiet test-hub
        test-hub publish results.xml \
          --url "$TEST_HUB_URL" \
          --project payments-api --team payments --type component \
          --branch "${BRANCH_NAME:-$GIT_BRANCH}" --commit "$GIT_COMMIT" \
          --pipeline-id "$BUILD_NUMBER" --pipeline-url "$BUILD_URL" \
          --token-env TEST_HUB_TOKEN
      '''
```

`--token-env` names the variable; the value never appears in the command line,
where it would be visible in `ps` to anything else on the agent and captured by
any build step that traces its commands.

## Parallel stages that belong to one run

Jenkins `parallel` stages each produce their own report. Published separately
they become four unrelated runs. Generate one run id before the parallel block
and give it to every branch:

```groovy
pipeline {
  agent any
  environment {
    TEST_HUB_URL = 'https://test-hub.internal'
    TEST_HUB_TOKEN = credentials('test-hub-token')
  }

  stages {
    stage('Prepare') {
      steps {
        script {
          // One id, shared. Deriving it from BUILD_TAG would work equally well,
          // as long as every branch derives the same one.
          env.TEST_HUB_RUN_ID = sh(
            script: 'python3 -c "import uuid; print(uuid.uuid4())"',
            returnStdout: true
          ).trim()
        }
        echo "Test Hub run: ${env.TEST_HUB_RUN_ID}"
      }
    }

    stage('Test') {
      parallel {
        stage('unit')        { steps { shardedTests('unit', '1/3') } }
        stage('integration') { steps { shardedTests('integration', '2/3') } }
        stage('e2e')         { steps { shardedTests('e2e', '3/3') } }
      }
    }
  }
}

// Each branch publishes into the same run id, so the four reports fold into one.
void shardedTests(String name, String shard) {
  sh """
    set -eu
    pytest --junitxml=${name}.xml --shard=${shard} || true
    test-hub publish ${name}.xml \
      --url "\$TEST_HUB_URL" \
      --project payments-api --team payments --type component \
      --run-id "\$TEST_HUB_RUN_ID" \
      --branch "\${BRANCH_NAME:-\$GIT_BRANCH}" --commit "\$GIT_COMMIT" \
      --token-env TEST_HUB_TOKEN
  """
}
```

Order does not matter, and re-running one branch is safe: its events were
already applied, so they are ignored and no count moves.

## Streaming while the suite runs

```groovy
    stage('Open the run') {
      steps {
        script {
          env.TEST_HUB_RUN_ID = sh(returnStdout: true, script: '''
            set -eu
            curl --fail-with-body -sS -X POST "$TEST_HUB_URL/api/v1/runs" \
              -H "Authorization: Bearer $TEST_HUB_TOKEN" \
              -H 'content-type: application/json' \
              -d '{"project_key":"payments-api","test_type":"e2e",
                   "context":{"ci_provider":"jenkins"}}' \
              | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])"
          ''').trim()
        }
      }
    }
    // … the suite posts to /api/v1/runs/$TEST_HUB_RUN_ID/events as tests finish …
    stage('Close the run') {
      steps {
        sh '''
          set -eu
          curl --fail-with-body -sS -X POST \
            "$TEST_HUB_URL/api/v1/runs/$TEST_HUB_RUN_ID/finish" \
            -H "Authorization: Bearer $TEST_HUB_TOKEN" \
            -H 'content-type: application/json' -d '{}'
        '''
      }
    }
```

An aborted build never reaches the close stage, and the run stays open in the
dashboard. That is the honest representation: nobody told Test Hub it finished.

## A scripted-pipeline equivalent

```groovy
node {
  checkout scm
  withCredentials([string(credentialsId: 'test-hub-token', variable: 'TEST_HUB_TOKEN')]) {
    try {
      sh 'pytest --junitxml=results.xml'
    } finally {
      sh '''
        set -eu
        test-hub publish results.xml \
          --url https://test-hub.internal \
          --project payments-api --type component \
          --token-env TEST_HUB_TOKEN
      '''
    }
  }
}
```

`try`/`finally` is the scripted equivalent of `post { always { … } }`, and
`withCredentials` scopes the token to the block instead of the whole build.

## Things that go wrong

| Symptom | Cause |
|---|---|
| Stage green, nothing published | `curl` without `--fail-with-body`, or a `sh` script without `set -e`. |
| Results only for passing builds | Publishing in `post { success }` instead of `post { always }`, or a failing `pytest` aborting the stage. |
| One run per parallel branch | The branches did not share `--run-id`. |
| `401 unauthorized` | The credential id does not match, so the variable is empty. |
| Branch shows as `origin/main` | `GIT_BRANCH` includes the remote. Prefer `BRANCH_NAME` on a multibranch pipeline. |
| Token visible in the console | It was passed as an argument instead of through `--token-env`. |
