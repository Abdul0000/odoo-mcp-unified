"""Inspect test scope and imports without importing application packages."""
import ast
import re
from pathlib import Path


def addon_root(root, name):
    parent = Path(name).parent
    for parent in (parent, *parent.parents):
        if any((root / parent / manifest).is_file() for manifest in ('__manifest__.py', '__openerp__.py')):
            return parent.as_posix()
    return None


def imports_odoo(path):
    if not path.is_file() or path.stat().st_size > 500000:
        return False
    try:
        tree = ast.parse(path.read_text())
    except (OSError, UnicodeError, SyntaxError):
        return False
    return any((isinstance(node, ast.Import) and any(a.name.split('.')[0] in {'odoo', 'openerp'} for a in node.names))
               or (isinstance(node, ast.ImportFrom) and (node.module or '').split('.')[0] in {'odoo', 'openerp'})
               for node in ast.walk(tree))


def same_acceptance_command(old, new, workspace_root=None):
    """A runtime adapter may change Python, never the assertions or selectors."""
    def trusted_original(value):
        path = Path(value)
        if not re.fullmatch(r"python(?:[23](?:\.\d+)?)?", path.name):
            return False
        if value == path.name:
            return True
        # Existing absolute system interpreters may be retained, but no path
        # inside the task tree can masquerade as Python, even on exact reuse.
        if not path.is_absolute():
            return False
        return not (workspace_root and path.resolve().is_relative_to(Path(workspace_root).resolve()))

    if not old or not new or not trusted_original(old[0]):
        return False
    if old == new:
        return True
    # A replacement must resolve through PATH. Reject ./python, workspace
    # scripts and arbitrary absolute shims while preserving every other arg.
    return new[0] == Path(new[0]).name and trusted_original(new[0]) and old[1:] == new[1:]


def test_targets(argv):
    """Parse collection arguments without treating option values as test paths.

    Unknown plugin options conservatively retain default collection. No test code
    is imported on the API host to discover runner options.
    """
    executable = Path(argv[0]).name
    if executable in {"pytest", "py.test"}:
        runner, args = "pytest", argv[1:]
    elif re.fullmatch(r"python(?:[23](?:\.\d+)?)?", executable):
        index = 1
        while index < len(argv) and argv[index].startswith("-") and argv[index] not in {"-m", "-c", "--"}:
            index += 2 if argv[index] in {"-W", "-X"} else 1
        if index + 1 >= len(argv) or argv[index] != "-m" or argv[index + 1] not in {"pytest", "unittest"}:
            return None, []
        runner, args = argv[index + 1], argv[index + 2:]
    else:
        return None, []
    values = {"-k", "-m", "-c", "-o", "-r", "--report", "--override-ini", "--rootdir", "--confcutdir",
              "--basetemp", "--import-mode", "--junitxml", "--junit-xml", "--tb", "--color",
              "--capture", "--maxfail", "--durations", "--durations-min", "--ignore",
              "--ignore-glob", "--deselect", "--log-level", "--log-file", "--log-file-level",
              "--log-cli-level", "--log-format", "--log-date-format", "--log-file-format", "--log-file-date-format",
              "--log-cli-format", "--log-cli-date-format", "--assert", "--verbosity", "--show-capture",
              "--junit-prefix", "--cov", "--cov-report", "--cov-config", "--cov-fail-under",
              "-p", "-n", "--dist"} if runner == "pytest" else {
              "-k", "-s", "--start-directory", "-t", "--top-level-directory", "-p", "--pattern", "--durations"}
    flags = {"-q", "-v", "-s", "-x", "-l", "--lf", "--ff", "--last-failed", "--failed-first",
             "--collect-only", "--co", "--disable-warnings", "--strict-markers", "--strict-config",
             "--showlocals", "--full-trace", "--runxfail", "--no-header", "--no-summary",
             "--continue-on-collection-errors", "--pyargs"} if runner == "pytest" else {
             "-q", "-v", "-f", "-c", "-b", "--quiet", "--verbose", "--failfast", "--catch", "--buffer", "--locals"}
    targets, start, fallback = [], None, False
    discovery = runner == "unittest" and "discover" in args
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            targets.extend(args[index + 1:])
            break
        if runner == "unittest" and arg == "discover":
            index += 1
            continue
        key, _, attached = arg.partition("=")
        if key in values:
            value = attached if "=" in arg else args[index + 1] if index + 1 < len(args) else ""
            if runner == "unittest" and key in {"-s", "--start-directory"}:
                start = value
            index += 1 if "=" in arg else 2
            continue
        if key in flags or arg.startswith("-") and len(arg) > 1 and set(arg[1:]) <= {"q", "v"}:
            index += 1
            continue
        if arg.startswith("-"):
            fallback = True
        else:
            targets.append(arg)
        index += 1
    if fallback:
        return runner, ["."]
    if start is not None:
        return runner, [start or "."]
    if discovery:
        return runner, targets[:1]
    if runner == "unittest":
        targets = [name if "/" in name or name.endswith(".py") else name.replace(".", "/") + ".py" for name in targets]
    elif "--pyargs" in args:
        targets = [name.split("::", 1)[0].replace(".", "/") for name in targets]
    return runner, [name.split("::", 1)[0] for name in targets]


def needs_odoo(root, name):
    path = root / name
    if not path.is_relative_to(root):
        return False
    if imports_odoo(path):
        return True
    # Test collection imports parent packages and conftest files too.
    for parent in (path.parent, *path.parent.parents):
        if not parent.is_relative_to(root):
            break
        if any((parent / manifest).is_file() for manifest in ('__manifest__.py', '__openerp__.py')):
            return True
        if any(imports_odoo(parent / file) for file in ('__init__.py', 'conftest.py')):
            return True
    return False
