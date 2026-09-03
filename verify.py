# -*- coding: utf-8 -*-
"""前端静态校验：JS 语法 + DOM id 完整性"""
import re, io, os, subprocess

os.chdir(os.path.dirname(os.path.abspath(__file__)))
html = io.open('index.html', encoding='utf-8').read()
scripts = re.findall(r'<script>(.*?)</script>', html, re.S)
js = scripts[-1]
io.open('_inline.js', 'w', encoding='utf-8').write(js)

refs = set(re.findall(r"\$\('([A-Za-z0-9_]+)'\)", js)) | \
       set(re.findall(r'\$\("([A-Za-z0-9_]+)"\)', js)) | \
       set(re.findall(r"getElementById\('([A-Za-z0-9_]+)'\)", js))
defs = set(re.findall(r'id="([A-Za-z0-9_]+)"', html))
missing = sorted(refs - defs)
print('referenced ids:', len(refs), '| defined ids:', len(defs))
print('MISSING:', missing if missing else 'none')

r = subprocess.run(['node', '--check', '_inline.js'], capture_output=True, text=True)
if r.returncode == 0:
    print('JS syntax OK')
else:
    print('JS SYNTAX ERROR:')
    print(r.stderr[:2500])
    raise SystemExit(1)
