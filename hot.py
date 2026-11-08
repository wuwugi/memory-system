# -*- coding: utf-8 -*-
"""热层：长期事实 + 最近几天摘要。每轮都带，所以必须有字数上限。"""
import io
import os


def facts(d, limit=2000):
    p = os.path.join(d, '长期事实.md')
    try:
        lines = [l.rstrip() for l in io.open(p, encoding='utf-8', errors='ignore') if l.startswith('- ')]
    except OSError:
        return ''
    out, total = [], 0
    for l in lines:
        if total + len(l) > limit:
            break
        out.append(l)
        total += len(l)
    return '\n'.join(out)


def recent(d, days=7, limit=1200):
    p = os.path.join(d, '日摘要')
    if not os.path.isdir(p):
        return ''
    files = sorted(f for f in os.listdir(p) if f.endswith('.md'))[-days:]
    out, total = [], 0
    for f in files:
        try:
            txt = io.open(os.path.join(p, f), encoding='utf-8', errors='ignore').read().strip()
        except OSError:
            continue
        piece = f'- {f[:-3]}：{txt}'
        if total + len(piece) > limit:
            break
        out.append(piece)
        total += len(piece)
    return '\n'.join(out)
