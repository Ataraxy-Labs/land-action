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
          run: |
            unit: pytest -q tests/unit
            api: pytest -q tests/api
```

## How it works

1. On a green run on your main branch, land records every file each command reads, in the kernel, and keeps that
   record in your repository's Actions cache.
2. On a pull request, a command whose recorded files are all unchanged is carried from main without running. Any
   other command runs as usual.
3. The job summary lists every command, whether it ran or was carried, and why.

Pull requests from forks work, because nothing needs a secret. A pull request can read main's record but never write
it, so only your own main branch decides what counts as green.

## When a command always runs

- There is no green run on main yet (the first run records).
- Any file it read on main changed, appeared or disappeared.
- It failed on main, or failed differently with the same files (it is never carried again).
- land's recorder could not see everything it did.

Split a big suite into a few named lines (`unit:`, `api:`, …) to carry more of it.

## Inputs

| input | default | |
|---|---|---|
| `run` | required | test commands, one per line, optionally `name: command` |
| `main` | the default branch | the branch whose green runs record |
| `working-directory` | `.` | where the commands run |

Linux runners only (`ubuntu-*`). Packages installed with `pip install --user` live in your home directory, which the
recorder does not see: use `actions/setup-python` or install system-wide.

## Want more?

The same recorder powers [land](https://useland.dev): faster machines, environments built once and kept warm, and
merging several agents' branches with one test run. 500 minutes a month free.
