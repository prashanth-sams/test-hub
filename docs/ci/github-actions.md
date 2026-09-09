# Publishing from GitHub Actions

Two ways to get results into Test Hub from a GitHub workflow: upload the report
file when the suite finishes, or stream results while it runs. Start with the
first — it is three lines and works with any framework that can write JUnit XML.

Everything here is plain `curl` or the `test-hub` CLI. There is no Test Hub
action to install and nothing GitHub-specific in the API.

## Before you start

Create a token on a machine that has Test Hub installed:

```shell
$ test-hub token create --label github-actions
```

It prints two things. The **token** goes into your repository's secrets as
`TEST_HUB_TOKEN` (Settings → Secrets and variables → Actions). The **hash** goes
into the server's `TEST_HUB_API_TOKEN_HASHES`. The server never sees the token
itself, and nothing can recover it from the server if you lose it.

## Upload a report when the suite finishes

```yaml
name: Tests

on: [push, pull_request]

permissions:
  contents: read

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install
        run: pip install -e ".[dev]"

      # `--junitxml` is pytest's; every framework has an equivalent. Nothing
      # about the report has to know Test Hub exists.
      - name: Run tests
        run: pytest --junitxml=results.xml

      - name: Publish results
        # `always()` is the whole point of this step. Without it the step is
        # skipped exactly when the tests failed — which is the run you most
        # wanted in the dashboard.
        if: always()
        env:
          TEST_HUB_TOKEN: ${{ secrets.TEST_HUB_TOKEN }}
        run: |
          curl --fail-with-body -sS -X POST "$TEST_HUB_URL/api/v1/imports" \
            -H "Authorization: Bearer $TEST_HUB_TOKEN" \
            -F "file=@results.xml" \
            -F "project=payments-api" \
            -F "team=payments" \
            -F "component=checkout" \
            -F "type=component" \
            -F "environment=ci" \
            -F "branch=${GITHUB_REF_NAME}" \
            -F "commit=${GITHUB_SHA}" \
            -F "trigger=${{ github.event_name == 'pull_request' && 'pull_request' || 'push' }}" \
            -F "ci_provider=github-actions" \
            -F "pipeline_id=${GITHUB_RUN_ID}" \
            -F "pipeline_url=${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
        env:
          TEST_HUB_URL: https://test-hub.internal
```

`--fail-with-body` matters: without it `curl` exits 0 on a 4xx and the step goes
green while nothing was published. With it, you get the server's
`{"error": "...", "message": "..."}` in the log and a failing step.

If Python is already in the job, the CLI is tidier and gives the same result:

```yaml
      - name: Publish results
        if: always()
        env:
          TEST_HUB_TOKEN: ${{ secrets.TEST_HUB_TOKEN }}
        run: |
          pip install test-hub
          test-hub publish results.xml \
            --url https://test-hub.internal \
            --project payments-api --team payments --type component \
            --branch "$GITHUB_REF_NAME" --commit "$GITHUB_SHA" \
            --pipeline-id "$GITHUB_RUN_ID" \
            --token-env TEST_HUB_TOKEN
```

The token is read from the environment, never passed as an argument — an
argument is visible in `ps` to every other process on the runner and is echoed
into the log if the step is traced.

## Shards that belong to one run

A matrix job produces one report per shard. Published naively that is four runs
with a quarter of the tests each, and the dashboard shows four unrelated
half-red runs instead of one complete one.

Give every shard the same run id, generated once in a job that runs before them:

```yaml
jobs:
  prepare:
    runs-on: ubuntu-latest
    outputs:
      run_id: ${{ steps.gen.outputs.run_id }}
    steps:
      - id: gen
        # A UUID generated once and shared. Deriving one from GITHUB_RUN_ID
        # would also work, as long as every shard derives the same one.
        run: echo "run_id=$(uuidgen | tr 'A-Z' 'a-z')" >> "$GITHUB_OUTPUT"

  test:
    needs: prepare
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        shard: [1, 2, 3, 4]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -e ".[dev]" test-hub

      - run: pytest --junitxml=results.xml --shard=${{ matrix.shard }}/4

      - name: Publish this shard into the shared run
        if: always()
        env:
          TEST_HUB_TOKEN: ${{ secrets.TEST_HUB_TOKEN }}
        run: |
          test-hub publish results.xml \
            --url https://test-hub.internal \
            --project payments-api --team payments --type component \
            --run-id "${{ needs.prepare.outputs.run_id }}" \
            --branch "$GITHUB_REF_NAME" --commit "$GITHUB_SHA" \
            --token-env TEST_HUB_TOKEN
```

Four shards publishing into one run id produce one run with all the tests. The
order they arrive in does not matter, and a re-run of a single shard replaces
nothing — its events were already applied, so they are ignored and the counts do
not move.

## Streaming results while the suite runs

Worth it when a suite takes long enough that people watch it. Open a run first,
post results as they finish, close it at the end:

```yaml
      - name: Open the run
        id: run
        env:
          TEST_HUB_TOKEN: ${{ secrets.TEST_HUB_TOKEN }}
        run: |
          RUN=$(curl --fail-with-body -sS -X POST "$TEST_HUB_URL/api/v1/runs" \
            -H "Authorization: Bearer $TEST_HUB_TOKEN" \
            -H 'content-type: application/json' \
            -d "{\"project_key\":\"payments-api\",\"test_type\":\"e2e\",
                 \"context\":{\"branch\":\"$GITHUB_REF_NAME\",\"ci_provider\":\"github-actions\"}}" \
            | jq -r .id)
          echo "id=$RUN" >> "$GITHUB_OUTPUT"

      - name: Run the suite
        run: pytest --test-hub-run-id="${{ steps.run.outputs.id }}"

      - name: Close the run
        if: always()
        env:
          TEST_HUB_TOKEN: ${{ secrets.TEST_HUB_TOKEN }}
        run: |
          curl --fail-with-body -sS -X POST \
            "$TEST_HUB_URL/api/v1/runs/${{ steps.run.outputs.id }}/finish" \
            -H "Authorization: Bearer $TEST_HUB_TOKEN" \
            -H 'content-type: application/json' -d '{}'
```

The final status is folded from the results that actually arrived, so a
cancelled job that never reaches the close step still shows what it managed to
report — as a run that is still open, which is the truth.

## Attaching screenshots and traces

```shell
curl --fail-with-body -sS -X POST "$TEST_HUB_URL/api/v1/runs/$RUN/artifacts" \
  -H "Authorization: Bearer $TEST_HUB_TOKEN" \
  -F "file=@test-results/failure.png" \
  -F "kind=screenshot" \
  -F "test_id=$TEST_ID"
```

Artifacts are content-addressed, so fifty shards uploading the same screenshot
store one copy.

## Things that go wrong

| Symptom | Cause |
|---|---|
| Step is green, nothing in the dashboard | `curl` without `--fail-with-body`; it exits 0 on a 4xx. |
| Results only appear when tests pass | The publish step is missing `if: always()`. |
| Four runs instead of one | Shards did not share a `--run-id`. |
| `415 unknown_format` | The file is not JUnit XML or Test Hub JSON. `GET /api/v1/adapters` lists what this server accepts. |
| `401 unauthorized` | The secret is unset — a missing secret expands to an empty string rather than failing. |
| Every run says branch `HEAD` | On `pull_request`, `GITHUB_REF_NAME` is `<n>/merge`. Use `github.head_ref` for the source branch. |
