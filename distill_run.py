# -*- coding: utf-8 -*-
"""记忆模糊化执行器（全阶段）。

S0 原文 → S1 一小段 → S2 几句话 → S3 一句主题 → S4 删除

用法：
    python distill_run.py                      # dry-run：只打印计划
    python distill_run.py --samples 2          # 附带样本
    python distill_run.py --apply --limit 5    # 只处理 5 个包
    python distill_run.py --purge              # 删缓冲期已过的原文
"""
import argparse
import collections
import hashlib
import importlib.util
import io
import json
import os
import random
import subprocess
import sys
import time

MEM = os.environ.get('MEM_DIR', './data/memory')
ARCH = os.path.join(MEM, '归档.md')
STATE = os.path.join(MEM, '记忆状态.json')
COLD = os.path.join(MEM, '过渡区')
FORGET = os.path.join(MEM, '忘掉的.md')
TOMB = os.path.join(MEM, '索引', '墓碑.json')

BASE = 21.0
GAPS = {1: 45.0, 2: 60.0, 3: 120.0}
CACHE_DAYS = 90      # 压缩缓存保留天数
DAY = 86400.0

spec = importlib.util.spec_from_file_location('dz', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'distill.py'))
dz = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dz)


def h_of(day, who, text):
    return hashlib.md5((day + who.strip()[-12:] + text[:120]).encode('utf-8')).hexdigest()[:16]


def interval_of(n):
    """间隔取整（天）"""
    return round(BASE * (1.5 ** min(n, 5)))


def ratio_of(n):
    return min(0.75, 1 / 3 + n / 12.0)


def proc_of(n):
    """处理期取整（天）"""
    return round(interval_of(n) * ratio_of(n))


def load_state():
    if os.path.exists(STATE):
        try:
            return json.load(io.open(STATE, encoding='utf-8')) or {}
        except Exception:
            pass
    return {}


def save_state(st):
    """原子写：先写临时文件再替换，避免写到一半崩溃把状态写坏"""
    tmp = STATE + '.tmp'
    io.open(tmp, 'w', encoding='utf-8').write(json.dumps(st, ensure_ascii=False, indent=0))
    os.replace(tmp, STATE)


def rewrite_archive(drop_hashes):
    """按哈希删行；写之前重新读一遍，避免把这段时间新追加的聊天记录冲掉"""
    keep = []
    for l in io.open(ARCH, encoding='utf-8', errors='ignore').read().splitlines():
        m = dz.LINE.match(l.strip())
        if m:
            h = h_of(f'{m.group(1)}-{m.group(2)}', m.group(5).strip(), m.group(6).strip())
            if h in drop_hashes:
                continue
        keep.append(l)
    tmp = ARCH + '.tmp'
    io.open(tmp, 'w', encoding='utf-8').write('\n'.join(keep) + '\n')
    os.replace(tmp, ARCH)


def read_rows():
    rows = []
    for line in io.open(ARCH, encoding='utf-8', errors='ignore'):
        m = dz.LINE.match(line.strip())
        if not m:
            continue
        day = f'{m.group(1)}-{m.group(2)}'
        who = dz.clean_name(m.group(5))
        text = m.group(6).strip()
        if not text:
            continue
        _surface, _who2 = dz.split_surface(m.group(5))
        rows.append({'h': h_of(day, who, text), 'day': day, 'surface': _surface,
                     'time': m.group(3) + ':' + m.group(4), 'who': who, 'label': m.group(5).strip(),
                     'text': text, 'line': line.rstrip('\n')})
    # 私聊里我自己发的话，归到当天最近一次私聊对象
    _last_peer = {}
    for r in rows:
        _s = r.get('surface') or ''
        _is_me = r['who'].startswith('我(') or '机器人' in (r.get('label') or '')
        if _s == '私聊' and _is_me:
            peer = _last_peer.get(r['day'])
            if peer:
                r['who'] = peer
        elif _s == '私聊':
            _last_peer[r['day']] = r['who']
    return rows


def pack_key(row):
    """打包键：私聊/电脑端按人拆；群按某天+某个群合并多人。"""
    surface = row.get('surface', '') or ''
    if surface.startswith('群'):
        return (row['day'], surface, '')      # 群：同一天同一个群，多人合并
    if not surface:
        return (row['day'], '电脑端', '')       # 电脑端：主人和我在同一个会话
    return (row['day'], surface, row['who'])   # 私聊：按人拆


def next_gap(stage):
    return GAPS.get(stage, 0.0)


def compress_piece(day, who, items, level, surface=''):
    """level=0 → S1；level=1 → S2；level=2 → S3"""
    if level == 0:
        return dz.compress(day, who, items, surface)
    body = '\n'.join(f"{i['time']} {i['text']}" for i in items)
    if level == 1:
        ask = ('压成两三句话：保留谁做了什么、定了什么；丢掉过程细节和语气。'
               '称呼沿用原文里的写法，不要出现「你」。150 字以内，只输出正文。')
    else:
        ask = ('压成一句主题：只留"什么时候 + 在做什么"，不带人名细节，'
               '30 字以内，只输出正文。')
    prompt = f'下面是「机器人」在 {day} 和「{who}」的一段回忆或对话。\n{ask}\n\n{body[:6000]}'
    import urllib.request
    data = json.dumps({'model': 'deepseek-chat',
                       'messages': [{'role': 'user', 'content': prompt}],
                       'temperature': 0.5, 'max_tokens': 300}).encode()
    req = urllib.request.Request('https://api.deepseek.com/chat/completions', data=data,
                                 headers={'Content-Type': 'application/json',
                                          'Authorization': 'Bearer ' + dz.api_key()})
    r = json.loads(urllib.request.urlopen(req, timeout=120).read().decode())
    return ((r.get('choices') or [{}])[0].get('message', {}).get('content', '') or '').strip()


def save_facts(rows):
    """被判定为事实（约定/承诺/亲属/生日/金额/账号）的原文，追加进长期事实。"""
    path = os.path.join(MEM, '事实库.md')
    try:
        txt = io.open(path, encoding='utf-8', errors='ignore').read()
    except OSError:
        txt = ''
    add = []
    for r in rows:
        if not dz.is_fact(r['text']) or dz.is_protected(r['text']):
            continue
        key = r['text'][:20]
        if key and key in txt:
            continue
        add.append(f"- [{r['day']}] {r['who']}：{r['text'].strip()[:80]}")
    if not add:
        return 0
    with io.open(path, 'a', encoding='utf-8') as f:
        if not txt:
            f.write('# 机器人 · 事实库（不进注入，只进检索）\n\n')
        f.write('\n'.join(add) + '\n')
    return len(add)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--purge', action='store_true')
    ap.add_argument('--samples', type=int, default=0)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--index', action='store_true', help='跑完顺手更新向量索引（增量）')
    args = ap.parse_args()
    now = time.time()

    st = load_state()
    rows = read_rows()

    # ---------- purge：删缓冲期已过的原文 ----------
    if args.purge:
        due = [k[8:] for k, v in st.items() if k.startswith('pending:') and v <= now]
        if not due:
            print('没有到期的待删原文')
            return
        due_set = set(due)
        keep = [r['line'] for r in rows if r['h'] not in due_set]
        io.open(ARCH, 'w', encoding='utf-8').write('\n'.join(keep) + '\n')
        tomb = []
        if os.path.exists(TOMB):
            try:
                tomb = json.load(io.open(TOMB, encoding='utf-8')) or []
            except Exception:
                tomb = []
        tomb.extend(due)
        io.open(TOMB, 'w', encoding='utf-8').write(json.dumps(tomb, ensure_ascii=False))
        for h in due:
            st.pop('pending:' + h, None)
        save_state(st)
        print(f'purge：删掉 {len(due)} 条原文，已记墓碑（共 {len(tomb)} 条）')
        return

    # ---------- 计划 ----------
    plan = collections.defaultdict(list)   # stage -> rows
    active_skip = prot = 0
    for r in rows:
        # 派生出来的回忆/主题行，不再参与压缩（它们由阶段逻辑单独处理）
        if '回忆' in r['who'] or '主题' in r['who']:
            continue
        if st.get('pending:' + r['h']):
            continue          # 原文已压缩，等缓冲期到了删，别再压一遍
        rec = st.get(r['h']) or {}
        stage = int(rec.get('stage', 0))
        n = int(rec.get('n', 0))
        if rec.get('active'):
            # 冷却够了就解冻；否则被提过一次的条目会永久冻结、再也不压不删
            _nd = float(rec.get('nextDue') or 0)
            if now < _nd:
                active_skip += 1
                continue
            rec['active'] = False
            st[r['h']] = rec
        if dz.is_protected(r['text']) or dz.is_fact(r['text']):
            prot += 1
            continue
        last = float(rec.get('lastHit') or 0)
        if not last:
            y = time.localtime().tm_year
            last = time.mktime((y, int(r['day'][:2]), int(r['day'][3:]), 0, 0, 0, 0, 0, -1))
        if stage == 0:
            due = last + proc_of(n) * DAY
            rec_nd = float(rec.get('nextDue') or 0)
            if rec_nd:
                due = max(due, rec_nd)
        else:
            due = last + next_gap(stage) * DAY
        if now >= due:
            plan[stage].append(r)

    print(f'归档 {len(rows)} 条')
    if args.apply:
        nf = save_facts(rows)
        if nf:
            print(f'  事实类追加进长期事实：{nf} 条')
    for s in sorted(plan):
        print(f'  S{s} → S{s+1}：{len(plan[s])} 条到期')
    print(f'  活跃跳过 {active_skip}，保护类 {prot}')

    if not args.apply:
        if args.samples and plan[0]:
            packs = collections.defaultdict(list)
            for r in plan[0]:
                packs[pack_key(r)].append(r)
            packs = {k: v for k, v in packs.items() if sum(len(x['text']) for x in v) >= 400}
            random.seed(5)
            for k in random.sample(list(packs), min(args.samples, len(packs))):
                if k[1] == '电脑端':
                    who_label = '对方(1000000001)'
                elif (k[1] or '').startswith('群'):
                    who_label = '群里几个人'
                else:
                    who_label = k[2]
                print(f'\n=== 样本 {k[0]} · {k[1] or "电脑端"} · {who_label} ===')
                print(dz.compress(k[0], who_label, sorted(packs[k], key=lambda x: x['time']), k[1])[:400])
        print('\n（dry-run，没动任何东西）')
        return

    # ---------- 恢复：被提起的回忆，把原文放回来 ----------
    to_restore = [h for h, v in st.items() if isinstance(v, dict) and v.get('restore')]
    if to_restore:
        done_r = 0
        _mem_hash = {}
        for l in io.open(ARCH, encoding='utf-8', errors='ignore').read().splitlines():
            mm = dz.LINE.match(l.strip())
            if not mm:
                continue
            lab = mm.group(5).strip()
            if '回忆·' not in lab and '主题·' not in lab:
                continue
            _h = h_of(f'{mm.group(1)}-{mm.group(2)}', lab.split('·', 1)[-1] if '·' in lab else lab,
                      mm.group(6).strip())
            _mem_hash[_h] = l
        for mh in to_restore:
            rec = st.get(mh) or {}
            orig = rec.get('orig') or []
            if not orig:              # 原文这时候本来就没了，别把这条回忆也删掉
                rec.pop('restore', None)
                continue
            alive = [o for o in orig if ('pending:' + o) in st]
            if alive:
                _n = max(1, int((rec or {}).get('n', 0)))      # 保留原来被提起的次数
                _iv, _rt = interval_of(_n), ratio_of(_n)
                for o in orig:
                    if ('pending:' + o) in st:
                        st.pop('pending:' + o, None)
                        st[o] = {'stage': 0, 'n': _n, 'interval': _iv, 'ratio': _rt,
                                 'proc_days': proc_of(_n), 'lastHit': int(now),
                                 'nextDue': int(now + proc_of(_n) * DAY)}
                done_r += 1
            st.pop(mh, None)
        _drop_lines = {_mem_hash[h] for h in to_restore if h in _mem_hash}
        if _drop_lines:
            _hs = set()
            for _l in _drop_lines:
                _mm = dz.LINE.match(_l.strip())
                if _mm:
                    _hs.add(h_of(f'{_mm.group(1)}-{_mm.group(2)}', _mm.group(5).strip(), _mm.group(6).strip()))
            rewrite_archive(_hs)
        save_state(st)
        print(f'恢复：{done_r} 个包回到 S0（整包原文全部恢复，回忆行已删）')

    # ---------- 执行 ----------
    os.makedirs(COLD, exist_ok=True)
    cold_path = os.path.join(COLD, time.strftime('%Y-%m') + '.md')
    done = 0

    # S0 → S1：每人每天一包
    if plan[0]:
        packs = collections.defaultdict(list)
        for r in plan[0]:
            packs[pack_key(r)].append(r)
        # 只剩我自己说话的私聊包：并到当天最大的私聊包，别单独压
        for k in list(packs):
            day_, surface_, who_ = k
            if surface_ == '私聊' and (not who_ or who_.startswith('我(') or '机器人' in who_):
                sib = [(kk, vv) for kk, vv in packs.items()
                       if kk[0] == day_ and kk[1] == '私聊' and kk != k]
                if sib:
                    tgt = max(sib, key=lambda kv: len(kv[1]))[0]
                    packs[tgt].extend(packs.pop(k))
        packs = {k: v for k, v in packs.items()
                 if sum(len(x['text']) for x in v) >= 400}
        todo = list(packs.items())
        if args.limit:
            todo = todo[:args.limit]
        with io.open(ARCH, 'a', encoding='utf-8') as fa:
            for (day, surface, who), items in todo:
                items = sorted(items, key=lambda x: x['time'])
                _who = who or ('对方(1000000001)' if surface == '电脑端' else '群里几个人')
                # 单个包太大就按 6000 字切块，避免被截断
                chunks, cur, acc = [], [], 0
                for it in items:
                    ln = len(it['text'])
                    if cur and acc + ln > 6000:
                        chunks.append(cur); cur, acc = [], 0
                    cur.append(it); acc += ln
                if cur:
                    chunks.append(cur)
                for _ci, chunk in enumerate(chunks, 1):
                    import hashlib as _hl
                    _sig = _hl.md5('\n'.join(x['text'] for x in chunk).encode('utf-8')).hexdigest()[:16]
                    _cached = st.get('cache:' + _sig) or {}
                    if _cached.get('t') and len(_cached['t']) >= 20:
                        s1 = _cached['t']
                        print(f'  复用缓存：{day} {_who} ({len(chunk)} 条)')
                    else:
                        s1 = compress_piece(day, _who, chunk, 0, surface)
                        if len(s1) >= 20:
                            st['cache:' + _sig] = {'t': s1, 'at': int(now)}
                    if len(s1) < 20:
                        continue
                    _label = (surface + '·' + _who) if surface else _who
                    if len(chunks) > 1:
                        _label += f'·{_ci}/{len(chunks)}'
                    fa.write(f'- **{day} {chunk[0]["time"]} · 回忆·{_label}**：{s1}\n')
                    hs = [it['h'] for it in chunk]
                    new_h = h_of(day, _label, s1)
                    for h in hs:
                        st.pop(h, None)
                        st['pending:' + h] = int(now + interval_of(0) * DAY)
                    st[new_h] = {'stage': 1, 'n': 0, 'interval': interval_of(0),
                                 'ratio': ratio_of(0), 'proc_days': ratio_of(0) * interval_of(0),
                                 'lastHit': int(now), 'nextDue': int(now + next_gap(1) * DAY),
                                 'from': len(hs), 'orig': hs}
                    done += 1
                    save_state(st)
                continue
                with io.open(cold_path, 'a', encoding='utf-8') as fc:
                    for it in items:
                        fc.write(it['line'] + '\n')
                if args.limit and done >= args.limit:
                    break

    # S1 → S2 / S2 → S3：按 日期 + 场合 + 人 合并，并删掉被替换的上一级回忆
    _drop = set()
    for stage in (1, 2):
        if not plan[stage]:
            continue
        groups = collections.defaultdict(list)
        for r in plan[stage]:
            surface, who = dz.split_surface(r.get('label', ''))
            if not who:
                who = r['who']
            groups[(r['day'], surface, who)].append(r)
        for (day, surface, who), items in list(groups.items()):
            items = sorted(items, key=lambda x: x['time'])
            s = compress_piece(day, who, items, stage, surface)
            if len(s) < 8:
                continue
            level = '回忆' if stage == 1 else '主题'
            _label = (surface + '·' + who) if surface else who
            with io.open(ARCH, 'a', encoding='utf-8') as fa:
                fa.write(f'- **{day} {items[0]["time"]} · {level}·{_label}**：{s}\n')
            for it in items:
                _drop.add(it['line'])                 # 上一级那行要删掉，别越压越多
                st.pop(it['h'], None)
                st.pop('pending:' + it['h'], None)
            new_h = h_of(day, _label, s)
            st[new_h] = {'stage': stage + 1, 'n': 0,
                         'interval': interval_of(0), 'ratio': ratio_of(0),
                         'proc_days': ratio_of(0) * interval_of(0),
                         'lastHit': int(now),
                         'nextDue': int(now + next_gap(stage + 1) * DAY),
                         'from': len(items)}
            done += 1
        save_state(st)

    # S3 → S4：删除并记目录
    if plan[3]:
        victims = plan[3]
        with io.open(FORGET, 'a', encoding='utf-8') as f:
            f.write(f"- **{time.strftime('%Y-%m-%d')}** 忘了 {len(victims)} 条\n")
        hs = {v['h'] for v in victims}
        rewrite_archive(hs)
        tomb = []
        if os.path.exists(TOMB):
            try:
                tomb = json.load(io.open(TOMB, encoding='utf-8')) or []
            except Exception:
                tomb = []
        tomb.extend(sorted(hs))
        io.open(TOMB, 'w', encoding='utf-8').write(json.dumps(tomb, ensure_ascii=False))
        for v in victims:
            st.pop(v['h'], None)
        save_state(st)
            # 过渡区里的副本也要删，否则原文只是搬了个地方，根本没忘
        try:
            for _f in os.listdir(COLD):
                if not _f.endswith('.md'):
                    continue
                _p = os.path.join(COLD, _f)
                _keep = []
                _ch = 0
                for _l in io.open(_p, encoding='utf-8', errors='ignore').read().splitlines():
                    _mm = dz.LINE.match(_l.strip())
                    if _mm:
                        _h = h_of(f'{_mm.group(1)}-{_mm.group(2)}', _mm.group(5).strip(), _mm.group(6).strip())
                        if _h in hs:
                            _ch += 1
                            continue
                    _keep.append(_l)
                if _ch:
                    io.open(_p, 'w', encoding='utf-8').write('\n'.join(_keep) + '\n')
                    print(f'  过渡区 {_f} 清掉 {_ch} 条')
        except Exception as _e:
            print(f'  过渡区清理失败：{_e}')

    print(f'S3→S4：删掉 {len(victims)} 条')

    if _drop:
        _hs2 = set()
        for _l in _drop:
            _mm = dz.LINE.match(_l.strip())
            if _mm:
                _hs2.add(h_of(f'{_mm.group(1)}-{_mm.group(2)}', _mm.group(5).strip(), _mm.group(6).strip()))
        rewrite_archive(_hs2)
        print(f'删掉被替换的上一级回忆 {len(_drop)} 行')

    # 缓存只留 180 天，别无限长
    for _k in [k for k, v in st.items()
               if k.startswith('cache:') and isinstance(v, dict)
               and now - (v.get('at') or 0) > CACHE_DAYS * DAY]:
        st.pop(_k, None)

    save_state(st)
    print(f'本轮处理 {done} 个包')

    if args.index and done:
        import subprocess
        try:
            subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rag_index.py')],
                           timeout=3600, capture_output=True)
            print('索引已更新')
        except Exception as e:
            print('索引更新失败:', e)


if __name__ == '__main__':
    main()
