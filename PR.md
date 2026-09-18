This PR adds a proof-of-concept `linkcheck-diff` command to the docs Makefile.

How it works:

- Adds a Sphinx extension (`docs/_dev/linkcheck_diff.py`) activated via `-D linkcheck_diff=diff`.
- The extension discovers the base branch from standard `conf.py` config values (`html_context["github_url"]` and `html_context["repo_default_branch"]`) — it uses `git remote -v` to find which local remote points at `github_url`, then uses `<remote>/<repo_default_branch>` as the base ref. No remote name is assumed.
- It creates a temporary `git worktree` of the base branch, runs a collect-mode Sphinx build to discover all baseline URLs (with no HTTP requests), then adds those URLs to `linkcheck_ignore` so the current build only checks URLs that are new or changed.
- Also provides a standalone `make linkcheck-collect` command that discovers all URLs without making any HTTP requests (useful for CI caching).
- The full design is documented in `LINKCHECK_DIFF_DESIGN.md`.

In the final implementation, the extension would be a pip-installable package (e.g., `sphinx-linkcheck-diff`) listed in `requirements.txt`, rather than a single module loaded via `sys.path` insert. The extension code itself would be identical — only the packaging and loading mechanism differ. The PoC also uses `-c` to point the baseline build at the current branch's `conf.py`, which means docstring URL changes are not caught (both baseline and current builds use the current source code); this is documented as a known limitation and is an acceptable trade-off for eliminating the bootstrapping requirement.

Sample output:

```text
...
(explanation/security: line   22) -ignored- https://canonical.com/juju/docs/juju-cli/3.6/reference/juju-cli/#juju-cli
(explanation/security: line   68) -ignored- https://semver.org/
(explanation/security: line   94) -ignored- https://docs.zizmor.sh/
(           index: line   12) -ignored- https://canonical.com/juju
(           index: line   14) -ignored- https://github.com/canonical/operator/tree/main/examples
(           index: line   35) -ignored- https://github.com/canonical/jubilant/releases
(           index: line   43) -ignored- https://ubuntu.com/community/ethos/code-of-conduct
(           index: line   45) -ignored- https://matrix.to/#/#charmhub-charmdev:ubuntu.com
(tutorial/getting-started: line    5) -ignored- https://canonical.com/juju/docs/juju-cli/3.6/howto/manage-juju/#install-juju
(tutorial/getting-started: line  180) -ignored- https://github.com/canonical/jubilant/tree/main/tests/integration
... (59 URLs ignored — in the baseline)
(           index: line   12) ok        https://docs.python.org/3/
(           index: line   31) ok        https://www.diataxis.fr/
(           index: line   12) broken    https://example.com/this-page-does-not-exist - 404 Client Error: Not Found for url: https://example.com/this-page-does-not-exist
build finished with problems.
index.md:12: [broken] https://example.com/this-page-does-not-exist: 404 Client Error: Not Found for url: https://example.com/this-page-does-not-exist
```

The three checked URLs are the ones new or changed on this branch — everything else is ignored as part of the baseline. The broken link is a deliberate test link (the CI linkcheck step is commented out on this branch).
