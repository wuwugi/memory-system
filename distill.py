# -*- coding: utf-8 -*-
"""记忆蒸馏（S0 → S1）原型。

只读 + 预览，不写任何文件。用法：
    python distill.py --dry-run --samples 2
"""
import argparse
import io
import json
import os
import random
import re
import urllib.request
import collections

ARCH = os.path.join(os.environ.get('MEM_DIR', './data/memory'), '归档.md')
CFG = os.environ.get('MEM_CONFIG', './config.json')

LINE = re.compile(r'- \*\*(\d\d)-(\d\d) (\d\d):(\d\d) · ([^*]+)\*\*：(.*)')
CHUNK_SUF = re.compile(r"·\d+/\d+$")
INVIS = re.compile(r'[\u200b-\u200f\u2060-\u206f\ufeff\ue000-\uf8ff\u202a-\u202e]')
QQ_IN = re.compile(r'[（(](\d{5,12})[）)]')

KNOWN = {
    '1000000001': '主人',
    '1000000002': '我',
    '1000000003': '姑姑',
}

# 带链接 / 本地路径 / 命令的条目 → 保护类，不进蒸馏
PROTECT = [
    re.compile(r'https?://', re.I),
    re.compile(r'[A-Za-z]:[\\/]|/mnt/|~/'),
    re.compile(r'\b(git|pip|npm|node|python|uv|curl|schtasks)\s+\S+', re.I),
    re.compile(r'密码|口令|token|密钥|apikey|api[_-]?key', re.I),
]


def canon_who(raw):
    """剥掉所有前导的「场合·」前缀，得到唯一署名。

    回忆·群·某人 → 某人
    私聊·某人    → 某人
    群1105·某人  → 某人
    所有算哈希的地方都必须用这个，否则同一行在不同模块里会得到不同哈希。
    """
    s = CHUNK_SUF.sub("", (raw or "").strip())
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"^(群\d*|群|私聊|回忆|主题)·", "", s)
    return s


def split_surface(raw):
    """把发送人标签拆成（场合, 干净署名）。

    私聊·influence(1000000001)      → ("私聊", "对方(1000000001)")
    群·小明(1000000004)             → ("群", "小明(1000000004)")
    群123456·小明(1000000004)   → ("群123456", "小明(1000000004)")
    """
    s = (raw or "").strip()
    m = re.match(r"^(私聊|群\d*|群|回忆|主题)·", s)
    surface = m.group(1) if m else ""
    if surface in ("回忆", "主题"):
        # 派生行：再拆一层
        s2 = s[len(surface) + 1:]
        m2 = re.match(r"^(私聊|群\d*|群)·", s2)
        if m2:
            return m2.group(1), clean_name(s2)
        return surface, clean_name(s2)
    return surface, clean_name(s)


def clean_name(raw):
    """群·influence(1000000001) → 对方(1000000001)"""
    s = raw.strip()
    # 电脑端（DSH）的两种标签：她=机器人（电脑上），对面=你（电脑上）
    if s.startswith('机器人') and '电脑上' in s:
        return '我(1000000002)'
    if s.startswith('你') and '电脑上' in s:
        return '对方(1000000001)'
    _prev = None
    while _prev != s:          # 剥掉所有前导场合前缀，保证哈希唯一
        _prev = s
        s = re.sub(r'^(群\d*|群|私聊|回忆|主题)·', '', s)
    s = INVIS.sub('', s)
    m = QQ_IN.search(s)
    qq = m.group(1) if m else ''
    name = QQ_IN.sub('', s).strip()
    name = re.sub(r'[（(]ID:\d+[）)]', '', name).strip()
    name = re.sub(r'^[.。\s]+|[.。\s]+$', '', name)
    # 有些条目没有括号，发送人直接就是 QQ 号
    if not qq and name.isdigit() and 5 <= len(name) <= 12:
        qq = name
        name = ''
    if qq in KNOWN:
        return f'{KNOWN[qq]}({qq})'
    if qq:
        return f'{name}({qq})' if name else f'({qq})'
    return name or '某人'


FACT = [
    re.compile(r'答应|承诺|说好|约好|约定|保证|一定|别忘了|记着'),
    re.compile(r'生日|纪念日|那天是|周年'),
    re.compile(r'我姐|我妈|我爸|我哥|姑姑|外婆|奶奶|舅舅|姨'),
    re.compile(r'\d+\s*(块|元|块钱|红包|工资|零花)'),
    re.compile(r'密码|账号|号是|QQ\s*\d|微信'),
]


def is_fact(text):
    """约定/承诺/亲属/生日/金额/账号 —— 这类不模糊化，直接进长期事实。

    2026-11-12 收紧：必须"主体 + 具体信息"，泛泛一句"我记着"不算；
    提问、求助、任务指令（带问号、问个问题、帮我、怎么）也不算。
    """
    t = (text or '').strip()
    if len(t) < 8:
        return False
    if t.endswith(('？', '?')) or '问个问题' in t or '帮我' in t or '怎么' in t:
        return False
    if re.search(r'记不记得|记得吗|还记不记得|知道吗', t):
        return False
    if is_protected(t):
        return False

    # 承诺 + 明确时间/对象
    if re.search(r'(答应|承诺|说好|约好|约定|保证)[^。]{0,16}'
                 r'(你|我|他|她|明天|后天|下周|周末|月底|\d{1,2}\s*号|\d{1,2}\s*月)', t):
        return True
    # 生日/纪念日 + 具体日期
    if re.search(r'(生日|纪念日)[^。]{0,8}(\d{1,2}\s*月|\d{1,2}\s*号|\d{1,2}/\d{1,2})', t):
        return True
    # 亲属/重要的人 + 具体信息
    if re.search(r'(我姐|我妈|我爸|我哥|我弟|我妹|姑姑|外婆|奶奶|爷爷|舅舅|姨)[^。]{0,10}'
                 r'(叫|名字|是|住|在|做|上班|给|送|病|生日|充|给了)', t):
        return True
    # 亲属 + 金额
    if re.search(r'(姑姑|妈|姐|哥|弟|妹|爸|外婆|奶奶)[^。]{0,10}(\d+\s*块|充|送了)', t):
        return True
    # 借还/转账 + 金额
    if re.search(r'(借|还|欠|充|转|给)[^。]{0,8}\d+\s*(块|元|块钱)', t):
        return True
    # 账号密码
    if re.search(r'(QQ|微信|账号|密码)[^。]{0,8}\d{5,}', t):
        return True
    return False


def is_protected(text):
    return any(p.search(text) for p in PROTECT)


def load():
    rows = []
    for line in io.open(ARCH, encoding='utf-8', errors='ignore'):
        m = LINE.match(line.strip())
        if not m:
            continue
        md, dd, hh, mi, who, text = m.groups()
        text = text.strip()
        if not text or len(text) < 4:
            continue
        rows.append({
            'day': f'{md}-{dd}', 'time': f'{hh}:{mi}',
            'who': clean_name(who), 'text': text,
            'protected': is_protected(text),
        })
    return rows


def api_key():
    d = json.load(io.open(CFG, encoding='utf-8-sig'))
    for s in d.get('provider_sources') or []:
        if s.get('id') == 'deepseek':
            k = s.get('key')
            if isinstance(k, list) and k:
                return k[0]
            if isinstance(k, str) and k.startswith('sk-'):
                return k
    return ''


def compress(day, who, items, surface=''):
    # 每行必须标出谁说的：分组内只有"对方"和"我"两种
    lines = []
    _is_group = (surface or '').startswith('群')
    for it in items:
        if _is_group:
            # 群里保留每个说话人的名字，别糊成"几个人"
            nm = (it.get('who') or '').strip()
            nm = QQ_IN.sub('', nm).strip(' ()（）')
            lines.append(f"{it['time']} {nm or '某人'}：{it['text']}")
        else:
            side = '我' if (it['who'] or '').startswith('对方(') or KNOWN.get(QQ_IN.search(it['who'] or '').group(1) if QQ_IN.search(it['who'] or '') else '', '') == '我' or it['who'].startswith('我(') else '对方'
            lines.append(f"{it['time']} {side}：{it['text']}")
    body = '\n'.join(lines)
    _where = ('在群里和' if surface.startswith('群') else ('电脑上跟' if surface == '电脑端' else '私聊里和')) if surface else '和'
    _who_txt = '群里这些人' if surface.startswith('群') else who
    prompt = f"""下面是「机器人」{day} {_where}「{_who_txt}」的全部对话。

把它压成一小段回忆（第一人称，我＝机器人）。要求：
1. 保留：时间（上午/下午/几点）、谁做了什么、聊的核心话题和结论、1~2 句有信息量的原话、情绪
2. 群里出现的每个人都要用原文里的名字（去掉括号和 QQ 号），不要写“几个人”“有人”；
   私聊时对方用「{_who_txt}」；我自己一律用「我」，不要用「你」
3. 按时间顺序，不要逐条罗列
4. 300 字以内，只输出正文

对话：
{body[:8000]}"""
    data = json.dumps({'model': 'deepseek-chat',
                       'messages': [{'role': 'user', 'content': prompt}],
                       'temperature': 0.6, 'max_tokens': 500}).encode()
    req = urllib.request.Request('https://api.deepseek.com/chat/completions', data=data,
                                 headers={'Content-Type': 'application/json',
                                          'Authorization': 'Bearer ' + api_key()})
    r = json.loads(urllib.request.urlopen(req, timeout=120).read().decode())
    return ((r.get('choices') or [{}])[0].get('message', {}).get('content', '') or '').strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--samples', type=int, default=2)
    ap.add_argument('--min-days', type=int, default=0, help='只看这么多天以前的')
    args = ap.parse_args()

    rows = load()
    prot = [r for r in rows if r['protected']]
    print(f'归档 {len(rows)} 条，其中保护类 {len(prot)} 条（{len(prot)*100//max(len(rows),1)}%）')
    print('保护类示例：')
    for r in prot[:3]:
        print(f"  {r['day']} {r['who']} | {r['text'][:70]}")

    by = collections.defaultdict(list)
    for r in rows:
        if r['protected']:
            continue
        by[(r['day'], r['who'])].append(r)

    print(f'\n可蒸馏的包：{len(by)} 个')
    keys = [k for k, v in by.items() if 10 <= len(v) <= 40]
    random.seed(7)
    for day, who in random.sample(keys, min(args.samples, len(keys))):
        items = sorted(by[(day, who)], key=lambda x: x['time'])
        orig = sum(len(i['text']) for i in items)
        if args.dry_run and orig < 400:
            print(f'\n=== 样本：{day} · {who} · {len(items)} 条 / {orig} 字 —— 太短，不压，保留原文 ===')
            continue
        print(f'\n=== 样本：{day} · {who} · {len(items)} 条 / {orig} 字 ===')
        for it in items[:4]:
            print(f"  {it['time']} {it['text'][:56]}")
        if len(items) > 4:
            print(f'  …还有 {len(items)-4} 条')
        if not args.dry_run:
            continue
        try:
            s1 = compress(day, who, items)
        except Exception as e:
            print('  模型调用失败:', e)
            continue
        print(f'\n--- S1（{len(s1)} 字，{orig/max(len(s1),1):.1f}:1）---')
        print(f'  {s1}')


if __name__ == '__main__':
    main()
