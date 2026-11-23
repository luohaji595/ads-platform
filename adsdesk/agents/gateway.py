"""magpie 网关客户端（OpenAI 兼容）。

为什么自己写 urllib 而不是 requests：
  本机 7897 端口挂着代理，requests 会读 HTTP_PROXY 直接 502。
  这里显式装一个空 ProxyHandler，等于给每个请求加 --noproxy '*'。

实测（2026-11-17）：auto/glm-5.3、auto/deepseek-v4.1-flash-1017、auto/kimi-k3.1-pro
三个都支持 response_format={"type":"json_object"}，content 是干净的 JSON。

一个必须记住的坑：这些模型都带思维链，reasoning_tokens 也算进 max_tokens。
给小了（比如 64）就 finish_reason=length、content=None、只剩 reasoning_content。
**实测的诊断 prompt（5.4k 字系统提示 + 11k 输入）在 4000 预算下会 100% 被思维链吃光**，
网关直接返 502 且错误信息是「the whole output spent on reasoning」。
给 16000 时 glm-5.3 reasoning≈6800、正文≈1400（331s）；
deepseek-v4.1-flash-1017 的思维链更长（实测 reasoning_tokens 15079），16000 会 finish=length，
所以默认给 32000 —— 思维链长度随输入变化，宁可多给。
所以默认 16000 + timeout 600，且下面在 finish_reason==length 时明确报错而不是把 null 当结果。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

# 允许用环境变量指到别处：验「网关挂了奏折照常出」这条降级线时，
# 指到 http://127.0.0.1:9999 就能复现，不用改代码（改代码验降级等于没验）。
BASE = os.environ.get("ADS_GATEWAY_BASE") or "http://127.0.0.1:3425/v1"
# 为什么从 auto/* 换到 duanyan/*（2026-11-23）：
# auto/* 是网关的路由别名，背后落到 CodeArts，而那个账号的积分已耗尽 ——
# 报错是 HTTP 400 + `TM.00001050 您的积分已耗尽`，不是网络问题、也不是限流。
# 现象是诊断链路「全挂」，看日志像网关挂了，其实是额度用光。
# duanyan/* 是同一个模型的另一条通道，实测正常（glm-5.3 / deepseek-v4.1-flash-1017
# / kimi-k3.1-pro 都能答）。保留 auto/* 作兜底：哪天 duanyan 出问题，auto 可能已充值。
# 2026-11-23 二次修正：duanyan/* 也是按额度掐的 —— max_tokens=8 能过，
# 4000 就报 429「书生端砚: quota exceeded」。也就是说它只够答短问，跑不动
# 一次要 48000 预算的诊断。现在默认走 trae-cn（实测 48k + JSON 模式正常），
# 后面按「能不能吃大预算」排序兜底。
DEFAULT_MODEL = "trae-cn/deepseek-v4.1-flash-max"
FALLBACK_MODEL = "scnet/DeepSeek-V4.1-Flash-1017-Event"

# 对话/收敛这类要一次吐几千字的场合用的兜底链。
# 顺序原则：先快后稳，且只放实测能在大 max_tokens 下答完的通道。
CHAT_CHAIN = ("trae-cn/deepseek-v4.1-flash-max",
              "scnet/DeepSeek-V4.1-Flash-1017-Event",
              "opencode-zen-free/deepseek-v4.1-flash-free",
              "trae-cn/glm-5.3-flashx")

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class GatewayError(RuntimeError):
    pass


def _post(body: dict, timeout: int) -> dict:
    """发一次请求，带**真正能生效的硬截止时间**。

    踩过两个坑，都记在这里：

    坑一：urllib 的 `timeout` 只限制单次 socket 操作，不限制总时长。一个带思维链的
    响应缓慢吐 token，`timeout=600` 根本不触发 —— 实测进程挂了 97 分钟还活着，
    DB 里 agent_run 永远是 running。

    坑二：起 watchdog 线程去 `resp.close()` **在 macOS 上无效**。已经阻塞在
    `recv()` 里的线程不会被 close 唤醒，close 只是让对象标记为关闭。实测又挂了 97 分钟。

    所以真正能用的办法是：**把整个请求丢到工作线程里，主线程 `join(timeout)`**。
    主线程到点就走，不管工作线程死没死 —— 它是 daemon，进程退出时一起带走。
    代价是那个连接会泄漏到进程结束（一天跑一次，无所谓），
    但「宁可失败重试，也不要永远挂着」。
    """
    box: dict = {}

    def _worker():
        try:
            req = urllib.request.Request(
                BASE + "/chat/completions",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with _OPENER.open(req, timeout=timeout) as r:
                box["raw"] = json.loads(r.read().decode("utf-8"))
        except BaseException as e:          # noqa: BLE001 —— 什么都不能漏出去
            box["err"] = e

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise GatewayError(
            f"网关响应超过硬截止 {timeout}s 仍未返回，已放弃（{body.get('model')}）。"
            f"通常是思维链太长或网关排队；可调大 timeout 或换更快的模型。"
        )
    if "err" in box:
        raise box["err"]
    return box["raw"]


def chat(messages: list[dict], *, model: str = DEFAULT_MODEL, max_tokens: int = 32000,
         temperature: float = 0.2, json_mode: bool = True, timeout: int = 600,
         retries: int = 1) -> dict:
    """返回 {content, tokens_in, tokens_out, model, finish_reason, reasoning}。

    json_mode=True 时要求网关返回 JSON 对象，并把 content 解析后的 dict 放在
    parsed 字段里；解析失败抛 GatewayError（不静默返回空字典）。
    """
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            raw = _post(body, timeout)
        except GatewayError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            if attempt < retries:
                time.sleep(2)
                continue
            raise GatewayError(f"网关连不上（{model}）：{e}") from e

        ch = (raw.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        content = msg.get("content")
        finish = ch.get("finish_reason")
        usage = raw.get("usage") or {}

        if finish == "length" or content is None:
            # 思维链吃光了预算。这不是「模型不想答」，是预算不够，必须显式失败：
            # 上层要能区分「agent 跑挂了」和「agent 说没问题」。
            raise GatewayError(
                f"{model} 输出被截断（finish={finish}，"
                f"reasoning_tokens={(usage.get('completion_tokens_details') or {}).get('reasoning_tokens')}）。"
                f"调大 max_tokens 重试。"
            )

        out = {
            "content": content,
            "tokens_in": usage.get("prompt_tokens"),
            "tokens_out": usage.get("completion_tokens"),
            "model": raw.get("model") or model,
            "finish_reason": finish,
            "reasoning": msg.get("reasoning_content"),
        }
        if json_mode:
            try:
                out["parsed"] = json.loads(content)
            except json.JSONDecodeError as e:
                raise GatewayError(f"{model} 返回的不是合法 JSON：{content[:200]}") from e
        return out

    raise GatewayError(f"网关重试耗尽：{last}")


def chat_chain(messages: list[dict], *, chain: tuple[str, ...] | None = None,
               model: str | None = None, **kw) -> dict:
    """按链路依次试，第一个答出来的就是结果。

    为什么要有这个：单模型失败的原因分两类 —— 额度/限流（429）、通道坏了（400/5xx），
    两类都不是「这个模型不会答」，换一条路就能出结论。诊断那边早就有链，
    对话这边一直只试一个模型，2026-11-23 duanyan 额度被掐就直接答不出来。
    model 若显式给出，插到链首当首选，不当唯一。
    """
    order = list(chain or CHAT_CHAIN)
    if model:
        if model in order:
            order.remove(model)
        order.insert(0, model)
    errs: list[str] = []
    for m in order:
        try:
            return chat(messages, model=m, **kw)
        except GatewayError as e:
            errs.append(f"{m}: {e}")
    raise GatewayError("全部通道都失败 —— " + " ｜ ".join(errs))


def health() -> dict:
    """探活。列出可用模型数，用于启动前自检。"""
    req = urllib.request.Request(BASE + "/models")
    with _OPENER.open(req, timeout=10) as r:
        d = json.loads(r.read().decode("utf-8"))
    ids = [m.get("id") for m in (d.get("data") or [])]
    return {"count": len(ids), "has_default": DEFAULT_MODEL in ids, "models": ids[:5]}


# 诊断链路：先快后稳。deepseek 实测 123 秒出结论（32000 预算、20734 tokens），
# 比 glm-5.3 的 331 秒快；但它在网关排队时会整个卡死。glm 慢一些，429 时也挂。
# 两个都试，才有当天出结论的可能。
DIAGNOSE_CHAIN = ("trae-cn/deepseek-v4.1-flash-max",
                  "scnet/DeepSeek-V4.1-Flash-1017-Event",
                  "opencode-zen-free/deepseek-v4.1-flash-free",
                  "trae-cn/glm-5.3-flashx",
                  "duanyan/deepseek-v4.1-flash-1017",
                  "duanyan/glm-5.3",
                  "auto/deepseek-v4.1-flash-1017",
                  "auto/glm-5.3")


def chat_with_fallback(messages: list[dict], *, models: tuple[str, ...] | list[str] | None = None,
                       timeout: int = 300, **kw) -> dict:
    """按顺序试每个模型，返回第一个成功的；全挂时抛 GatewayError。

    为什么要换模型而不是重试同一个：2026-11-19 那次 deepseek 在网关排队，
    600s 硬截止到时它还在队列里 —— 再试一次只是再排一次队。换模型是当天还能
    出结论的唯一办法。

    为什么把每个模型的失败原因都留着：只报最后一个，会把「主用排队、备用
    截断」显示成「备用截断」，下次照着修还会踩同一个坑。
    """
    chain = list(models or DIAGNOSE_CHAIN)
    fails: list[str] = []
    for m in chain:
        try:
            out = chat(messages, model=m, timeout=timeout, **kw)
        except GatewayError as e:
            fails.append(f"{m}: {e}")
            continue
        if fails:
            # 成功但降级过 —— 上层可以记一笔，页面也值得知道今天是谁答的。
            out["fell_back"] = list(fails)
        return out
    raise GatewayError("诊断链路全挂 —— " + " ｜ ".join(fails))
