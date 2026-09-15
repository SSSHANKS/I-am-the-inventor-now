"""Import provenance checks for disposable behavior-probe subprocesses.

This is a validation guard, not a security sandbox for hostile Python code.
"""
import builtins
import runpy
import sys
from pathlib import Path


def run_probe(probe_file, import_roots):
    roots = tuple(Path(root).resolve() for root in import_roots)
    project_names = set()
    for root in roots:
        if root.is_dir():
            for child in root.iterdir():
                if child.is_file() and child.suffix == '.py':
                    project_names.add(child.stem)
                elif child.is_dir() and any(child.rglob('*.py')):
                    project_names.add(child.name)
    original_import = builtins.__import__
    violations = set()
    observed = set()
    checking = False

    def check_modules():
        for name, module in tuple(sys.modules.items()):
            if name.partition('.')[0] not in project_names:
                continue
            observed.add(name)
            spec = getattr(module, '__spec__', None)
            origin = getattr(spec, 'origin', None)
            locations = list(getattr(spec, 'submodule_search_locations', None) or [])
            if origin:
                locations.append(origin)
            file = getattr(module, '__file__', None)
            if file:
                locations.append(file)
            if not locations or any(
                not any(Path(location).resolve().is_relative_to(root) for root in roots)
                for location in locations
            ):
                violations.add(name)

    def checked_import(*args, **kwargs):
        nonlocal checking
        result = original_import(*args, **kwargs)
        if not checking:
            checking = True
            try:
                check_modules()
            finally:
                checking = False
        return result

    builtins.__import__ = checked_import
    completed = False
    try:
        runpy.run_path(probe_file, run_name='__main__')
        completed = True
    finally:
        builtins.__import__ = original_import
        check_modules()
        if violations:
            raise RuntimeError('PROBE_INTEGRITY: imported replacement project modules: '
                               + ', '.join(sorted(violations)))
        if completed and not observed:
            raise RuntimeError('PROBE_INTEGRITY: probe did not import the generated project')


if 'PROBE_FILE' in globals():
    run_probe(PROBE_FILE, IMPORT_ROOTS)
