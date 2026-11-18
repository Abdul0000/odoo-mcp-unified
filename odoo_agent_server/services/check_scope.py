"""Inspect test scope and imports without importing application packages."""
import ast
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


def test_targets(argv):
    """Conventional explicit pytest/unittest targets; no source execution."""
    if len(argv) >= 3 and argv[1] == '-m' and argv[2] in {'pytest', 'unittest'}:
        runner, args = argv[2], argv[3:]
    elif Path(argv[0]).name == 'pytest':
        runner, args = 'pytest', argv[1:]
    else:
        return None, []
    targets = []
    for arg in args:
        if arg.startswith('-') or arg == 'discover':
            continue
        name = arg.split('::', 1)[0]
        if runner == 'unittest' and '/' not in name and not name.endswith('.py'):
            name = name.replace('.', '/') + '.py'
        targets.append(name)
    return runner, targets


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
