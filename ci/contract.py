"""Public, versioned data contract for a standalone Lean compiler. No controller logic."""
from __future__ import annotations
import hashlib
import json
from pathlib import PurePosixPath
import re

WORKER_PATHS = frozenset({'.github/workflows/lean-check.yml', 'ci/contract.py',
                         'ci/check.py', 'ci/bootstrap.sh', '.gitignore'})
REQUEST_PATH = '.build/request.json'
HEX = re.compile(r'[0-9a-f]{64}')
SHA = re.compile(r'[0-9a-f]{40}')
NAME = re.compile(r'[A-Za-z][A-Za-z0-9_]*')
RESERVED = {'ci', 'scripts', 'config', 'dispatch', 'publication', 'development',
            'tests', 'templates', 'private', 'Paper'}

class ContractError(ValueError):
    pass

def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def safe_path(path: str) -> str:
    if not isinstance(path, str) or not path or len(path) > 240:
        raise ContractError('Invalid path')
    p = PurePosixPath(path)
    if p.is_absolute() or any(x in ('', '.', '..') for x in path.split('/')):
        raise ContractError('Non-canonical relative path')
    if not re.fullmatch(r'[A-Za-z0-9_.\-/]+', path) or '\\' in path:
        raise ContractError('Use ASCII public filenames without escapes')
    return path

def validate_pins(toolchain: str, mathlib: str, roots: list[str]):
    if not isinstance(toolchain, str) or not re.fullmatch(
            r'leanprover/lean4:v\d+\.\d+\.\d+(?:[-.][A-Za-z0-9.]+)?', toolchain):
        raise ContractError('Invalid toolchain')
    if not isinstance(mathlib, str) or not SHA.fullmatch(mathlib):
        raise ContractError('Mathlib must have a full immutable commit')
    if not isinstance(roots, list) or not roots or len(set(roots)) != len(roots):
        raise ContractError('Invalid roots')
    if not all(isinstance(r, str) and NAME.fullmatch(r) and r not in RESERVED for r in roots):
        raise ContractError('Invalid/reserved Lean root')

def allowed_destination(path: str, roots: list[str]) -> bool:
    safe_path(path)
    if path in ('README.md', 'LICENSE', 'lean-toolchain', 'lakefile.toml'):
        return True
    p = PurePosixPath(path)
    if any(x.startswith('.') for x in p.parts):
        return False
    if p.parts[0] == 'Paper' and p.suffix.lower() in ('.tex', '.bib', '.pdf', '.md'):
        return True
    if p.suffix == '.lean':
        parts = p.with_suffix('').parts
        return parts[0] in roots and all(NAME.fullmatch(x) for x in parts)
    return False

def lakefile(mathlib: str, roots: list[str]) -> bytes:
    # Generated TOML has no executable controller or arbitrary Lake hooks.
    text = 'name = "formal_verification"\nversion = "0.1.0"\n\n[leanOptions]\nautoImplicit = false\n\n'
    text += '[[require]]\nname = "mathlib"\ngit = "https://github.com/leanprover-community/mathlib4.git"\n'
    text += 'rev = ' + json.dumps(mathlib) + '\n'
    for root in roots:
        text += '\n[[lean_lib]]\nname = ' + json.dumps(root) + '\n'
    return text.encode()

def bundle_id(request: dict) -> str:
    return sha256(canonical({k: v for k, v in request.items() if k != 'bundle'}))

def validate_request(req: dict) -> list[str]:
    fields = {'schema', 'bundle', 'worker_repository', 'worker_visibility', 'toolchain', 'mathlib', 'roots', 'modules', 'files', 'worker_files'}
    if not isinstance(req, dict) or set(req) != fields or type(req['schema']) is not int or req['schema'] != 2:
        raise ContractError('Unexpected worker request fields/schema')
    if not isinstance(req['worker_repository'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', req['worker_repository']):
        raise ContractError('Invalid worker repository identity')
    if req['worker_visibility'] not in ('private', 'public'):
        raise ContractError('Invalid worker visibility; mode must be explicit')
    validate_pins(req['toolchain'], req['mathlib'], req['roots'])
    if not isinstance(req['bundle'], str) or not HEX.fullmatch(req['bundle']) or req['bundle'] != bundle_id(req):
        raise ContractError('Bundle digest mismatch')
    files = req['files']
    if not isinstance(files, list) or not files or len(files) > 10000:
        raise ContractError('Invalid file list')
    seen, folded, modules = set(), set(), []
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {'path', 'sha256', 'size'}:
            raise ContractError('Invalid file entry')
        path = entry['path']
        if not allowed_destination(path, req['roots']):
            raise ContractError('Disallowed published file')
        if path in seen or path.casefold() in folded:
            raise ContractError('Duplicate/case-colliding path')
        seen.add(path); folded.add(path.casefold())
        if not isinstance(entry['sha256'], str) or not HEX.fullmatch(entry['sha256']):
            raise ContractError('Invalid file digest')
        if type(entry['size']) is not int or not 0 <= entry['size'] <= 20000000:
            raise ContractError('Invalid file size')
        if path.endswith('.lean'):
            modules.append('.'.join(PurePosixPath(path).with_suffix('').parts))
    if not {'lean-toolchain', 'lakefile.toml'} <= seen or not modules:
        raise ContractError('Missing pins or Lean sources')
    workers = req['worker_files']
    if not isinstance(workers, dict) or set(workers) != WORKER_PATHS or not all(
            isinstance(v, str) and HEX.fullmatch(v) for v in workers.values()):
        raise ContractError('Worker fingerprint missing or malformed')
    selected = req['modules']
    if selected is not None and (not isinstance(selected, list) or not selected or
            len(set(selected)) != len(selected) or not all(m in modules for m in selected)):
        raise ContractError('Requested modules do not match published Lean sources')
    return sorted(modules) if selected is None else selected
