# 本船事件中台

事件由 ingest / engine 收尾写入 `event_inbox`，本目录只消费、去重、派单、留痕。不新建定时器，不拉积加，不写广告后台，不直接往聊天发消息。

- 规则：`rules.yaml`（新增一类事件只改这个文件）
- 收件箱：`event_inbox`（P0 已建）
- 留痕：`dispatch_log`
- 待送达：`tell_outbox`（同一业务日同一动作只留一张；由船长送到本聊天）
- 台账投影：`python3 -m midship --project`（失败不回写数据库）

手动排空：`python3 -m midship`（在工程根目录）。

每次运行（除 `--dry-run`）会在 `run_log` 记一笔自己的账：`dataset='midship.project'`
（带投影）或 `'midship'`（只消费），`status` 为 `ok` / `failed`，`note` 里写消费条数
与写出文件数。链的 `step_done` 读的就是这一行 —— 不读 `chain_run`，因为链是先复验
再 record，第一次成功也会被误判成失败。

## 在链上是什么位置（2026-11-24 补）

`adsdesk/chain.py` 已经认识 `midship` 这个 step（`MAX_ATTEMPTS["midship"]=2`、
`TIMEOUTS["midship"]=600`、`build_cmd("midship", ...)` → `python3 -m midship --project`、
`step_done` 查 `run_log` 里 `dataset like 'midship%' and status='ok'`），但**没有把它
放进默认七步**。

原因：本目录的输入是 `event_inbox`，而事件是 ingest / engine 收尾时写的。整链跑完
之前，当天的账本还没闭合；这时候先投影，等于把半截事件推给 gbrain，而且它一旦
失败会把整链标成 `blocked`，让后面本该跑的步也停下。

所以它是 **opt-in**：

```
python3 adsdesk/chain.py --only midship        # 只跑这一步（链跑完之后手动补）
```

想让它每天自动跟跑，把 `midship` 加进 `chain.py:main()` 的 `steps` 列表末尾即可 ——
但先确认「链跑完」这个前提还成立（`--only` 手动跑的时候它当然不成立）。
