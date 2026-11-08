# memory-system · 会遗忘的记忆

给聊天机器人装一套**会遗忘**的记忆：记得住重要的，也忘得掉没用的。

<p>
  <img alt="python" src="https://img.shields.io/badge/python-3.10%2B-blue">
  <img alt="astrbot" src="https://img.shields.io/badge/AstrBot-4.x-green">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-black">
</p>

## 它解决什么

机器人没有记忆：要么全塞上下文（越聊越贵），要么什么都不记（每次都像第一次聊天）。
这个项目做的是第三种——**像人一样记，也像人一样忘**。

- 每轮只注入约 3,000 字：长期事实 + 最近 7 天摘要
- 旧事按需检索：关键词 + 语义，命中才注入
- 久了没人提的事：压短、再压短、最后删除
- 任何一次被提起：回退到原文，重新计时

## 效果（真实跑出来的数）

| 指标 | 数值 |
|---|---|
| 归档原文 | 19,329 条 |
| 压缩后回忆 | 89 段（压缩比 10.7:1） |
| 单次输入 | 平均 44,891 token（此前峰值 20 万） |
| 每日维护成本 | 0.02~0.04 元 |

## 四层结构

```
热层    长期事实 + 近 7 天摘要     每轮注入（≤3,000 字）
检索    关键词 + 语义              命中才注入 8 条
原文    归档.md                   一行一条
模糊化  久了没人提 → 压短 → 删除   逐级衰减
```

## 遗忘节奏

```
原文 21 天没人提   → 压成一小段（约 10:1）
再过 45 天          → 压成几句话
再过 60 天          → 压成一句主题
再过 120 天         → 删除，只留一行目录
```

被提起就回退，间隔按 `21 × 1.5ⁿ` 延长，处理期按 `1/3 + n/12`（封顶 3/4）延长。

## 安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/wuwugi/memory-system.git memory_system
pip install -r memory_system/requirements.txt   # numpy，fastembed 可选
cp memory_system/config.example.json memory_system/config.json
```

编辑 `config.json`：

```json
{
  "data_dir": "./data/memory",
  "model": {"api_key": "sk-...", "model": "deepseek-chat"}
}
```

重启 AstrBot，日志出现下面两行就正常：

```
[记忆] 已启动
[记忆] 已挂上「注入不落库」钩子
```

第二行如果是 warning：注入照常，历史靠每 30 分钟清理兜底。

## 目录

```
main.py               AstrBot 适配：写入、注入、钩子、每日维护
store.py            归档写入（哈希、去重）
hot.py               热层（长期事实 / 近 7 天摘要）
search.py            检索（关键词 + 语义，可退化）
distill.py           压缩与保护类判断
distill_run.py       状态机：分包、到期、恢复、删除
rag_index.py         向量索引
docs/                 安装 / 使用说明 / 注意事项
demo.md      示例数据（虚构）
```

## 配置

| 键 | 说明 | 默认 |
|---|---|---|
| `data_dir` | 记忆目录 | `./data/memory` |
| `model.api_key` | 压缩用；留空则不压缩 | 空 |
| `search.top_k` | 每次注入几条 | 8 |
| `search.enable_semantic` | 是否用向量检索 | true |
| `distill.pack_min_chars` | 小于这个字数不压 | 400 |

## 手动跑

```bash
cd data/memory
python ../../plugins/memory_system/distill_run.py                # 预演
python ../../plugins/memory_system/distill_run.py --apply --limit 3
python ../../plugins/memory_system/distill_run.py --purge
```

## 注意

- 记忆全在本地 markdown，不上传
- 压缩时对话正文会发给模型厂商，这是唯一出网点；在意隐私就把 `api_key` 留空
- 会丢细节：压到最后只剩一句主题；想保住的写进 `长期事实.md`
- 恢复是延迟生效（下一次维护），不是检索当场

详见 [注意事项.md](注意事项.md)。

## License

MIT
