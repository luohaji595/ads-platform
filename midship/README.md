# 本船事件中台

事件由 ingest / engine 收尾写入 `event_inbox`，本目录只消费、去重、派单、留痕。不新建定时器，不拉积加，不写广告后台，不直接往聊天发消息。

- 规则：`rules.yaml`（新增一类事件只改这个文件）
- 收件箱：`event_inbox`（P0 已建）
- 留痕：`dispatch_log`
- 待送达：`tell_outbox`（同一业务日同一动作只留一张；由船长送到本聊天）
- 台账投影：`python3 -m midship --project`（失败不回写数据库）

手动排空：`python3 -m midship`（在工程根目录）。
