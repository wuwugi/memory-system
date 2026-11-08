# -*- coding: utf-8 -*-
"""检索：关键词 + 语义（可选），合并去重。

索引文件：索引/meta.jsonl（每行 {h, day, who, text, kind}）、索引/vectors.npy
没有索引也能用：退化成关键词扫描，只是慢。
"""
import io
import json
import math
import os
import re
import time

STOP = set('的了吗呢吧啊我你他她它们这那有是在和就不都很也还你个一'.split())
INVIS = re.compile(r'[\u200b-\u200f\u202a-\u202e\u2066-\u2069]')

_embedder = None


def _load(dirpath):
    meta_p = os.path.join(dirpath, '索引', 'meta.jsonl')
    vec_p = os.path.join(dirpath, '索引', 'vectors.npy')
    if not (os.path.exists(meta_p) and os.path.exists(vec_p)):
        return None, None
    meta = [json.loads(l) for l in io.open(meta_p, encoding='utf-8', errors='ignore') if l.strip()]
    try:
        import numpy as np
        vec = np.load(vec_p)
    except Exception:
        return meta, None
    return meta, vec


def _embed(texts):
    global _embedder
    try:
        if _embedder is None:
            from fastembed import TextEmbedding
            _embedder = TextEmbedding(model_name='BAAI/bge-small-zh-v1.5')
        import numpy as np
        out = list(_embedder.embed(texts))
        v = np.array(out, dtype='float32')
        n = np.linalg.norm(v, axis=1, keepdims=True)
        n[n == 0] = 1
        return v / n
    except Exception:
        return None


def _bigrams(s):
    s = INVIS.sub('', s or '')
    s = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]', '', s)
    return [s[i:i + 2] for i in range(max(0, len(s) - 1))]


def _kw_score(q_grams, text):
    if not q_grams:
        return 0.0
    t = INVIS.sub('', text or '')
    hit = sum(1 for g in q_grams if g in t)
    return hit / len(q_grams)


def query(dirpath, q, top=8, semantic=True):
    meta, vec = _load(dirpath)
    try:
        st = json.load(io.open(os.path.join(dirpath, '记忆状态.json'), encoding='utf-8'))
    except Exception:
        st = {}
    if not meta:
        return _scan(dirpath, q, top)
    grams = _bigrams(q)
    scores = [0.0] * len(meta)
    for i, m in enumerate(meta):
        scores[i] = _kw_score(grams, m.get('text', ''))

    if semantic and vec is not None and len(meta) == len(vec):
        qv = _embed([q])
        if qv is not None:
            import numpy as np
            sim = vec @ qv[0]
            for i in range(len(scores)):
                scores[i] = 0.6 * float(sim[i]) + 0.4 * scores[i]

    # 缓冲期内原文为主：原文还在时，回忆行降权当备胎
    for i, m in enumerate(meta):
        w = (m.get('who') or '') + (m.get('text') or '')[:16]
        if '回忆·' in w or '主题·' in w:
            rec = st.get(m.get('h')) or {}
            orig = rec.get('orig') or []
            if any(('pending:' + o) in st for o in orig):
                scores[i] *= 0.55
        elif ('pending:' + (m.get('h') or '')) in st:
            scores[i] *= 1.15

    order = sorted(range(len(meta)), key=lambda i: -scores[i])[:top]
    out = []
    for i in order:
        if scores[i] <= 0:
            continue
        m = meta[i]
        _d = m.get('day') or m.get('d') or ''
        _tm = m.get('time') or m.get('m') or ''
        _w = m.get('who') or m.get('s') or ''
        _t = m.get('text') or m.get('t') or ''
        out.append(f"- [{_d} {_tm}] {_w}：{_t[:120]}")
    return out


def _scan(dirpath, q, top):
    path = os.path.join(dirpath, '归档.md')
    if not os.path.exists(path):
        return []
    grams = _bigrams(q)
    scored = []
    for line in io.open(path, encoding='utf-8', errors='ignore'):
        s = _kw_score(grams, line)
        if s > 0:
            scored.append((s, line.strip()))
    scored.sort(key=lambda x: -x[0])
    return [l[:160] for _, l in scored[:top]]
