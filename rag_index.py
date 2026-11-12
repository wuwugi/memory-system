# -*- coding: utf-8 -*-
"""给记忆建/更新语义索引（本地 CPU，不联网、不花钱）。

产物（都在 记忆/索引/ 下）：
  vectors.npy   每行一条记忆的向量（float32）
  meta.jsonl    每行的元数据（日期/说话人/原文）
  state.json    已处理的条目哈希，用来增量更新

用法：
  python rag_index.py          增量更新（同步脚本里每分钟调一次也行，很快）
  python rag_index.py --rebuild 全量重建
"""
import hashlib
import io
import re as _re
import json
import os
import sys
import time

import os as _os0
_os0.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import numpy as np

ROOT = os.environ.get("MEM_DIR", "./data/memory")
MEM = ROOT if os.path.basename(os.path.normpath(ROOT)) not in ("记忆",) and os.path.basename(os.path.normpath(ROOT)) != "" else os.path.join(ROOT, "记忆")
IDX = os.path.join(MEM, "索引")
SHARED = os.path.join(MEM, "共享记忆.md")
ARCHIVE = os.path.join(MEM, "归档.md")
TECH = os.path.join(MEM, "技术记录.md")

VEC = os.path.join(IDX, "vectors.npy")
META = os.path.join(IDX, "meta.jsonl")
STATE = os.path.join(IDX, "state.json")

MODEL = "BAAI/bge-small-zh-v1.5"
MAX_LEN = 200          # 单条喂给模型的字数

_model = None


def embedder():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        _model = TextEmbedding(model_name=MODEL)
    return _model


def parse(line):
    import re
    m = re.match(r"- \*\*(\d\d-\d\d) (\d\d:\d\d) · ([^*]+)\*\*：(.*)", line)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3).strip(), m.group(4).strip()


def watch_entries():
    """看过的视频/图片记录：每份 md 作为一条记忆，方便她"记得自己看过"。"""
    import glob as _g
    d = os.path.join(MEM, "看过的")
    if not os.path.isdir(d):
        return []
    out = []
    for p in sorted(_g.glob(os.path.join(d, "*.md"))):
        try:
            txt = io.open(p, encoding="utf-8", errors="ignore").read().strip()
        except OSError:
            continue
        if not txt:
            continue
        title = os.path.splitext(os.path.basename(p))[0]
        day = time.strftime("%m-%d", time.localtime(os.path.getmtime(p)))
        body = txt.replace("\n", " ")[:400]
        h = hashlib.md5((p + body[:80]).encode("utf-8")).hexdigest()[:16]
        out.append({"h": h, "d": day, "m": "00:00", "s": "看过的·" + title[:12],
                    "t": (title + "：" + body)[:MAX_LEN], "k": "watch"})
    return out


def _norm_sender(sender):
    """和 distill_run 用同一个哈希口径：去掉 群·/私聊· 前缀、不可见字符，QQ 统一成 名(QQ)。"""
    try:
        import importlib.util as _iu
        _sp = _iu.spec_from_file_location('_dz', os.path.join(os.path.dirname(__file__), 'distill.py'))
        _dz = _iu.module_from_spec(_sp)
        _sp.loader.exec_module(_dz)
        return _dz.clean_name(sender)
    except Exception:
        return sender


def entries():
    out = []
    POSTS = os.path.join(MEM, "说说存档.md")
    FACTS_LIB = os.path.join(MEM, "事实库.md")
    for path, kind in ((SHARED, "share"), (ARCHIVE, "archive"), (TECH, "tech"),
                       (POSTS, "post"), (FACTS_LIB, "fact")):
        if not os.path.exists(path):
            continue
        try:
            lines = io.open(path, encoding="utf-8", errors="ignore").read().splitlines()
        except OSError:
            continue
        for l in lines:
            if kind == "fact":
                # 事实库行格式：- [MM-DD] 谁：内容
                _m = _re.match(r"^- \[(\d\d-\d\d)\]\s*([^：:]{1,40})[：:]\s*(.+)$", l)
                if not _m:
                    continue
                d, hm, sender, text = _m.group(1), "00:00", _m.group(2), _m.group(3)
            else:
                if not l.startswith("- **"):
                    continue
                p = parse(l)
                if not p:
                    continue
                d, hm, sender, text = p
            text = text.strip()
            if len(text) < 3:
                continue
            _sn = _norm_sender(sender)
            h = hashlib.md5((d + hm + text[:120]).encode("utf-8")).hexdigest()[:16]
            out.append({"h": h, "d": d, "m": hm, "s": sender[-14:], "t": text[:MAX_LEN], "k": kind})
    out.extend(watch_entries())
    out.extend(summary_entries())
    return out



def summary_entries():
    """日摘要本身也进向量库（k=summary），去重 key 用日期。"""
    out = []
    d = os.path.join(ROOT, "记忆", "日摘要")
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".md"):
            continue
        day = fn[:-3]
        p2 = os.path.join(d, fn)
        try:
            txt = io.open(p2, encoding="utf-8", errors="ignore").read().strip()
        except OSError:
            continue
        if len(txt) < 20:
            continue
        h = hashlib.md5(("summary" + day).encode("utf-8")).hexdigest()[:16]
        out.append({"h": h, "d": day, "m": "23:59", "s": "日摘要", "t": txt[:MAX_LEN], "k": "summary"})
    return out


def load_state():
    try:
        return json.load(io.open(STATE, encoding="utf-8"))
    except Exception:
        return {"seen": []}


def save_state(st):
    os.makedirs(IDX, exist_ok=True)
    io.open(STATE, "w", encoding="utf-8").write(json.dumps(st, ensure_ascii=False))


def load_index():
    if not (os.path.exists(VEC) and os.path.exists(META)):
        return np.zeros((0, 0), dtype=np.float32), []
    vecs = np.load(VEC)
    metas = [json.loads(l) for l in io.open(META, encoding="utf-8", errors="ignore").read().splitlines() if l.strip()]
    return vecs, metas


def main():
    if "--rebuild" in __import__("sys").argv:
        for _f in (META, VEC, STATE):
            try:
                os.remove(_f)
            except OSError:
                pass
    """重建前先清空索引，否则 --rebuild 会在旧索引上追加，条目翻倍。"""
    rebuild = "--rebuild" in sys.argv
    os.makedirs(IDX, exist_ok=True)
    all_items = entries()
    st = load_state()
    seen = set() if rebuild else set(st.get("seen") or [])
    # rebuild 之后 state 是空的；如果此时增量任务并发跑，会把整库再追加一遍
    # （2026-11-12 索引翻倍就是这个原因）。所以把已有 META 的哈希也算进 seen。
    if not rebuild:
        try:
            if os.path.exists(META):
                for _l in io.open(META, encoding="utf-8", errors="ignore"):
                    if _l.strip():
                        _m = json.loads(_l)
                        if _m.get("h"):
                            seen.add(_m["h"])
        except Exception:
            pass
    # 同一轮里同 hash 只能进一条（否则 8 行相同文本会全部入库）
    _uniq = {}
    for _x in all_items:
        _uniq.setdefault(_x["h"], _x)
    all_items = list(_uniq.values())
    todo = [x for x in all_items if x["h"] not in seen]
    if not todo:
        print("[索引] 没有新条目")
        return
    print(f"[索引] 待处理 {len(todo)} 条（已有 {len(seen)} 条）")
    try:
        model = embedder()
    except Exception as e:
        print(f"[索引] 没装 fastembed（{type(e).__name__}），跳过语义索引；"
              f"检索会退化为关键词匹配。装法：pip install fastembed")
        return
    texts = [x["t"] for x in todo]
    vecs = np.array(list(model.embed(texts, batch_size=32)), dtype=np.float32)
    # 归一化，后面用点积当余弦
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vecs = vecs / norms

    if rebuild:
        old_vecs, old_metas = np.zeros((0, vecs.shape[1]), dtype=np.float32), []
    else:
        old_vecs, old_metas = load_index()
        if old_vecs.shape[0] and old_vecs.shape[1] != vecs.shape[1]:
            old_vecs, old_metas = np.zeros((0, vecs.shape[1]), dtype=np.float32), []

    all_vecs = np.vstack([old_vecs, vecs]) if old_vecs.size else vecs
    all_metas = old_metas + todo
    np.save(VEC, all_vecs)
    with io.open(META, "w", encoding="utf-8") as f:
        for m in all_metas:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    seen |= {x["h"] for x in todo}
    save_state({"seen": sorted(seen)})
    print(f"[索引] 完成：共 {len(all_metas)} 条，向量维度 {all_vecs.shape[1]}")


SELF_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rag_index.log")


if __name__ == "__main__":
    import traceback
    with io.open(SELF_LOG, "a", encoding="utf-8") as _lg:
        sys.stdout = _lg
        sys.stderr = _lg
        _lg.write("\n=== " + __import__("time").strftime("%m-%d %H:%M:%S") + " ===\n")
        try:
            main()
        except Exception:
            traceback.print_exc()

