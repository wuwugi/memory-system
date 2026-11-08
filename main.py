# -*- coding: utf-8 -*-
"""memory-system · 记忆体系（AstrBot 插件）

四层：写入 → 热层注入 → 检索 → 模糊化
本文件只做框架适配，核心逻辑在 core/ 下，平台无关。
"""
import asyncio
import io
import json
import os
import re
import time

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core import logger

try:
    from .core import store, hot, search
    from .core.distill_run import main as distill_main
except Exception:            # 平铺安装（文件都在插件根目录）
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    import store, hot, search
    from distill_run import main as distill_main

MARK_START = '<<<MEMORY>>>'
MARK_END = '<<<END_MEMORY>>>'

CFG_DEFAULT = {
    'data_dir': './data/memory',
    'model': {'api_key': '', 'base_url': 'https://api.deepseek.com', 'model': 'deepseek-chat'},
    'search': {'top_k': 8, 'enable_semantic': True, 'daily_limit': 3},
    'distill': {'pack_min_chars': 400, 'chunk_max_chars': 6000, 'cache_days': 90},
    'people': {},
}


def _cfg(ctx):
    raw = {}
    try:
        raw = ctx.get_config() or {}
    except Exception:
        raw = {}
    out = json.loads(json.dumps(CFG_DEFAULT))
    for k, v in (raw or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def _strip_memory(text):
    """把注入块剥掉，免得它被存进历史。"""
    if not text or MARK_START not in text:
        return text
    return re.sub(re.escape(MARK_START) + r'.*?' + re.escape(MARK_END), '', text, flags=re.S).strip()


@register('memory_system', 'dsh', '会遗忘的记忆：热层注入 + 检索 + 逐级模糊化', '0.1.0')
class MemorySystem(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.cfg = _cfg(context)
        self.dir = os.path.abspath(self.cfg['data_dir'])
        os.makedirs(self.dir, exist_ok=True)
        store.ensure(self.dir)
        self._cache = {}
        self._patched = False
        self._no_hook = False
        self._task = None

    async def initialize(self):
        # 注入不落库：挂写库钩子，挂不上就靠定时清理兜底
        try:
            from astrbot.core.db.sqlite import SQLiteDatabase
            orig = SQLiteDatabase.update_conversation
            if not getattr(orig, '__memory_patched__', False):
                async def patched(conn_self, *a, **kw):
                    try:
                        if a and isinstance(a[-1], list):
                            for m in a[-1]:
                                if isinstance(m, dict) and isinstance(m.get('content'), str):
                                    m['content'] = _strip_memory(m['content'])
                        if 'history' in kw and isinstance(kw['history'], list):
                            for m in kw['history']:
                                if isinstance(m, dict) and isinstance(m.get('content'), str):
                                    m['content'] = _strip_memory(m['content'])
                    except Exception:
                        pass
                    return await orig(conn_self, *a, **kw)
                patched.__memory_patched__ = True
                SQLiteDatabase.update_conversation = patched
                self._patched = True
                logger.info('[记忆] 已挂上「注入不落库」钩子')
        except Exception as e:
            self._no_hook = True
            logger.warning(f'[记忆] 写库钩子没挂上，改用每 30 分钟清理历史：{e}')

        self._task = asyncio.create_task(self._loop())
        logger.info('[记忆] 已启动')

    async def terminate(self):
        if self._task:
            self._task.cancel()

    # ---------- 写 ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_msg(self, event: AstrMessageEvent):
        try:
            text = (event.message_str or '').strip()
            if not text:
                return
            umo = str(event.unified_msg_origin)
            where = '群' + umo.split(':')[-1] if 'GroupMessage' in umo else '私聊'
            name = event.get_sender_name() or '某人'
            uid = event.get_sender_id() or ''
            store.append(self.dir, where, f'{name}({uid})' if uid else name, text)
        except Exception as e:
            logger.warning(f'[记忆] 写入失败：{e}')

    # ---------- 注入 ----------
    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req):
        try:
            cfg = self.cfg
            parts = []
            facts = hot.facts(self.dir)
            if facts:
                parts.append('### 长期事实\n' + facts)
            warm = hot.recent(self.dir, days=7)
            if warm:
                parts.append('### 最近几天\n' + warm)
            q = (event.message_str or '').strip()
            if q:
                hits = search.query(self.dir, q, top=cfg['search']['top_k'],
                                    semantic=cfg['search']['enable_semantic'])
                if hits:
                    parts.append('### 相关旧事\n' + '\n'.join(hits))
            if not parts:
                return
            block = f'{MARK_START}\n' + '\n\n'.join(parts) + f'\n{MARK_END}'
            req.system_prompt = (req.system_prompt or '') + '\n\n' + block
        except Exception as e:
            logger.warning(f'[记忆] 注入失败：{e}')

    # ---------- 定时 ----------
    async def _loop(self):
        await asyncio.sleep(180)
        while True:
            try:
                await self._daily()
                if self._no_hook:
                    n = await asyncio.to_thread(self._clean_db)
                    if n:
                        logger.info(f'[记忆] 清理历史里的注入块：{n} 条会话')
            except Exception as e:
                logger.warning(f'[记忆] 维护失败：{e}')
            await asyncio.sleep(1800)

    def _clean_db(self):
        """钩子挂不上时的兜底：直接把历史里的注入块剥掉。"""
        import sqlite3
        db = self.cfg.get('db_path') or os.path.join(
            os.environ.get('ASTRBOT_ROOT', '.'), 'data', 'data_v4.db')
        if not os.path.exists(db):
            return 0
        n = 0
        try:
            con = sqlite3.connect(db)
            rows = con.execute(
                "select conversation_id, content from conversations where content like ?",
                (f'%{MARK_START}%',)).fetchall()
            for cid, content in rows:
                try:
                    msgs = json.loads(content) if isinstance(content, str) else content
                except Exception:
                    msgs = None
                if not isinstance(msgs, list):
                    continue
                changed = False
                for m in msgs:
                    if isinstance(m, dict) and isinstance(m.get('content'), str):
                        s = _strip_memory(m['content'])
                        if s != m['content']:
                            m['content'] = s
                            changed = True
                if changed:
                    con.execute('update conversations set content=? where conversation_id=?',
                                (json.dumps(msgs, ensure_ascii=False), cid))
                    n += 1
            con.commit()
            con.close()
        except Exception as e:
            logger.warning(f'[记忆] 清理历史失败：{e}')
        return n

    async def _daily(self):
        mark = os.path.join(self.dir, '索引', '上次.json')
        today = time.strftime('%Y-%m-%d')
        try:
            last = json.load(io.open(mark, encoding='utf-8')).get('d')
        except Exception:
            last = ''
        if last == today:
            return
        os.makedirs(os.path.dirname(mark), exist_ok=True)
        os.environ['MEM_DIR'] = self.dir
        import sys
        for args in (['distill_run', '--apply', '--limit', '20'],
                     ['distill_run', '--purge']):
            sys.argv = args
            await asyncio.to_thread(distill_main)
        io.open(mark, 'w', encoding='utf-8').write(json.dumps({'d': today}))
