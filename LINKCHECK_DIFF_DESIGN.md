# Design: Restricted link checker

## Goal

A Sphinx extension that enables a "restricted" link check — checking only URLs that are new or changed relative to the base branch. The existing `make linkcheck` command remains unchanged and checks all links.

The extension is a general-purpose, pip-installable package. It works with any Sphinx project regardless of markup format (MyST, reStructuredText), extensions (autodoc, intersphinx, custom roles), or directory structure. It reads standard config values that any Canonical Sphinx Stack project already defines.

---

## User experience

### Commands

| Command | What it does |
|---------|-------------|
| `make linkcheck` | Unchanged. Checks every external link. |
| `make linkcheck-diff` | Checks only URLs that are new or changed relative to the base branch. Fully automatic — no branch switching or baseline management required. |
| `make linkcheck-collect` | Standalone utility: collects all URLs into a baseline `output.json` with no linkcheck HTTP requests. Useful for CI caching. |

### What `make linkcheck-diff` does

When you run `make linkcheck-diff`, the extension automatically:

1. Checks out the base branch into a temporary `git worktree` (your working tree is not disturbed).
2. Runs a collect-mode Sphinx build in that worktree — discovering all URLs via Sphinx's own `HyperlinkCollector`, with no linkcheck HTTP requests.
3. Reads the baseline URL set from that build's `output.json`.
4. Cleans up the worktree.
5. Adds every baseline URL to `linkcheck_ignore` for the current build.
6. The current build's linkcheck runs normally, checking only URLs not in the baseline.

You just run `make linkcheck-diff`. No branch switching, no stashing, no baseline freshness concerns.

### What "new or changed" means

A URL is checked if Sphinx discovers it in the current build but it does not appear in the baseline (the base branch's URL set). This catches:

- A brand-new link added to a doc.
- An existing link whose URL was changed (the old URL is in the baseline, the new URL is not).

Note: because the baseline build uses the current branch's `conf.py` (see the diff mode handler below), URLs in Python docstrings are read from the current source code in both the baseline and current builds. This means docstring URL changes are **not** caught by the diff — only URLs in doc source files (`*.md`, `*.rst`) are diffed against the base branch. See limitation #4 for details.

### What is not checked

- **Unchanged URLs** — those in the baseline. Assumed already verified by upstream CI or a prior `make linkcheck` run.
- **Intersphinx links** — these are resolved at build time from remote inventory files. Bad intersphinx references fail the normal Sphinx build, so linkcheck doesn't need to cover them. Both the baseline build and the diff build discover them the same way, so they appear in the baseline and are ignored in the diff.

### Behaviour when there's nothing to check

If there are no new URLs, `make linkcheck-diff` completes without making any linkcheck HTTP requests. (Sphinx still does a full build — parsing docs, running autodoc, resolving intersphinx — before the linkcheck phase runs, so this is not instant, but no linkcheck HTTP requests are made.)

### Fallback

If the base branch cannot be resolved (e.g., the upstream remote is not configured, or the base ref doesn't exist), the extension does nothing and `make linkcheck-diff` behaves identically to `make linkcheck` — checking all links. This is the safe default.

### Standalone collect mode

`make linkcheck-collect` runs a Sphinx linkcheck build where every URL is ignored (no linkcheck HTTP requests). Sphinx's `HyperlinkCollector` discovers all URLs and writes them to `output.json` with status `ignored`. This is useful in CI for caching: collect the baseline once, reuse it across multiple PR checks without re-building the base branch each time.

### Base branch discovery

The extension reads two standard config values from `conf.py`:

- `html_context["github_url"]` — the upstream repo URL (e.g., `https://github.com/canonical/jubilant`).
- `html_context["repo_default_branch"]` — the base branch name (e.g., `main`).

It uses `git remote -v` to find which local remote points at `github_url`, then uses `<remote>/<repo_default_branch>` as the base ref. This avoids assuming a specific remote name (`origin`, `upstream`, etc.) — the correct remote is discovered from `github_url`, which always points at the upstream repo.

---

## Technical implementation

### Packaging

The extension is delivered as a pip-installable Python package (e.g., `sphinx-linkcheck-diff`), following the same pattern as other Sphinx extensions. No `sys.path` manipulation needed — the package is installed into the docs venv and importable by name.

### Files to create

- The Python package (e.g., `sphinx_linkcheck_diff/`), containing the extension module with a `setup()` entry point.

### Files to modify (per project)

- `requirements.txt` — add the package dependency.
- `conf.py` — add the extension to the `extensions` list.
- `Makefile` — add `linkcheck-diff` and `linkcheck-collect` targets.

### Sphinx extension design

**Config value:**

```python
app.add_config_value("linkcheck_diff", "", "", (str,))
```

A single string config value with three states:

- `""` (default) — extension is dormant. `make linkcheck` and `make html` are unaffected.
- `"collect"` — collect mode. All URLs are ignored; `output.json` contains every discovered URL with status `ignored`.
- `"diff"` — diff mode. Automatically collects a baseline from the base branch (via `git worktree`), then checks only new URLs.

**Collect mode: `builder-inited` handler**

1. Checks `isinstance(app.builder, CheckExternalLinksBuilder)` — returns early if not.
2. Checks `app.builder.config["linkcheck_diff"] == "collect"` — returns early if not.
3. Appends a catch-all pattern (`.*`) to `app.builder.config["linkcheck_ignore"]`. This causes `HyperlinkAvailabilityChecker` to yield every URL as `ignored` with no linkcheck HTTP requests. Sphinx's `HyperlinkCollector` still discovers all URLs normally, and `output.json` is written with every URL and its `ignored` status.

**Diff mode: `builder-inited` handler**

1. Checks `isinstance(app.builder, CheckExternalLinksBuilder)` — returns early if not.
2. Checks `app.builder.config["linkcheck_diff"] == "diff"` — returns early if not.
3. Finds the git repo root by running `git rev-parse --show-toplevel` from the Sphinx source directory (`app.confdir`). This avoids assuming `conf.py` is at a fixed depth relative to the repo root.
4. Reads `github_url` and `repo_default_branch` from `app.builder.config["html_context"]`. Uses `git remote -v` (run from the repo root) to find which remote points at `github_url`, then forms the base ref as `<remote>/<repo_default_branch>`. If no matching remote exists or the ref doesn't resolve, returns early (full-check fallback).
5. Creates a temporary `git worktree` of the base ref: `git worktree add --detach <temp_dir> <base_ref>`.
6. Runs a collect-mode Sphinx build in the worktree: `sphinx-build -b linkcheck -D linkcheck_diff=collect -c <current_confdir> <worktree_docs_dir> <worktree_build_dir>`. The `-c` flag tells Sphinx to use the **current branch's `conf.py`** (which has the extension registered) while reading **source files from the worktree** (the base branch). This is essential: without `-c`, the baseline build would use the worktree's `conf.py`, which may not have the extension registered — in that case the `-D linkcheck_diff=collect` flag would be silently ignored and the baseline build would do a real linkcheck (slow, HTTP requests) instead of a collect. Using `-c` ensures the extension is always active in the baseline build, regardless of whether the base branch has it. The build reuses the current venv (the same `sphinx-build` binary and installed packages) — the worktree provides source files, the venv provides the Python environment. This assumes dependency compatibility between branches, which is almost always true in practice.
7. Reads the baseline `output.json` from the worktree build dir. Parses the JSONL (one JSON object per line, each with a `uri` field). Extracts the set of URLs.
8. Cleans up: `git worktree remove --force <temp_dir>`.
9. For each baseline URL, appends an exact-match pattern (`^<escaped_url>$`) to `app.builder.config["linkcheck_ignore"]`.

Sphinx's own `HyperlinkCollector` then discovers all current URLs during the current build. Any URL not in the baseline ignore list is, by definition, new, and gets checked normally.

**No other hooks.** No `build-finished`, no cache file, no persistent state. The extension is purely a pre-filter on `linkcheck_ignore`, driven by one config value.

### Makefile targets

```makefile
linkcheck-collect: install
	. $(DOCS_VENV) ; $(SPHINX_BUILD) -b linkcheck -q "$(DOCS_SOURCEDIR)" "$(DOCS_BUILDDIR)" $(SPHINX_OPTS) -D linkcheck_diff=collect || { grep --color -F "[broken]" "$(DOCS_BUILDDIR)/output.txt"; exit 1; }
	exit 0

linkcheck-diff: install
	. $(DOCS_VENV) ; $(SPHINX_BUILD) -b linkcheck -q "$(DOCS_SOURCEDIR)" "$(DOCS_BUILDDIR)" $(SPHINX_OPTS) -D linkcheck_diff=diff || { grep --color -F "[broken]" "$(DOCS_BUILDDIR)/output.txt"; exit 1; }
	exit 0
```

`linkcheck-collect` is identical to `linkcheck` but with `-D linkcheck_diff=collect`.

`linkcheck-diff` is identical to `linkcheck` but with `-D linkcheck_diff=diff`. The extension handles the baseline collection internally — no extra Makefile variables or user steps.

### Interaction with existing `linkcheck_ignore`

The existing `linkcheck_ignore` entries in `conf.py` are preserved. The extension **appends** to the list, never replaces. In collect mode, the catch-all `.*` pattern is appended alongside existing entries — existing patterns are redundant but harmless. In diff mode, the baseline URL patterns are appended — user-configured ignores always take precedence.

### Why this design balances the two goals

**No burden for the user:** `make linkcheck-diff` is a single command. The extension handles branch discovery, worktree creation, baseline collection, and cleanup automatically. No branch switching, no stashing, no baseline file management.

**Generic across projects:** The extension reads `github_url` and `repo_default_branch` from `html_context` — standard config values that any Canonical Sphinx Stack project already defines. URL discovery uses Sphinx's own `HyperlinkCollector`, which handles all markup formats, extensions, and custom roles. No grep, no hardcoded paths, no project-specific assumptions.

### Known limitations

1. **No re-checking of existing links.** A URL that was fine when the baseline was collected but later rots will not be caught by `make linkcheck-diff`. Use `make linkcheck` for a full check.
2. **Two Sphinx builds in diff mode.** The baseline collection is a full Sphinx build (parsing, autodoc, intersphinx) — not instant. But it makes no linkcheck HTTP requests, so it's much faster than a real linkcheck. The standalone collect mode can be used to cache the baseline in CI.
3. **Requires the upstream remote.** Diff mode needs a local remote pointing at the upstream repo (discoverable via `github_url`). If the remote doesn't exist (e.g., shallow clone without it), diff mode falls back to a full check. The remote is discovered by URL, not name — both HTTPS (`https://github.com/owner/repo`) and SSH (`git@github.com:owner/repo`) forms are matched against `github_url`.
4. **Docstring URL changes not caught.** Because the baseline build uses `-c` to load the current branch's `conf.py` (see step 6 above), autodoc in the baseline build reads docstrings from the **current** source code, not the base branch's. This means a URL added or changed in a Python docstring will appear in both the baseline and the current build, so it will be ignored in the diff. Only URLs in doc source files (`*.md`, `*.rst`) are diffed against the base branch. This is an acceptable trade-off: it eliminates the bootstrapping requirement (the extension does not need to exist in the base branch), and docstring URL changes are rare relative to doc URL changes.
5. **Nested sphinx-build.** Diff mode runs a `sphinx-build` subprocess inside a `builder-inited` handler (which is itself inside a `sphinx-build` process). The baseline build's output should be suppressed or clearly labelled to avoid confusing interleaved output. If the baseline build fails (e.g., missing dependencies, broken source files on the base branch), the extension should report the failure clearly and fall back to a full check. Note: since the baseline build uses the current branch's `conf.py` (via `-c`), a broken `conf.py` on the base branch is not a failure mode.

### Testing plan

1. **Collect mode:** Run `make linkcheck-collect`, verify `output.json` contains all URLs with status `ignored`.
2. **Diff mode, no new URLs:** Run `make linkcheck-diff` with no changes since the base branch. Verify no linkcheck HTTP requests are made.
3. **Diff mode, new URL:** Add a link to a doc, run `make linkcheck-diff`. Verify only the new URL is checked.
4. **Diff mode, changed URL:** Modify an existing link's URL, run `make linkcheck-diff`. Verify the new URL is checked (the old URL is in the baseline, the new one is not).
5. **No upstream remote:** Remove the upstream remote entirely (not just rename it — the extension discovers the remote by URL, not name), verify `make linkcheck-diff` falls back to full check.
6. **`make linkcheck` unchanged:** Verify `make linkcheck` still checks all links and is unaffected by the extension.
7. **`make html` unaffected:** Verify the extension is dormant during HTML builds.
8. **Existing `linkcheck_ignore` preserved:** Verify user-configured ignores are still respected in both collect and diff modes.
9. **Worktree cleanup:** Run `make linkcheck-diff`, verify the temporary worktree is removed after the build.

---

## Proof-of-concept implementation

Before packaging the extension as a standalone pip-installable package, we'll implement a proof-of-concept (PoC) in the Jubilant repo to validate the design end-to-end.

### How the extension is loaded in the PoC

In the PoC, the extension lives as a single Python module in `docs/_dev/linkcheck_diff.py` (not a pip package). It is loaded via a `sys.path` insert in `conf.py`:

```python
sys.path.insert(0, str(pathlib.Path(__file__).parent / "_dev"))
```

And added to the `extensions` list in `conf.py`:

```python
extensions = [
    ...
    "linkcheck_diff",
]
```

The Makefile targets (`linkcheck-collect`, `linkcheck-diff`) are added to the existing `docs/Makefile` as described in the design above.

### What differs in the final implementation

| Aspect | PoC | Final |
|--------|-----|-------|
| **Packaging** | Single module in `docs/_dev/linkcheck_diff.py`, loaded via `sys.path` insert. | Pip-installable package (e.g., `sphinx-linkcheck-diff`), listed in `requirements.txt`. |
| **Loading** | `sys.path.insert` + `"linkcheck_diff"` in `extensions`. | Just `"sphinx_linkcheck_diff"` in `extensions` — no `sys.path` manipulation. |
| **Distribution** | Lives in the Jubilant repo only. | Published to PyPI (or a Canonical package index), usable by any Sphinx project. |
| **`conf.py` changes** | `sys.path` insert + extension name in `extensions`. | Extension name in `extensions` only. |
| **Scope** | Validated against Jubilant's docs only. | Tested across multiple project structures, markup formats, and extension combinations. |

The PoC validates the core mechanism — the `builder-inited` handler, collect/diff modes, worktree-based baseline collection, and `linkcheck_ignore` manipulation — using Jubilant's actual docs. The extension code itself is identical in both; only the packaging and loading mechanism differ.

### PoC validation results

All 9 tests from the testing plan pass. Key findings from the PoC:

- **The `-c` flag is essential.** Without it, the baseline build uses the worktree's `conf.py`, which doesn't have the extension registered. The `-D linkcheck_diff=collect` flag is then silently ignored, and the baseline build does a real linkcheck (slow, HTTP requests) instead of a collect. Using `-c <current_confdir>` ensures the extension is always active in the baseline build. This eliminates the original bootstrapping requirement (the extension does not need to exist in the base branch first).
- **Remote discovery matches on URL, not name.** The extension finds the upstream remote by matching its fetch/push URL against `github_url` from `html_context`. Both HTTPS (`https://github.com/owner/repo`) and SSH (`git@github.com:owner/repo`) remote URLs are matched. This means renaming a remote does not affect discovery — only removing it (or changing its URL) triggers the fallback.
- **`Config.__getitem__` returns the live list.** Sphinx's `Config.__getitem__` returns `getattr(self, name)`, so `app.builder.config['linkcheck_ignore']` returns the actual list object. Appending to it in the `builder-inited` handler modifies the config in place, and `HyperlinkAvailabilityChecker` (which compiles the patterns later, in `finish()`) picks up the appended entries. This is why the extension only needs a `builder-inited` handler — no `build-finished` or other hooks.
