#!/usr/bin/env python3
"""Minimal worker compilation only. Never contacts its private controller."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import sys
import time
from contract import (ContractError, REQUEST_PATH, WORKER_PATHS, canonical,
                      lakefile, sha256, validate_request)

ROOT = Path(__file__).resolve().parents[1]

def git(root: Path, *args) -> str:
    return subprocess.check_output(['git', *args], cwd=root, text=True,
                                   stderr=subprocess.PIPE).strip()

def read_regular(root: Path, rel: str) -> bytes:
    p = root / rel
    for parent in [p, *p.parents]:
        if parent == root.parent:
            break
        if parent.is_symlink():
            raise ContractError('Symlink in source tree')
    if not p.is_file() or not p.resolve().is_relative_to(root.resolve()):
        raise ContractError('Missing or escaped Worker file')
    return p.read_bytes()

def check_github_worker_identity(req: dict, source_sha: str):
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        return  # local testing has no GitHub repository-event identity
    if os.environ.get('GITHUB_REPOSITORY', '').casefold() != req['worker_repository'].casefold():
        raise ContractError('Actions repository identity does not match approved worker')
    if os.environ.get('GITHUB_REF') != 'refs/heads/main' or os.environ.get('GITHUB_EVENT_NAME') not in ('push', 'workflow_dispatch'):
        raise ContractError('Unexpected worker event/branch')
    if os.environ.get('GITHUB_SHA') != source_sha:
        raise ContractError('Action commit does not match checked out worker commit')
    event_path = os.environ.get('GITHUB_EVENT_PATH')
    if not event_path:
        raise ContractError('Missing GitHub event identity')
    event = json.loads(Path(event_path).read_text(encoding='utf-8'))
    repo = event.get('repository')
    if not isinstance(repo, dict) or repo.get('full_name', '').casefold() != req['worker_repository'].casefold():
        raise ContractError('GitHub event repository mismatch')
    actual_private = repo.get('private')
    if type(actual_private) is not bool or actual_private != (req['worker_visibility'] == 'private'):
        raise ContractError('Worker visibility mismatch against approved manifest')
    if repo.get('visibility') is not None and repo['visibility'] != req['worker_visibility']:
        raise ContractError('Worker visibility metadata disagreement')


def preflight(root: Path = ROOT) -> tuple[dict, list[str], str]:
    req = json.loads(read_regular(root, REQUEST_PATH))
    modules = validate_request(req)
    if sum(x['size'] for x in req['files']) > 50000000:
        raise ContractError('Worker source tree too large')
    for entry in req['files']:
        data = read_regular(root, entry['path'])
        if len(data) != entry['size'] or sha256(data) != entry['sha256']:
            raise ContractError('Worker file does not match approved snapshot')
    for path, digest in req['worker_files'].items():
        if sha256(read_regular(root, path)) != digest:
            raise ContractError('Compiler infrastructure fingerprint mismatch')
    if read_regular(root, 'lean-toolchain') != (req['toolchain'] + '\n').encode():
        raise ContractError('Toolchain file mismatch')
    if read_regular(root, 'lakefile.toml') != lakefile(req['mathlib'], req['roots']):
        raise ContractError('Only the generated, pinned Lake dependency file is accepted')
    if git(root, 'status', '--porcelain', '--untracked-files=no'):
        raise ContractError('Dirty tracked worktree: cannot attribute to a commit')
    tracked = set(git(root, 'ls-files', '-z').split('\0')) - {''}
    expected = WORKER_PATHS | {REQUEST_PATH} | {x['path'] for x in req['files']}
    if tracked != expected:
        raise ContractError('Unexpected tracked files or missing Worker manifest entries')
    for prefix in req['roots']:
        for p in (root / prefix).rglob('*.lean'):
            if p.relative_to(root).as_posix() not in expected:
                raise ContractError('Untracked Lean source would affect the build')
    source = git(root, 'rev-parse', 'HEAD')
    check_github_worker_identity(req, source)
    return req, modules, source

# An import plan is derived from the EXACT approved source snapshot. It never
# rewrites Lean proof files or guesses imports from theorem names.
_IMPORT_LINE = re.compile(r'^\s*(?:(?:public|meta)\s+)*import\s+(.+?)\s*$')
_MODULE = re.compile(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*\Z')
_BROAD_MATHLIB = {'Mathlib', 'Mathlib.Tactic'}


def strip_lean_comments(source: str) -> str:
    """Mask nested Lean comments and quoted strings while preserving newlines.

    We inspect import commands rather than alter the source. This conservative
    parser accepts ordinary Lean import declarations and rejects malformed
    import lists instead of silently broadening the cache download.
    """
    out = []
    i = 0
    depth = 0
    quoted = False
    line_comment = False
    while i < len(source):
        c = source[i]
        if c == '\n':
            out.append('\n')
            line_comment = False
            i += 1
            continue
        if line_comment:
            out.append(' ')
            i += 1
            continue
        if depth:
            if source.startswith('/-', i):
                depth += 1
                out.extend('  ')
                i += 2
            elif source.startswith('-/', i):
                depth -= 1
                out.extend('  ')
                i += 2
            else:
                out.append(' ')
                i += 1
            continue
        if quoted:
            if c == '\\' and i + 1 < len(source):
                out.extend('  ')
                i += 2
            elif c == '"':
                out.append(' ')
                quoted = False
                i += 1
            else:
                out.append(' ')
                i += 1
            continue
        if source.startswith('/-', i):
            depth = 1
            out.extend('  ')
            i += 2
        elif source.startswith('--', i):
            line_comment = True
            out.extend('  ')
            i += 2
        elif c == '"':
            quoted = True
            out.append(' ')
            i += 1
        else:
            out.append(c)
            i += 1
    if depth:
        raise ContractError('Unclosed Lean block comment in approved source')
    return ''.join(out)


def imports_from_sources(sources: dict[str, bytes], selected: list[str],
                         roots: list[str]) -> list[str]:
    """Pure import-plan computation over the approved Lean files only.

    Also called by the private Controller before the SHA256 approval phase, so
    invalid local dependencies and broad umbrella imports fail before release.
    """
    approved = {'.'.join(Path(path).with_suffix('').parts): data
                for path, data in sources.items() if path.endswith('.lean')}
    needed = set()
    visited = set()
    pending = list(selected)
    while pending:
        module = pending.pop()
        if module in visited:
            continue
        if module not in approved:
            raise ContractError('Approved local Lean dependency missing: ' + module)
        visited.add(module)
        try:
            source = approved[module].decode('utf-8')
        except UnicodeDecodeError as ex:
            raise ContractError('Lean source must be UTF-8') from ex
        for line in strip_lean_comments(source).splitlines():
            match = _IMPORT_LINE.fullmatch(line)
            if not match:
                continue
            imports = match.group(1).split()
            if not imports or not all(_MODULE.fullmatch(m) for m in imports):
                raise ContractError('Unsupported Lean import syntax in ' + module)
            for imported in imports:
                if imported in _BROAD_MATHLIB:
                    raise ContractError('Broad Mathlib import forbidden in ' + module + ': ' + imported)
                if imported.startswith('Mathlib.'):
                    needed.add(imported)
                elif imported in approved:
                    pending.append(imported)
                elif imported.split('.')[0] in roots:
                    raise ContractError('Unapproved local dependency: ' + imported)
                # Other transitive packages (e.g. Lean, Batteries) are handled
                # by the pinned Lake dependency, not by a fake Mathlib root.
    return sorted(needed)


def required_mathlib_imports(root: Path = ROOT) -> list[str]:
    """Direct Mathlib imports from the transitive approved module closure.

    The pinned Mathlib cache tool downloads transitive upstream dependencies.
    This function selects only its roots; Lean compilation checks sufficiency.
    """
    req, selected, _ = preflight(root)
    sources = {e['path']: read_regular(root, e['path'])
               for e in req['files'] if e['path'].endswith('.lean')}
    return imports_from_sources(sources, selected, req['roots'])


def compiler_env() -> dict:
    # Hygiene, NOT an OS sandbox; this worker must contain no controller secrets.
    keys = ('PATH', 'HOME', 'USER', 'LANG', 'LC_ALL', 'TMPDIR', 'TMP', 'TEMP',
            'ELAN_HOME', 'TERM', 'SSL_CERT_FILE', 'SSL_CERT_DIR')
    env = {k: os.environ[k] for k in keys if k in os.environ}
    env['PATH'] = str(Path.home() / '.elan/bin') + os.pathsep + env.get('PATH', '')
    env['GIT_TERMINAL_PROMPT'] = '0'
    return env

def compile_project(root: Path = ROOT, timeout: int = 1800) -> dict:
    req, modules, source = preflight(root)
    actual = git(root / '.lake/packages/mathlib', 'rev-parse', 'HEAD')
    if actual != req['mathlib']:
        raise ContractError('Resolved Mathlib revision mismatch')
    out = root / '.build-output'; out.mkdir(exist_ok=True)
    log = out / 'compiler.log'
    started = time.monotonic()
    checks = []
    overall = 0
    with log.open('wb') as fd:
        for index, module in enumerate(modules):
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                overall = 124
                checks.extend({'module': m, 'status': 'not_run', 'exit_code': None,
                               'elapsed_seconds': 0.0} for m in modules[index:])
                break
            fd.write(('\n[worker] BEGIN module ' + module + '\n').encode()); fd.flush()
            module_start = time.monotonic()
            proc = subprocess.Popen(['lake', 'build', module], cwd=root, env=compiler_env(),
                                    stdout=fd, stderr=subprocess.STDOUT, start_new_session=True)
            timed_out = False
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                timed_out = True
                try: os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError: pass
                try: proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try: os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    proc.wait()
            code = 124 if timed_out else proc.returncode
            status = 'timed_out' if timed_out else ('passed' if code == 0 else 'failed')
            checks.append({'module': module, 'status': status, 'exit_code': code,
                           'elapsed_seconds': round(time.monotonic() - module_start, 3)})
            fd.write(('[worker] END module ' + module + ' status=' + status + '\n').encode()); fd.flush()
            if code != 0 and overall == 0:
                overall = code
            if timed_out:
                checks.extend({'module': m, 'status': 'not_run', 'exit_code': None,
                               'elapsed_seconds': 0.0} for m in modules[index+1:])
                break
    if len(checks) != len(modules):
        raise ContractError('Incomplete module accounting')
    digest = hashlib.sha256()
    with log.open('rb') as fd:
        for block in iter(lambda: fd.read(1024 * 1024), b''): digest.update(block)
    record = {'schema': 2, 'bundle': req['bundle'], 'worker_repository': req['worker_repository'],
              'worker_visibility': req['worker_visibility'], 'worker_commit': source,
              'toolchain': req['toolchain'], 'mathlib': actual, 'modules': modules,
              'module_checks': checks,
              'whole_project': req['modules'] is None, 'exit_code': overall,
              'build_success': overall == 0 and all(c['status'] == 'passed' for c in checks),
              'proof_complete': None,
              'elapsed_seconds': round(time.monotonic() - started, 3),
              'log_sha256': digest.hexdigest(), 'assurance': 'compiler-output-trusted-input'}
    (out / 'receipt.json').write_text(json.dumps(record, indent=2) + '\n')
    print('Build status:', overall, 'Commit:', source, 'Bundle:', req['bundle'], flush=True)
    return record

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=['preflight', 'compile', 'imports'])
    p.add_argument('--timeout', type=int, default=1800)
    a = p.parse_args()
    if not 1 <= a.timeout <= 3000: raise ContractError('Invalid timeout')
    if a.command == 'preflight':
        req, _, _ = preflight()
        print('Approved worker snapshot:', req['bundle'])
        return 0
    if a.command == 'imports':
        for module in required_mathlib_imports():
            print(module)
        return 0
    return 0 if compile_project(timeout=a.timeout)['build_success'] else 1

if __name__ == '__main__':
    try: sys.exit(main())
    except (ContractError, ValueError, OSError, subprocess.CalledProcessError):
        print('Compiler preflight/runtime error. Inspect the worker snapshot and pinned environment.', file=sys.stderr)
        sys.exit(2)
