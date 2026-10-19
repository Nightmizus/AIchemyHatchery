"""Opt-in real-model check against disposable synthetic website source only."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from site_files_tests import FileFixture, USER
import server
import site_files


def main():
    if '--live' not in sys.argv:
        raise SystemExit('Use --live to call the configured model (consumes API usage). No production website is scanned.')
    with FileFixture() as fixture, patch.object(server, 'PORT', fixture.port):
        # Keep the smoke fixture independent of other suites using the same user ID.
        fixture.stack.enter_context(patch.object(site_files, 'LOCK_ROOT', fixture.root / '.locks'))
        (fixture.directory / 'index.html').write_text('''<!doctype html><html lang="zh"><title>本地安全检查样例</title>
<main id="welcome"></main><script>
const name = new URLSearchParams(location.search).get('name');
document.getElementById('welcome').innerHTML = name;
</script></html>''')
        (fixture.directory / 'backend').mkdir()
        (fixture.directory / 'backend/app.py').write_text('''# Synthetic source only; never deployed or executed.
def read_records(db, request):
    owner = request.args.get("owner")
    return db.execute("SELECT title FROM records WHERE owner='" + owner + "'").fetchall()
''')
        before = site_files.file_manifest(fixture.directory)
        def progress(event):
            if event.get('kind') != 'context':
                print(json.dumps({key: event.get(key) for key in ('label', 'detail', 'status')}, ensure_ascii=False), flush=True)
        result = fixture.handler.run_ai_harness(USER, '', {}, True, [], [], mode='security', progress=progress)
        assert before == site_files.file_manifest(fixture.directory), 'Read-only audit modified source'
        print(json.dumps({'model': result['security'].get('model'), 'summary': result['summary'],
                          'changedFiles': result['changedFiles'], 'checks': result['checks']}, ensure_ascii=False, indent=2))


if __name__ == '__main__': main()
