# Git flow

How a change gets from a branch to `main`, and from `main` to the server. The same scheme as poketrain.

## Branching

One long-lived branch, `main`. It is always deployable, and once Coolify is set up **every merge to `main` deploys** ([coolify.md](coolify.md)). Work happens on short-lived branches and reaches `main` only through a pull request.

```
  feat/xyz  ──PR + CI──▶  main  ──push──▶  Coolify builds the Dockerfile on the droplet
```

**Rules**
- Nobody pushes to `main`, including for a one-line fix. Branch, PR, merge. (GitHub already refuses direct pushes.)
- A PR merges only when the `check` job of [CI](../.github/workflows/ci.yml) passes.
- Merges are **squash** merges: `main` has one commit per PR and a revert is one click.
- Branch names: `type/short-description`, kebab-case, cut from `main`, same types as commits. Delete the branch after the merge.
- One PR does one thing.

## Daily flow

```bash
git switch main && git pull
git switch -c feat/my-change

# work, commit, push
.venv/bin/ruff check . && .venv/bin/pytest
git add <files>
git commit -m "feat: describe the change"
git push -u origin feat/my-change

gh pr create        # wait for the check, then squash and merge

git switch main && git pull --prune
git branch -d feat/my-change
```

A squash merge gives `main` a new commit that your local branch commits do not match. If `git pull` complains after you committed on local `main` by mistake, `git reset --hard origin/main` is safe once `git diff <your-branch> origin/main` is empty.

## Commit and PR conventions

Format: `type: short description`, at most 72 characters, imperative, lowercase after the colon, no period.

| Type | Use for |
|---|---|
| `feat` | new behaviour |
| `fix` | a bug fix |
| `refactor` | no behaviour change |
| `docs` | documentation only |
| `test` | adding or fixing tests |
| `chore` | dependencies, tooling, config, Docker |
| `ci` | the CI workflow |

- **The PR title is the commit on `main`** (squash adds `(#N)`). Write it in this format.
- The PR description uses the [template](../.github/pull_request_template.md): what and why, how it was checked, deploy notes (a migration, a Dockerfile change, a new env var).
- A PR with a migration says so: it runs on deploy while the old version may still be serving.
- PRs that touch orders, the engine or reconciliation say what was tried on the testnet and what was not.

## CI

[.github/workflows/ci.yml](../.github/workflows/ci.yml) runs on every PR to `main` and every push to `main`:

1. Starts a throwaway PostgreSQL 18 service (destroyed when the job ends).
2. `uv sync --frozen`, then `ruff check .`.
3. `alembic upgrade head` and `alembic check` against that database.
4. `pytest`. With `CI=true` the database tests **fail instead of skipping** if the database is missing.

The job has no Binance keys and no Telegram token, so it cannot send an order or an alert. It does not build the Docker image: Coolify does that, and a broken Dockerfile shows up there while the previous version keeps running. Build locally first for a risky Dockerfile change: `docker build -t tidewatch:test .`.

Coolify deploys on push to `main` and does not wait for CI. The ruleset below is what makes CI a gate.

## GitHub settings (one-time, by hand)

Settings → Rules → Rulesets → New branch ruleset on `main`, enforcement **Active**:

| Setting | Value |
|---|---|
| Restrict deletions | on |
| Block force pushes | on |
| Require a pull request before merging | on, **0** required approvals (a solo owner cannot approve their own PR) |
| Require conversation resolution | on |
| Require status checks to pass | on, add **`check`** (it appears in the list only after CI has run once on a PR) |
| Require branches to be up to date before merging | on |
| Require linear history | on |
| Bypass list | empty |

Settings → General → Pull Requests: allow squash merging **on**, merge commits and rebase **off**, default message "Pull request title and description", automatically delete head branches **on**, allow auto-merge on.

Settings → Code security: secret scanning with push protection on, Dependabot alerts on. Binance keys and the Telegram token live only in Coolify and the local `.env`.

The repository already has a "changes must go through a pull request" rule; the status-check requirement and the settings above are not confirmed to be set.
