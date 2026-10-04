"""Protocol double for anonymous K3-derived indexing failure dimensions.

Real model acceptance uses the four selected local K3 tasks separately. This
worker preserves the complete block protocol and cannot claim model quality.
"""
import json
from pathlib import Path
import sys
import time

from mindie_knowledge.materials import summarizer as s


def command(*, title='Public case', summary='Reported observations remain reference material.',
            calls=None, started=None, release=None, fail=False, required=None):
    options = dict(title=title, summary=summary, calls=str(calls) if calls else None,
                   started=str(started) if started else None, release=str(release) if release else None,
                   fail=fail, required=required)
    return [sys.executable, str(Path(__file__).resolve()), json.dumps(options)]


def answer(request, title, summary):
    result = dict(blocks=[dict(block_id=b['block_id'], title=title, summary=summary)
                          for b in request['blocks']], navigation=dict(title=title, summary=summary))
    return s.outcome(request, status='returned', result=result, raw_result=json.dumps(result),
                     model_calls=1, usage_known=True,
                     usage=dict(input_tokens=120, cached_input_tokens=0, output_tokens=30))


def identity():
    return s.policy_identity(model='fixture', effort='low', implementation={'fixture':'anonymous-k3-v1'})


def package_body(package):
    import yaml
    header = yaml.safe_load(package['files']['index.md'][4:].split('\n---\n\n', 1)[0])
    return ''.join(package['files'][f"blocks/{b['block_id']}.md"].split('\n---\n\n', 1)[1]
                   for b in header['blocks'])


if __name__ == '__main__':
    options = json.loads(sys.argv[1])
    if '--identity' in sys.argv:
        print(json.dumps(identity()))
        raise SystemExit
    request = json.load(sys.stdin)
    if options['required']:
        assert options['required'] in ''.join(b['text'] for b in request['blocks'])
    if options['calls']:
        path = Path(options['calls'])
        path.write_text((path.read_text() if path.exists() else '') + 'x')
    if options['started']:
        Path(options['started']).touch()
    while options['release'] and not Path(options['release']).exists():
        time.sleep(.05)
    response = (s.outcome(request, status='failed', error='invalid_result', model_calls=1,
                          usage_known=True, usage={'input_tokens':120,'cached_input_tokens':0,'output_tokens':30})
                if options['fail'] else answer(request, options['title'], options['summary']))
    print(json.dumps(response))
