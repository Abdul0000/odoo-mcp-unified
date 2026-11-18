"""Keep verification files and generated caches outside deployed Odoo addons."""
from pathlib import Path, PurePosixPath
from fastapi import HTTPException

CACHE_DIRS = {'__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', '.hypothesis'}
MANIFESTS = {'__manifest__.py', '__openerp__.py'}


def artifact_reason(relative, in_module=False):
    path = PurePosixPath(str(relative).replace('\\', '/'))
    if CACHE_DIRS.intersection(path.parts) or path.suffix in {'.pyc', '.pyo'} or path.name in {'.coverage', '.DS_Store'}:
        return 'Generated cache/artifact files must not be written or published'
    test_source = path.suffix in {'.py', '.js', '.ts', '.jsx', '.tsx'} and (
        path.name.startswith(('test_', 'qa_')) or path.stem.endswith(('_test', '_qa'))
        or '.test.' in path.name or '.spec.' in path.name)
    if in_module and ({'tests', 'test', 'qa'}.intersection(path.parts) or path.name == 'conftest.py'
                      or path.name.lower() in {'qa.md', 'qa_report.md'} or test_source):
        return 'Test files must not be generated inside Odoo modules; use existing tests or external temporary checks'
    return None


def validate_source_paths(root, paths, module_roots=()):
    root = Path(root).resolve()
    paths = [PurePosixPath(str(path).replace('\\', '/')) for path in paths]
    if any(path.is_absolute() or '..' in path.parts for path in paths):
        raise HTTPException(400, 'Source artifact paths must remain inside the repository')
    roots = [PurePosixPath(str(path)) for path in module_roots]
    roots += [path.parent for path in paths if path.name in MANIFESTS]
    for path in paths:
        in_module = any(path.is_relative_to(module) for module in roots)
        parent = (root / str(path)).parent
        while not in_module and parent.is_relative_to(root):
            in_module = any((parent / name).is_file() for name in MANIFESTS)
            if parent == root:
                break
            parent = parent.parent
        reason = artifact_reason(path, in_module)
        if reason:
            raise HTTPException(409, {'error_code': 'source_artifact_forbidden', 'path': str(path), 'message': reason,
                                      'recovery': 'Keep declared checks; run inline or in /tmp with caches outside source. Do not commit generated artifacts.'})


def validate_git_artifacts(root, args):
    # Use trusted Git control operations; never silently accept truncated paths.
    from .execution_service import git
    output = git(args, root)
    if len(output) >= 32000:
        raise HTTPException(409, 'Changed file list exceeds safe artifact review limit; split the change')
    validate_source_paths(root, [path for path in output.split('\0') if path])
