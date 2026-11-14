#!/usr/bin/env python3
"""Builds dist/index.html from page.src.html + base.css + data.json."""
import json, os
src = open('page.src.html', encoding='utf8').read()
css = open('base.css', encoding='utf8').read()
data = json.dumps(json.load(open('data.json', encoding='utf8')), ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
out = src.replace('/*__BASE_CSS__*/', css).replace('/*__EMBED__*/null', data)
os.makedirs('dist', exist_ok=True)
open('dist/index.html', 'w', encoding='utf8').write(out)
open('dist/data.json', 'w', encoding='utf8').write(json.dumps(json.load(open('data.json', encoding='utf8')), ensure_ascii=False, separators=(',', ':')))
print(len(out))
