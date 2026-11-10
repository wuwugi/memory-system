# -*- coding: utf-8 -*-
"""从归档里抽"用户自述事实"，写 用户档案.md。用模型抽，带日期，按字段留最新。

用法：
  python profile.py            # 增量抽取（只处理新行）
  python profile.py --rebuild  # 全量重抽
"""
import hashlib
import io
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, 'config.json')
CFG_EXAMPLE = os.path.join(HERE, 'config.example.json')


def load_cfg():
    for p in (CFG_PATH, CFG_EXAMPLE):
        if os.path.exists(p):
            return json.load(io.open(p, encoding='utf-8-sig'))
    return {}


CFG = load_cfg()
DATA_DIR = os.environ.get('MEM_DIR') or CFG.get('data_dir', './data/memory')
if not os.environ.get('MEM_DIR') and not os.path.isabs(DATA_DIR):
    DATA_DIR = os.path.join(HERE, DATA_DIR)
ARCH = os.path.join(DATA_DIR, '归档.md')
OUT = os.path.join(DATA_DIR, '用户档案.md')
STATE = os.path.join(DATA_DIR, '索引', 'profile_state.json')

MODEL = CFG.get('model', {})
API_KEY = os.environ.get('FISH_API_KEY') or MODEL.get('api_key') or ''
BASE = (MODEL.get('base_url') or 'https://api.deepseek.com').rstrip('/')
NAME = MODEL.get('model') or 'deepseek-chat'

LINE = re.compile(r'^- \*\*(\d\d)-(\d\d) (\d\d):(\d\d) · (.+?)\*\*：(.+)$')
FIELDS = ('姓名', '年龄', '城市', '职业', '生日', '家人', '账号', '偏好', '其他')
CHUNK = 240                      # 每次喂给模型的行数

# 模型抽不到时的兜底（只认最明确的句式）
FALLBACK = [
    ('姓名', r'叫我([\u4e00-\u9fa5]{2,4})(?:就行|就好了|吧|好了)'),
    ('姓名', r'我叫([\u4e00-\u9fa5]{2,4})'),
    ('年龄', r'我(?:今年)?(\d{1,2})岁'),
    ('生日', r'生日(?:是|在)?\s*(\d{1,2}\s*月\s*\d{1,2}\s*[日号])'),
    ('城市', r'我(?:现在)?(?:在|住在)([\u4e00-\u9fa5]{2,6})(?:上班|工作|生活)'),
]


def read_state():
    if os.path.exists(STATE):
        try:
            return json.load(io.open(STATE, encoding='utf-8'))
        except Exception:
            pass
    return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + '.tmp'
    io.open(tmp, 'w', encoding='utf-8').write(json.dumps(st, ensure_ascii=False))
    os.replace(tmp, STATE)


def ask(lines):
    if not API_KEY:
        return []
    body_txt = '\n'.join(f'[{d}] {w}：{t}' for d, w, t in lines)
    prompt = ('抽取用户自述事实。每条一行 JSON：{"date":"MM-DD","field":"姓名|年龄|城市|职业|生日|家人|账号|偏好|其他",'
              '"value":"简短"}。只抽用户自己明确说过的；改口各记一条；不要推断。没有就返回空。\n\n' + body_txt[:12000])
    payload = {'model': NAME,
               'messages': [{'role': 'system', 'content': '只输出 JSON 行，不要解释。'},
                            {'role': 'user', 'content': prompt}],
               'temperature': 0.0, 'max_tokens': 800}
    req = urllib.request.Request(BASE + '/chat/completions',
                                 data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json',
                                          'Authorization': 'Bearer ' + API_KEY})
    with urllib.request.urlopen(req, timeout=120) as r:
        txt = json.loads(r.read().decode())['choices'][0]['message']['content']
    out = []
    for l in txt.splitlines():
        m = re.search(r'\{.*\}', l)
        if not m:
            continue
        try:
            d = json.loads(m.group(0))
        except Exception:
            continue
        dt, fl, val = (str(d.get('date') or '').strip(), str(d.get('field') or '其他').strip(),
                       str(d.get('value') or '').strip())
        if re.match(r'^\d\d-\d\d$', dt) and val:
            out.append({'date': dt, 'field': fl if fl in FIELDS else '其他', 'value': val[:60]})
    return out


def fallback(rows):
    out = []
    for day, who, text in rows:
        for field, pat in FALLBACK:
            m = re.search(pat, text)
            if m:
                out.append({'date': day, 'field': field, 'value': m.group(1).strip()[:40]})
    return out


def write(facts):
    cur = {}
    for f in facts:
        k = f['field']
        if k not in cur or f['date'] >= cur[k]['date']:
            cur[k] = f
    body = ['# 用户档案（自动整理，可能过时）', '',
            '> 从聊天里抽出来的用户自述。和对话冲突时，以对话为准。', '']
    for k in ('姓名', '年龄', '生日', '城市', '职业', '家人', '账号', '偏好', '其他'):
        if k in cur:
            body.append(f"- **{k}**：{cur[k]['value']}")
    io.open(OUT, 'w', encoding='utf-8').write('\n'.join(body) + '\n')
    return OUT


def main():
    rebuild = '--rebuild' in sys.argv
    if not os.path.exists(ARCH):
        print('没有归档，跳过')
        return

    rows = []
    with io.open(ARCH, encoding='utf-8', errors='ignore') as f:
        for raw in f:
            m = LINE.match(raw.strip())
            if not m:
                continue
            who = m.group(5)
            if '回忆·' in who or '主题·' in who or '·我(' in who:
                continue
            rows.append((f'{m.group(1)}-{m.group(2)}', who, m.group(6)))

    st = {} if rebuild else read_state()
    facts = st.get('facts') or []
    done = int(st.get('n') or 0)
    todo = rows[done:]
    if not todo:
        write(facts)
        print(f'没有新内容（档案 {len(facts)} 条）')
        return

    print(f'新增 {len(todo)} 行，分 {max(1, (len(todo) + CHUNK - 1) // CHUNK)} 批抽取…')
    got = []
    for i in range(0, len(todo), CHUNK):
        part = todo[i:i + CHUNK]
        try:
            got.extend(ask(part))
        except Exception as e:
            print('  模型抽取失败：', type(e).__name__, str(e)[:80])
        got.extend(fallback(part))
    facts.extend(got)
    save_state({'n': len(rows), 'facts': facts})
    p = write(facts)
    print(f'已写 {p}（本次 {len(got)} 条，累计 {len(facts)} 条）')


if __name__ == '__main__':
    main()
