# land action

Run only the tests a change can reach, on GitHub's own runners, with no account and no token.

```yaml
# .github/workflows/land.yml
name: land
on: [pull_request, push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5   # or whatever your tests need
        with: { python-version: "3.12" }
      - run: pip install -r requirements.txt
      - uses: Ataraxy-Labs/land-action@v1
        with:
          split: files      # one unit per test file
          run: |
            tests: pytest -q tests
```

See it on a real repository in [land-action-example](https://github.com/Ataraxy-Labs/land-action-example): [one pull request](https://github.com/Ataraxy-Labs/land-action-example/pull/3) edits the receipt code, so only the receipt tests run and the other two are carried, and [another](https://github.com/Ataraxy-Labs/land-action-example/pull/4) breaks a tax rate, so the pricing tests run and fail.

## How it works

1. On a green run on your main branch, land records every file each command reads, in the kernel, and keeps that
   record in your repository's Actions cache.
2. On a pull request, a command whose recorded files are all unchanged is carried from main without running. Any
   other command runs as usual.
3. The job summary lists every command, whether it ran or was carried, and why.

Pull requests from forks work, because nothing needs a secret. A pull request reads main's record and keeps its own,
but never writes main's, so only your own main branch decides what counts as green.

## The speedups, borrowed from land

Each of these comes from [land](https://useland.dev), the hosted version, and works on GitHub's free runners.

| | what it does | what you see |
|---|---|---|
| **Skip by recorded reads** | a command is carried only when every file, folder listing and installed package it read is byte for byte the same | no path filters to write or keep up to date |
| **Each pull request remembers** | a pull request keeps its own record beside main's, so the next push carries what the last push proved | an agent pushing fix after fix to one PR reruns only what each fix touched |
| **One unit per test file** | `split: files` turns one pytest, vitest or jest line into one command per test file, and `go test ./...` into one per package | edit one module and only the test files that read it run |
| **Rerun before blaming** | a command that fails beside others is rerun on its own before it is judged, and one that flips is marked nondeterministic and never carried | a flaky test shows up as flaky, not as your change breaking main |
| **Every core** | commands run in parallel, each in its own fresh home and temp folder | the job uses the whole runner without you sharding anything |

## When a command always runs

- There is no green run on main yet (the first run records).
- Any file it read changed, appeared or disappeared.
- It failed last time, or failed differently with the same files (it is never carried again).
- land's recorder could not see everything it did.

Without `split`, splitting a big suite into a few named lines (`unit:`, `api:`, …) carries more of it. A line that
already names its test files runs whole, as written.

## Inputs

| input | default | |
|---|---|---|
| `run` | required | test commands, one per line, optionally `name: command` |
| `main` | the default branch | the branch whose green runs record |
| `working-directory` | `.` | where the commands run |
| `split` | `none` | `files` runs each test file (pytest, vitest, jest) or Go package as its own command |

Linux runners only (`ubuntu-*`). Packages installed with `pip install --user` live in your home directory, which the
recorder does not see: use `actions/setup-python` or install system-wide.

## Learn more

[Speeding up CI, a practical guide](https://ataraxy-labs.github.io/land-action/) covers twelve ways to make GitHub Actions faster, with a snippet for each and which ones this action already does.

## Want more?

The same recorder powers [land](https://useland.dev): faster machines, environments built once and kept warm, and
merging several agents' branches with one test run. 500 minutes a month free.

## GitLab CI

The same recorder runs on GitLab.com's hosted Linux runners. In `.gitlab-ci.yml`:

```yaml
include:
  - remote: https://raw.githubusercontent.com/Ataraxy-Labs/land-action/v1/gitlab/land.gitlab-ci.yml

land:
  extends: .land
  image: python:3.12          # any Debian or Ubuntu image with your toolchain
  before_script:
    - pip install -r requirements-dev.txt
  variables:
    LAND_RUN: |
      core: python -m pytest -q tests/core
      api: python -m pytest -q tests/api
```

The record lives in GitLab's cache and only a green pipeline on the default branch writes it; merge request pipelines carry from it. Commands run as an ordinary user that owns the checkout, so install packages system-wide in `before_script`. The summary is saved with the job as `land-summary.md`. The job needs a privileged runner, which GitLab.com's hosted runners are; on your own runners set `privileged = true`.
