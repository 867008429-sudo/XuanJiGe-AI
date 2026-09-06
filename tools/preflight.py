#!/usr/bin/env python3
"""Local deployment preflight checks for XuanJiGe."""
import argparse
import json
import os
import py_compile
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


REQUIRED_GITIGNORE = ['.env', '*.db', '.planning/', 'docs/PROJECT_GUIDE.md', 'backups/']
SECRET_PATTERNS = [
    re.compile(r'github_' + r'pat_[A-Za-z0-9_]+'),
    re.compile(r'DEEPSEEK_API_KEY\s*=\s*sk-(?!your-key-here)[A-Za-z0-9_-]+'),
    re.compile('Aa' + '867008429' + '!'),
]


def make_check(name, ok, required=True, **meta):
    return {
        'name': name,
        'ok': bool(ok),
        'required': bool(required),
        **meta,
    }


def py_files():
    excluded_parts = {'.venv', 'venv', '__pycache__'}
    for path in ROOT.rglob('*.py'):
        if excluded_parts.intersection(path.parts):
            continue
        yield path


def check_python_compile():
    failures = []
    for path in py_files():
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            failures.append({'file': str(path.relative_to(ROOT)), 'error': str(exc)})
    return make_check('python_compile', not failures, failures=failures)


def check_gitignore():
    gitignore = ROOT / '.gitignore'
    content = gitignore.read_text(encoding='utf-8') if gitignore.exists() else ''
    missing = [entry for entry in REQUIRED_GITIGNORE if entry not in content]
    return make_check('gitignore_private_files', not missing, missing=missing)


def tracked_files():
    try:
        result = subprocess.run(
            ['git', 'ls-files'],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            encoding='utf-8',
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [ROOT / line.strip() for line in result.stdout.splitlines() if line.strip()]


def check_tracked_secrets():
    hits = []
    for path in tracked_files():
        if not path.exists() or path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.gif', '.ico', '.db'}:
            continue
        try:
            content = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(content):
                hits.append(str(path.relative_to(ROOT)))
                break
    return make_check('tracked_secret_scan', not hits, hits=hits)


def check_runtime(port=None):
    import app  # noqa: WPS433

    report = app.build_runtime_checks(port=port)
    required_failures = [
        item['name']
        for item in report['checks']
        if item.get('required') and not item.get('ok')
    ]
    return make_check(
        'runtime_checks',
        not required_failures,
        status=report['status'],
        required_failures=required_failures,
        checks=report['checks'],
    )


def check_operator_warnings():
    import app  # noqa: WPS433
    import ai_service  # noqa: WPS433

    warnings = []
    if not ai_service.DEEPSEEK_API_KEY:
        warnings.append('DEEPSEEK_API_KEY 未配置，AI 解读不可用')
    if not app.ADMIN_TOKEN:
        warnings.append('ADMIN_TOKEN 未配置，/api/stats 会默认关闭')
    if os.environ.get('FLASK_DEBUG', '0') == '1':
        warnings.append('FLASK_DEBUG=1，不适合生产环境')
    return make_check('operator_warnings', True, required=False, warnings=warnings)


def run_preflight(port=None):
    checks = [
        check_python_compile(),
        check_gitignore(),
        check_tracked_secrets(),
        check_runtime(port=port),
        check_operator_warnings(),
    ]
    failed = [item for item in checks if item['required'] and not item['ok']]
    warned = [item for item in checks if item.get('warnings')]
    status = 'fail' if failed else 'warn' if warned else 'ok'
    return {
        'status': status,
        'checks': checks,
    }


def main():
    parser = argparse.ArgumentParser(description='Run XuanJiGe pre-deploy checks')
    parser.add_argument('--port', type=int, default=None, help='port to validate')
    parser.add_argument('--json', action='store_true', help='print full JSON output')
    args = parser.parse_args()

    report = run_preflight(port=args.port)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"status={report['status']}")
        for check in report['checks']:
            marker = 'OK' if check['ok'] else 'FAIL'
            required = 'required' if check['required'] else 'optional'
            print(f"{marker} {check['name']} ({required})")
            for warning in check.get('warnings', []):
                print(f"  warning: {warning}")

    return 1 if report['status'] == 'fail' else 0


if __name__ == '__main__':
    raise SystemExit(main())
