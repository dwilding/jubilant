# Design: Restricted link checker

## Goal

Add a restricted link check to any Sphinx project: only URLs that are new or changed relative to the base branch are checked. The existing `make linkcheck` command is unchanged and checks all links.

The tool ships as a pip-installable package. It reads standard config values that any Canonical Sphinx Stack project already defines.

## User experience

### Commands

| Command | What it does |
|---|---|
| `make linkcheck` | Unchanged. Checks every external link. |
| `make linkcheck-diff` | Checks only URLs that are new or changed relative to the base branch. Collects a baseline automatically on first use for a given base SHA. |

### What `make linkcheck-diff` does

The make target runs two phases in sequence:

1. **Ensure baseline.** The collector CLI resolves the head SHA of the base branch and makes sure a cached baseline exists for that SHA. The first run for a SHA performs an isolated build of the base branch to collect its URLs. Subsequent runs reuse the cache. The CLI prints the path of the baseline file.
2. **Run the linkcheck build.** A normal `sphinx-build -b linkcheck` run, plus `-D linkcheck_diff_baseline=<path>`. The extension adds every baseline URL to `linkcheck_ignore`, so only URLs outside the baseline are checked.

If there are no new URLs, the command makes no linkcheck HTTP requests. Sphinx still parses the docs and resolves intersphinx before the linkcheck phase runs.

### What "new or changed" means

A URL is checked if the current build discovers it and the baseline does not contain it. This catches:

- A brand-new link added to a doc or a Python docstring.
- An existing link whose URL was changed. The old URL is in the baseline, the new one is not.

The baseline build runs the base branch's own `conf.py` and source code in an isolated environment, so URLs in docstrings are diffed the same as URLs in doc files.

### What is not checked

- **Unchanged URLs** — those in the baseline. They exist on the base branch, so they are not new.
- **Intersphinx links** — these are resolved at build time from remote inventory files, and the resolved URLs are discovered like any other URL, so they appear in the baseline and are ignored in the diff. An unresolvable intersphinx reference is a Sphinx warning, not a linkcheck result.

### Failure behavior

If a step fails, `make linkcheck-diff` fails with a clear error. It never silently degrades to a full check.

- **The fetch fails** (for example, you are offline): the collector prints a warning and continues with the SHA from the last successful fetch. A baseline cached for that SHA is still used.
- **The base branch cannot be resolved** (no remote matches `github_url`, or the ref was never fetched): the collector errors out.
- **The baseline collection build fails** (broken `conf.py` on the base branch, dependencies that no longer install): the error propagates. There are no retries or workarounds.
- **The base branch contains broken links**: not a failure. See "Success criterion" below.

## Architecture

| Component | Lives in | Purpose |
|---|---|---|
| **Collector CLI** | Console script from the `sphinx-linkcheck-diff` package | Resolve the base head SHA and produce a cached baseline of the base branch's URLs. All git, worktree, and venv logic lives here. |
| **Baseline cache** | `docs/_dev/.linkcheck-diff/baselines/` | SHA-keyed baseline files. A cache hit is a file existence check. |
| **Baseline filter (Sphinx extension)** | Same package as the collector; registered in `conf.py` | Given a baseline path, append its URLs to `linkcheck_ignore`. No git, no subprocesses, no state. |

The Makefile orchestrates the two phases.

Principles behind the split:

- The collector owns everything slow and environment-dependent: network, worktrees, venvs. The extension does nothing but filter a list.
- A cache entry always matches the SHA in its file name, so a baseline can never be silently wrong for the base it claims. If the base moves, the new SHA misses and the collector runs again.
- Everything outside the cache directory is temporary. The worktree, venv, and build directory are removed after collection.

## Baseline collector

The CLI has one command, `ensure-baseline`, run from the docs directory. It prints the path of a cached baseline for the current base head SHA, collecting one first if needed. It resolves the docs directory and cache location from its working directory, so no arguments are required in the standard setup. An optional `--docs-dir` overrides the working directory, for tests.

1. Read `github_url` and `repo_default_branch` from `html_context` in the docs `conf.py`. The collector executes `conf.py` in a fresh namespace to get them — the same file Sphinx executes on every build.
2. Find the remote: run `git remote -v` from the repo root and match remote URLs against `github_url`. Both HTTPS (`https://github.com/owner/repo`) and SSH (`git@github.com:owner/repo`) forms are matched, so no remote name is assumed.
3. Fetch the remote with `git fetch <remote>`, which updates the remote-tracking ref. Run the fetch with a timeout of about 30 seconds. DNS failures exit immediately with a clear message, but an unreachable host can block for minutes, and git's own HTTP timeout settings don't cover the TCP connect phase. If the fetch fails or times out, print a warning and continue with the tracking ref from the last successful fetch.
4. Resolve the base head SHA from the remote-tracking ref `<remote>/<repo_default_branch>`. If the ref can't be resolved, error out.
5. If the cache holds a baseline for this SHA, print its path and exit.
6. Otherwise, collect one (below), write it to the cache, print its path, and exit.

### Isolated baseline collection

The collection build is isolated from both your working tree and the current venv:

1. Create a temporary `git worktree` of the base head SHA. Your working tree is not touched.
2. Create a fresh venv and install the worktree's own `docs/requirements.txt` into it.
3. Run a collect build with stock Sphinx — nothing needs to be installed or configured on the base branch for this:

   ```
   sphinx-build -b linkcheck -D linkcheck_ignore=.* <worktree docs dir> <temp build dir>
   ```

   `linkcheck_ignore` takes regex patterns, and `.*` matches every URL. Sphinx's `HyperlinkCollector` still discovers every URL and writes `output.json`, but the checker reports all of them as `ignored`, so the build makes no linkcheck HTTP requests. The `-D` override replaces the base branch's own `linkcheck_ignore`, which is harmless because `.*` matches everything.
4. Parse `output.json` (JSONL, one object per line) and extract the `uri` fields.
5. Remove the worktree, the venv, and the build directory.

The venv install dominates the collection time. The Sphinx build itself is a small fraction of it.

The success criterion is `output.json` existing, not the exit code. Sphinx writes all results to `output.json` before setting its failure exit code, so a base branch with broken links still produces a complete URL set. Harder failures (a bad `conf.py`, missing dependencies) produce no `output.json` at all, and the collector crashes with the build's error output.

## Baseline cache

Baselines live at `docs/_dev/.linkcheck-diff/baselines/<sha>.json`, one file per base SHA. The `_dev` directory already holds regenerable build caches such as `.doctrees`, and this follows that convention. Add `_dev/.linkcheck-diff/` to `docs/.gitignore`.

Neither `make clean-doc` nor `make clean` removes the directory. Deleting the directory by hand is always safe — the next diff recollects.

Each baseline records its own SHA and collection time:

```json
{
  "base_sha": "a1b2c3d...",
  "collected_at": "2026-09-18T14:03:11+08:00",
  "uris": ["https://example.com/docs", "..."]
}
```

The SHA makes every entry self-describing: the file name, the `base_sha` field, and the base branch state always agree. The URIs are sorted, so rewriting a baseline for the same SHA always produces the same bytes.

## Sphinx extension (baseline filter)

```python
app.add_config_value("linkcheck_diff_baseline", "", "linkcheck", (str,))
```

The rebuild domain is `linkcheck`, not `env`. Sphinx re-reads every doctree when an `env`-domain value changes, and this value alternates between empty (`make html`) and a path (`make linkcheck-diff`) — an `env` domain would force a full re-parse on each switch between the two commands. A `linkcheck`-domain change leaves the doctree cache untouched.

The `builder-inited` handler:

1. If the builder is not `CheckExternalLinksBuilder`, return.
2. If `linkcheck_diff_baseline` is empty, return. The extension is dormant, so `make linkcheck` and `make html` are unaffected.
3. Load the baseline JSON. If the path doesn't exist, the file isn't valid JSON, or the JSON lacks a `uris` list, fail the build with an error naming the path. The make target guarantees a valid baseline before invoking Sphinx; this check catches hand-written invocations.
4. Append `^<re.escape(uri)>$` to `linkcheck_ignore` for each URI in the baseline.

Existing `linkcheck_ignore` entries in `conf.py` are preserved — the extension appends, never replaces.

Appending in `builder-inited` is sufficient: `Config.__getitem__` returns the live list object, and `HyperlinkAvailabilityChecker` compiles the patterns later, in `finish()`. No other hooks are needed.

## Makefile target

```makefile
linkcheck-diff: install
	@BASELINE=$$($(DOCS_VENVDIR)/bin/sphinx-linkcheck-diff ensure-baseline) || exit 1; \
	. $(DOCS_VENV); $(SPHINX_BUILD) -b linkcheck "$(DOCS_SOURCEDIR)" "$(DOCS_BUILDDIR)" $(SPHINX_OPTS) -D linkcheck_diff_baseline="$$BASELINE" || { grep --color -F "[broken]" "$(DOCS_BUILDDIR)/output.txt"; exit 1; }
```

The target differs from `linkcheck` only in the ensure-baseline step and the `-D` flag. It does not pass `-q`: the per-URL output is the point of the command. The recipe is one shell invocation (the backslashes continue the line), so `$$BASELINE` carries the collector's stdout — the baseline path — into the `-D` argument, and `|| exit 1` propagates a collector failure instead of running a full check.

## Implementation and packaging

The package is `sphinx-linkcheck-diff`, listed in `docs/requirements.txt`. It ships the collector as a console script and the extension, which registers as `sphinx_linkcheck_diff`.

To adopt it in a project:

1. Add `sphinx-linkcheck-diff` to `docs/requirements.txt`.
2. Add `sphinx_linkcheck_diff` to the `extensions` list in `conf.py`.
3. Add the `linkcheck-diff` target to the `Makefile`, as shown above.

## Known limitations

1. **No re-checking existing links.** A URL that was fine when the baseline was collected but later rots is not caught. Use `make linkcheck` for a full check.
2. **The first diff for a base SHA is slow.** Collection is a venv install plus a full Sphinx build. Subsequent diffs reuse the cache.
3. **Requires an upstream remote and one successful fetch.** Without a remote matching `github_url`, or before the first fetch, the base SHA can't be resolved and the command errors. Here "upstream" means the repo the docs describe, discovered via `github_url` — which may be your fork.

## Future work

- **Cache eviction.** The cache keeps one file per base SHA forever. Prune to the newest N entries plus the current SHA.
- **CI caching.** Cache `docs/_dev/.linkcheck-diff/` in CI, keyed on the base SHA, so PR checks skip collection entirely.

## Testing plan

The package ships a pytest suite with unit tests for the extension and integration tests for the collector and the end-to-end flow. The integration tests use a fixture repository with a known base branch, so they don't depend on any real remote. Two fixture details make this work:

- **The fixture docs use reST.** The isolated baseline build installs only what the fixture `requirements.txt` lists, and the fixture installs just this package — so the baseline venv has stock Sphinx, which doesn't parse Markdown.
- **The fixture remote is a local bare repository, and the fixture's `github_url` is its path.** The collector's URL matching must normalize the `.git` suffix on both sides for this to resolve.

The fixture URIs point at example.com paths that don't exist, so a checked URI ends up `broken`. That distinguishes "checked" from "ignored" without needing a link that responds; the build's exit code is irrelevant.

**Extension (unit tests):**

1. Set `linkcheck_diff_baseline` to a fixture file and run a linkcheck build. Every fixture URI is ignored, all others are checked.
2. A URI matching the project's own `linkcheck_ignore` stays ignored alongside the baseline.
3. Point `linkcheck_diff_baseline` at a missing file. The build fails with an error naming the path.
4. Point it at a file that isn't valid JSON, and at one without a `uris` list. Both fail the build.
5. With `linkcheck_diff_baseline` unset, no URI is ignored.

**Collector (integration tests, against the fixture repository):**

6. `ensure-baseline` collects the base branch's URI set.
7. A second run returns the same path without recollecting.
8. The baseline records the base SHA (matching `git rev-parse origin/main`) and a collection time.
9. A `github_url` with no matching local remote errors.
10. An unreachable remote (the fetch fails fast) prints a warning and collects from the last-fetched head.
11. The console script prints the baseline path and exits zero.
12. After collection, no worktree remains (`git worktree list`).
13. Collecting after edits to the working-tree docs still yields the base branch's URI set — the baseline reflects the base, not the working tree.

**End to end (integration tests, against the fixture repository):**

14. With no doc changes since the base branch, every URI is ignored.
15. A URL added on the branch is checked; base URLs stay ignored.
16. A base branch with a broken link still produces a complete baseline.
