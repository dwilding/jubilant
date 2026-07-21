"""Sphinx extension for restricted (diff-based) link checking.

This extension adds a single config value, ``linkcheck_diff``, with three
states:

- ``""`` (default) -- the extension is dormant. ``make linkcheck`` and
  ``make html`` are unaffected.
- ``"collect"`` -- collect mode. Every URL is ignored (no linkcheck HTTP
  requests). ``output.json`` contains every discovered URL with status
  ``ignored``.
- ``"diff"`` -- diff mode. Collects a baseline URL set from the base branch
  (via a temporary ``git worktree``), then adds every baseline URL to
  ``linkcheck_ignore`` so that only new or changed URLs are checked.

See ``LINKCHECK_DIFF_DESIGN.md`` for the full design.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from sphinx.builders.linkcheck import CheckExternalLinksBuilder

if TYPE_CHECKING:
    from sphinx.application import Sphinx
    from sphinx.util.typing import ExtensionMetadata

logger = logging.getLogger(__name__)

# Label used to prefix log messages from this extension, so that the nested
# baseline build's output is clearly distinguishable from the current build.
_LOG_PREFIX = '[linkcheck-diff]'


def _on_builder_inited(app: Sphinx) -> None:
    """``builder-inited`` handler: pre-filter ``linkcheck_ignore``."""
    if not isinstance(app.builder, CheckExternalLinksBuilder):
        return

    mode = app.builder.config['linkcheck_diff']
    if mode == 'collect':
        _setup_collect_mode(app)
    elif mode == 'diff':
        _setup_diff_mode(app)
    # Any other value (including "") -> dormant; do nothing.


def _setup_collect_mode(app: Sphinx) -> None:
    """Append a catch-all pattern so every URL is ignored.

    Sphinx's ``HyperlinkCollector`` still discovers all URLs normally, and
    ``output.json`` is written with every URL and its ``ignored`` status.
    No linkcheck HTTP requests are made.
    """
    app.builder.config['linkcheck_ignore'].append('.*')
    logger.info('%s collect mode: ignoring all URLs (no HTTP requests)', _LOG_PREFIX)


def _setup_diff_mode(app: Sphinx) -> None:
    """Collect a baseline from the base branch, then ignore those URLs.

    Falls back to a full check (does nothing) if the base branch cannot be
    resolved or the baseline collection fails.
    """
    repo_root = _find_repo_root(app.confdir)
    if repo_root is None:
        logger.warning(
            '%s could not find git repo root; falling back to full check',
            _LOG_PREFIX,
        )
        return

    base_ref = _resolve_base_ref(app, repo_root)
    if base_ref is None:
        logger.warning(
            '%s could not resolve base branch; falling back to full check',
            _LOG_PREFIX,
        )
        return

    baseline_urls = _collect_baseline(app, repo_root, base_ref)
    if baseline_urls is None:
        logger.warning(
            '%s baseline collection failed; falling back to full check',
            _LOG_PREFIX,
        )
        return

    # Append an exact-match pattern for each baseline URL so that only URLs
    # not in the baseline are checked. Existing ``linkcheck_ignore`` entries
    # are preserved (we append, never replace).
    ignore = app.builder.config['linkcheck_ignore']
    for url in baseline_urls:
        ignore.append('^' + re.escape(url) + '$')

    logger.info(
        '%s diff mode: ignoring %d baseline URLs from %s',
        _LOG_PREFIX,
        len(baseline_urls),
        base_ref,
    )


def _find_repo_root(confdir: str) -> Path | None:
    """Find the git repo root by running ``git rev-parse --show-toplevel``."""
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'],
            cwd=confdir,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return Path(result.stdout.strip())


def _resolve_base_ref(app: Sphinx, repo_root: Path) -> str | None:
    """Resolve the base ref as ``<remote>/<repo_default_branch>``.

    Reads ``github_url`` and ``repo_default_branch`` from ``html_context``,
    then finds which local remote points at ``github_url`` via
    ``git remote -v``.
    """
    html_context = app.builder.config['html_context']
    github_url = html_context.get('github_url', '')
    repo_default_branch = html_context.get('repo_default_branch', '')
    if not github_url or not repo_default_branch:
        return None

    remote_name = _find_remote_for_url(repo_root, github_url)
    if remote_name is None:
        return None

    base_ref = f'{remote_name}/{repo_default_branch}'
    # Verify the ref resolves.
    try:
        subprocess.run(
            ['git', 'rev-parse', '--verify', base_ref],
            cwd=repo_root,
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return None
    return base_ref


def _find_remote_for_url(repo_root: Path, github_url: str) -> str | None:
    """Find the local remote name whose fetch/push URL points at ``github_url``."""
    try:
        result = subprocess.run(
            ['git', 'remote', '-v'],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

    # Output format:
    #   <name>\t<url> (fetch)
    #   <name>\t<url> (push)
    for line in result.stdout.splitlines():
        parts = line.split('\t')
        if len(parts) < 2:
            continue
        name = parts[0]
        url = parts[1].split(' ')[0]
        if _urls_match(url, github_url):
            return name
    return None


def _urls_match(remote_url: str, github_url: str) -> bool:
    """Check if a remote URL points at the same repo as ``github_url``.

    Handles both HTTPS and SSH remote URLs for the same GitHub repo.
    """
    # Normalise the github_url (HTTPS).
    gh = github_url.rstrip('/')
    # HTTPS form: https://github.com/owner/repo
    https_form = gh
    # SSH form: git@github.com:owner/repo
    ssh_form = gh.replace('https://github.com/', 'git@github.com:')

    remote_url = remote_url.rstrip('/')
    if remote_url.endswith('.git'):
        remote_url = remote_url[:-4]

    return remote_url in (https_form, ssh_form)


def _collect_baseline(app: Sphinx, repo_root: Path, base_ref: str) -> set[str] | None:
    """Create a worktree of ``base_ref`` and run a collect-mode build in it.

    Returns the set of baseline URLs, or ``None`` if collection failed.
    """
    worktree_dir = Path(tempfile.mkdtemp(prefix='linkcheck-diff-'))
    try:
        # Create a worktree of the base ref (detached HEAD).
        try:
            subprocess.run(
                ['git', 'worktree', 'add', '--detach', str(worktree_dir), base_ref],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            logger.warning(
                '%s could not create worktree for %s: %s',
                _LOG_PREFIX,
                base_ref,
                exc.stderr.strip(),
            )
            return None

        # The docs source dir in the worktree is relative to the repo root.
        # app.confdir is the current build's confdir (e.g. <repo>/docs); we
        # need the corresponding dir in the worktree.
        current_confdir = Path(app.confdir)
        rel_confdir = current_confdir.relative_to(repo_root)
        worktree_confdir = worktree_dir / rel_confdir

        if not worktree_confdir.is_dir():
            logger.warning(
                '%s docs dir %s not found in %s; falling back to full check',
                _LOG_PREFIX,
                rel_confdir,
                base_ref,
            )
            return None

        worktree_builddir = worktree_dir / '_build'

        # Run a collect-mode Sphinx build in the worktree. Reuse the current
        # venv (same sphinx-build binary and installed packages).
        sphinx_build = _find_sphinx_build()
        if sphinx_build is None:
            logger.warning(
                '%s could not locate sphinx-build; falling back to full check',
                _LOG_PREFIX,
            )
            return None

        logger.info(
            '%s collecting baseline URLs from %s ...',
            _LOG_PREFIX,
            base_ref,
        )
        result = subprocess.run(
            [
                sphinx_build,
                '-b', 'linkcheck',
                '-q',
                '-D', 'linkcheck_diff=collect',
                str(worktree_confdir),
                str(worktree_builddir),
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning(
                '%s baseline build failed (rc=%d); falling back to full check.\n'
                'stderr:\n%s',
                _LOG_PREFIX,
                result.returncode,
                result.stderr,
            )
            return None

        output_json = worktree_builddir / 'output.json'
        if not output_json.is_file():
            logger.warning(
                '%s baseline build produced no output.json; falling back to full check',
                _LOG_PREFIX,
            )
            return None

        urls: set[str] = set()
        with open(output_json, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                uri = entry.get('uri')
                if uri:
                    urls.add(uri)
        return urls
    finally:
        # Clean up the worktree regardless of success or failure.
        subprocess.run(
            ['git', 'worktree', 'remove', '--force', str(worktree_dir)],
            cwd=repo_root,
            capture_output=True,
        )
        # Remove the temp parent dir if it's now empty.
        with contextlib.suppress(OSError):
            worktree_dir.rmdir()


def _find_sphinx_build() -> str | None:
    """Find the sphinx-build executable to use for the baseline build.

    Prefer the same interpreter's sphinx-build so the worktree build reuses
    the current venv.
    """
    # Same venv as the current process.
    candidate = Path(sys.executable).parent / 'sphinx-build'
    if candidate.is_file():
        return str(candidate)
    # Fall back to PATH.
    found = shutil.which('sphinx-build')
    return found


def setup(app: Sphinx) -> ExtensionMetadata:
    """Sphinx extension entry point."""
    app.add_config_value('linkcheck_diff', '', '', (str,))
    app.connect('builder-inited', _on_builder_inited)
    return {
        'version': '0.1.0',
        'parallel_read_safe': True,
        'parallel_write_safe': True,
    }
