"""Syntax-check inline JavaScript in static HTML pages using node --check."""
import os
import re
import subprocess
import tempfile

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app', 'static')
LT = chr(60)
GT = chr(62)
SCRIPT_PATTERN = re.compile(
    LT + 'script(?![^' + GT + ']*\\bsrc=)[^' + GT + ']*>' + '(.*?)' + LT + '/script>',
    re.DOTALL | re.IGNORECASE,
)


def main():
    failures = 0
    checked = 0
    for name in sorted(os.listdir(STATIC_DIR)):
        if not name.endswith('.html'):
            continue
        with open(os.path.join(STATIC_DIR, name), encoding='utf-8') as handle:
            text = handle.read()
        for index, block in enumerate(SCRIPT_PATTERN.findall(text)):
            if not block.strip():
                continue
            checked += 1
            temp_path = os.path.join(tempfile.gettempdir(), f'stormpilot_{name}_{index}.js')
            with open(temp_path, 'w', encoding='utf-8') as handle:
                handle.write(block)
            result = subprocess.run(['node', '--check', temp_path], capture_output=True, text=True)
            if result.returncode != 0:
                failures += 1
                print(f'FAIL {name} script #{index}: {result.stderr.strip()}')
            else:
                print(f'OK   {name} script #{index}')
            os.remove(temp_path)
    if checked == 0:
        print('No inline scripts found to check.')
    print(f'checked={checked} failed={failures}')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())