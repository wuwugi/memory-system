# -*- coding: utf-8 -*-
"""归档写入：一行一条 markdown，格式固定。"""
import hashlib
import io
import os
import re
import time

INVIS = re.compile(r'[\u200b-\u200f\u202a-\u202e\u2066-\u2069]')


def ensure(d):
    os.makedirs(d, exist_ok=True)
    for f in ('归档.md', '事实库.md', '长期事实.md', '忘掉的.md'):
        p = os.path.join(d, f)
        if not os.path.exists(p):
            io.open(p, 'w', encoding='utf-8').write('')
    st = os.path.join(d, '记忆状态.json')
    if not os.path.exists(st):
        io.open(st, 'w', encoding='utf-8').write('{}')
    for sub in ('索引', '过渡区', '日摘要', '备份'):
        os.makedirs(os.path.join(d, sub), exist_ok=True)


def clean(name):
    s = INVIS.sub('', name or '').strip()
    s = re.sub(r'^(群\d*|群|私聊|回忆|主题)·', '', s).strip()
    return s


def h_of(day, who, text):
    return hashlib.md5((day + clean(who)[-12:] + (text or '')[:120]).encode('utf-8')).hexdigest()[:16]


def append(d, where, sender, text, max_len=600):
    p = os.path.join(d, '归档.md')
    now = time.localtime()
    day = time.strftime('%m-%d', now)
    line = f"- **{day} {time.strftime('%H:%M', now)} · {where}·{sender}**：{text[:max_len]}\n"
    try:
        tail = io.open(p, encoding='utf-8', errors='ignore').read()[-400:]
        if line in tail:
            return
    except Exception:
        pass
    with io.open(p, 'a', encoding='utf-8') as f:
        f.write(line)
