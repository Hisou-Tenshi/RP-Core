# SPDX-License-Identifier: AGPL-3.0-only









































from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, TypeVar

from error_text import format_exception_inline
from llm_compat import LlmDeadlineExceeded, call_with_deadline
from llm_tool_schema import should_skip_provider_on_error

T = TypeVar("T")

__all__ = [
    "BUDGET_EXHAUSTED",
    "LlmDeadlineExceeded",
    "ResilienceOutcome",
    "RetryPolicy",
    "call_with_deadline",
    "clamp_timeout",
    "degrade",
    "finalize_reply_deadline",
    "is_budget_exhausted",
    "is_retryable_error",
    "reply_deadline_exceeded",
    "reply_deadline_started",
    "reply_deadline_status",
    "run_with_resilience",
    "start_reply_deadline",
    "startable",
]







def _cfg(name: str, default: float) -> float:
    """读一个数值型超时配置；缺失/非法时回落到 default。"""
    try:
        import tenshi_config

        raw = getattr(tenshi_config, name, None)
        if raw is None:
            return float(default)
        val = float(raw)
        return val
    except Exception:
        return float(default)






DEFAULT_MODEL_CALL_TIMEOUT = 240.0
DEFAULT_AUX_LLM_TIMEOUT = 45.0
DEFAULT_IMAGE_ANALYSIS_TIMEOUT = 60.0
DEFAULT_PRE_ROUTING_WAVE_TIMEOUT = 90.0
DEFAULT_MEMORY_RETRIEVAL_TIMEOUT = 30.0
DEFAULT_TOOL_LOOP_TIMEOUT = 300.0
DEFAULT_REPLY_DEADLINE = 780.0


def model_call_timeout() -> float:
    return _cfg("REPLY_MODEL_CALL_TIMEOUT_SEC", DEFAULT_MODEL_CALL_TIMEOUT)


def aux_llm_timeout() -> float:
    return _cfg("REPLY_AUX_LLM_TIMEOUT_SEC", DEFAULT_AUX_LLM_TIMEOUT)


def image_analysis_timeout() -> float:
    return _cfg("REPLY_IMAGE_ANALYSIS_TIMEOUT_SEC", DEFAULT_IMAGE_ANALYSIS_TIMEOUT)


def pre_routing_wave_timeout() -> float:
    return _cfg("REPLY_WAVE1_TIMEOUT_SEC", DEFAULT_PRE_ROUTING_WAVE_TIMEOUT)


def memory_retrieval_timeout() -> float:
    return _cfg("REPLY_MEMORY_RETRIEVAL_TIMEOUT_SEC", DEFAULT_MEMORY_RETRIEVAL_TIMEOUT)


def tool_loop_timeout() -> float:
    return _cfg("REPLY_TOOL_LOOP_TIMEOUT_SEC", DEFAULT_TOOL_LOOP_TIMEOUT)


def reply_deadline_seconds() -> float:
    return _cfg("REPLY_TOTAL_DEADLINE_SEC", DEFAULT_REPLY_DEADLINE)


def _log():
    """尽力拿一个 logger；拿不到就返回 None（所有调用点都要容忍）。"""
    try:
        import tenshi_config

        path = getattr(tenshi_config, "DEBUG_LOG_PATH", None)
        from tenshi_utils import setup_logger

        return setup_logger("TenshiResilience", path)
    except Exception:
        return None





@dataclass(frozen=True)
class RetryPolicy:
    """瞬时错误的重试策略。

    `max_attempts=1` 表示「不重试、失败即降级」——这是主回复链的既有策略
    （见 llm_routing 的默认值：SDK 内置重试必须关掉，否则一次 524 被放大成 3~5 次请求）。
    预处理环节可以放宽到 2，因为它们的故障不影响用户看到正文。

    注意：**确定性错误永不重试**（key 无效 / 模型不存在 / 工具声明坏掉），
    判定复用 `llm_tool_schema.should_skip_provider_on_error`——
    同一个 provider 的所有候选都会一模一样地失败，重试只是白烧时间。
    """

    max_attempts: int = 1
    base_sleep: float = 0.8
    max_sleep: float = 8.0

    def attempts(self) -> int:
        return max(1, int(self.max_attempts))


NO_RETRY = RetryPolicy(max_attempts=1)
FAST_RETRY = RetryPolicy(max_attempts=2, base_sleep=0.5, max_sleep=2.0)


def is_retryable_error(exc: BaseException) -> bool:
    """这个异常值不值得重试？（确定性错误一律不值得，且超时不算——已经放弃等待了。）"""
    if isinstance(exc, LlmDeadlineExceeded):

        return False
    try:
        skip, _reason = should_skip_provider_on_error(exc)
    except Exception:
        return True
    return not skip





@dataclass
class ResilienceOutcome:
    """一次「带容灾的调用」的完整结果。

    调用方用 `outcome.ok` 分流，用 `outcome.degradation` 判断降级层级：

    * `"none"`         —— 首次尝试就成功（含重试 1 次后成功，`degradation="retried"`）
    * `"retried"`      —— 重试后成功
    * `"deadline"`     —— 到点放弃等待 → 用缺省值降级
    * `"unavailable"`  —— 确定性不可用（key 全灭 / 通路全挂 / 参数坏掉）→ 用缺省值降级
    * `"error"`        —— 其他异常 → 用缺省值降级
    """

    name: str
    value: Any = None
    ok: bool = False
    degradation: str = "none"
    attempts: int = 0
    elapsed: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return self.degradation not in ("none", "retried")

    def summary(self) -> str:
        detail = f"{self.name}: ok={self.ok} degradation={self.degradation} " \
                 f"attempts={self.attempts} elapsed={self.elapsed:.2f}s"
        if self.errors:
            detail += f" last_error={self.errors[-1]!r}"
        return detail





def run_with_resilience(
    fn: Callable[[], T],
    *,
    name: str,
    default: T,
    timeout_sec: float | None = None,
    retry: RetryPolicy | None = None,
    on_success: Callable[[T], bool] | None = None,
) -> ResilienceOutcome:
    """带「超时 + 重试 + 降级」地执行 fn，**永不抛异常**。

    参数：
      * `timeout_sec`：单次尝试的硬上限（None/<=0 = 不设上限，退回原行为）。
      * `retry`：瞬时错误的重试策略；默认 `NO_RETRY`（失败即降级）。
      * `on_success`：可选的「结果可用性」判定。返回 False 表示这次调用虽然
        没抛异常，但结果不可用（例如空文本 / 错误占位 / 空列表），
        应当继续重试；重试用尽后按「结果不可用」降级。
      * `default`：**语义上的空值**。全链失败时返回它，调用方据此知道
        「这一环没数据」，而不是拿到错误文本。

    始终返回 `ResilienceOutcome`；异常只被记录在 `outcome.errors` 里。
    """
    policy = retry or NO_RETRY
    attempts = policy.attempts()
    started = time.monotonic()
    errors: list[str] = []
    log = _log()

    last_outcome: ResilienceOutcome | None = None
    for attempt in range(1, attempts + 1):
        try:
            value = call_with_deadline(fn, timeout_sec=timeout_sec, label=name)
        except LlmDeadlineExceeded as exc:
            errors.append(f"deadline: {format_exception_inline(exc)}")
            if log:
                log.warning(
                    f"[resilience] {name} 超过 {float(timeout_sec):.1f}s 无返回，"
                    f"放弃等待并降级（attempt {attempt}/{attempts}）。"
                )
            last_outcome = ResilienceOutcome(
                name=name,
                value=default,
                ok=False,
                degradation="deadline",
                attempts=attempt,
                elapsed=time.monotonic() - started,
                errors=list(errors),
            )

            break
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            errors.append(format_exception_inline(exc))
            deterministic = not is_retryable_error(exc)
            if log:
                level = "error" if deterministic else "warning"
                getattr(log, level)(
                    f"[resilience] {name} 调用失败"
                    f"（attempt {attempt}/{attempts}"
                    f"{'，确定性错误' if deterministic else '，瞬时错误'}）："
                    f"{format_exception_inline(exc)}"
                )
            last_outcome = ResilienceOutcome(
                name=name,
                value=default,
                ok=False,
                degradation="unavailable" if deterministic else "error",
                attempts=attempt,
                elapsed=time.monotonic() - started,
                errors=list(errors),
            )
            if deterministic or attempt >= attempts:
                break

        else:
            usable = True
            if on_success is not None:
                try:
                    usable = bool(on_success(value))
                except Exception:
                    usable = True
            if usable:
                return ResilienceOutcome(
                    name=name,
                    value=value,
                    ok=True,
                    degradation="none" if attempt == 1 else "retried",
                    attempts=attempt,
                    elapsed=time.monotonic() - started,
                    errors=list(errors),
                )
            errors.append("unusable result")
            if log:
                log.warning(
                    f"[resilience] {name} 返回了不可用结果（空/错误占位），"
                    f"attempt {attempt}/{attempts}。"
                )
            last_outcome = ResilienceOutcome(
                name=name,
                value=default,
                ok=False,
                degradation="unusable",
                attempts=attempt,
                elapsed=time.monotonic() - started,
                errors=list(errors),
            )
            if attempt >= attempts:
                break


        sleep_for = min(policy.base_sleep * (2 ** (attempt - 1)), policy.max_sleep)
        if sleep_for > 0:
            time.sleep(sleep_for)

    assert last_outcome is not None
    return last_outcome


class _ValueBox(dict):
    """一个 dict 子类：拦截 `box["value"] = ...`，把「已赋值」记下来。

    为什么需要：`degrade()` 想在退出 with 块时判断「这一环到底产出了没有」，
    但普通 dict 无法拦截 `__setitem__`。这里只对 `"value"` 这一个键置位，
    其余键（ok / degradation / error …）仍是普通字典语义，调用方照常 `ctx["ok"]`。
    """

    __slots__ = ("_flag",)

    def __init__(self, flag: list, initial: dict):
        super().__init__(initial)
        self._flag = flag

    def __setitem__(self, key, val):
        if key == "value":
            self._flag[0] = True
        super().__setitem__(key, val)


@contextmanager
def degrade(
    name: str,
    *,
    timeout_sec: float | None = None,
    retry: RetryPolicy | None = None,
) -> Iterator[dict]:
    """非关键环节的「降级命名空间」，**绝不抛异常**。

    用法::

        with degrade("weather") as ctx:
            ctx["value"] = _fetch_weather()
        report = ctx["value"] or "（天气数据获取失败）"

    语义：
      * 块内抛出的异常（含超时）被吞掉，`ctx["ok"]` 置 False，
        `ctx["degradation"]` 记录原因；
      * **赋值即成功**：只有把结果写进 `ctx["value"]` 才算这一环产出了东西。
        块跑完了却什么都没赋值 → `degradation="empty"`，
        避免「任务静默没产出」被误判成成功；
      * 异常绝不外泄——调用方只需关心 `ctx["value"]` 能不能用。

    注意判定口径是「是否被显式赋值」而非「是否 truthy」：
    `ctx["value"] = ""` / `= []` / `= None` 都算**有意的空值降级**（ok=True）。
    """
    flag = [False]
    ctx: dict = _ValueBox(
        flag,
        {
            "name": name,
            "value": None,
            "ok": False,
            "assigned": False,
            "degradation": "none",
            "error": None,
            "elapsed": 0.0,
        },
    )
    log = _log()
    started = time.monotonic()
    try:
        yield ctx
    except KeyboardInterrupt:
        raise
    except BaseException as exc:
        ctx["error"] = exc
        ctx["ok"] = False
        if isinstance(exc, LlmDeadlineExceeded):
            ctx["degradation"] = "deadline"
        else:
            ctx["degradation"] = (
                "unavailable" if not is_retryable_error(exc) else "error"
            )
        if log:
            log.warning(
                f"[degrade] {name} 降级（{ctx['degradation']}）：{format_exception_inline(exc)}"
            )
    else:
        ctx["assigned"] = bool(flag[0])
        ctx["ok"] = ctx["assigned"]
        if not ctx["ok"]:
            ctx["degradation"] = "empty"
            if log:
                log.warning(f"[degrade] {name} 未产出可用结果，按空值降级。")
    finally:
        ctx["elapsed"] = time.monotonic() - started










_reply_deadline: dict = {"start": None, "total": None}


def start_reply_deadline(total_sec: float | None = None) -> None:
    """在 main() 开头调用：记录本轮回复的起点与总预算。"""
    total = reply_deadline_seconds() if total_sec is None else float(total_sec)
    _reply_deadline["start"] = time.monotonic()
    _reply_deadline["total"] = total if total and total > 0 else None


def reply_deadline_started() -> bool:
    return _reply_deadline.get("start") is not None


def reply_deadline_remaining() -> float | None:
    """剩余预算（秒）；未启动或未设上限时返回 None。"""
    start = _reply_deadline.get("start")
    total = _reply_deadline.get("total")
    if start is None or total is None:
        return None
    return float(total) - (time.monotonic() - float(start))


def reply_deadline_exceeded(reserve_sec: float = 0.0) -> bool:
    """整轮预算是否已耗尽（可预留 reserve_sec 给收尾/落盘）。"""
    remaining = reply_deadline_remaining()
    if remaining is None:
        return False
    return remaining <= float(reserve_sec or 0.0)


def reply_deadline_status() -> dict:
    """诊断用：{total, elapsed, remaining}；未启动时全为 None。"""
    start = _reply_deadline.get("start")
    total = _reply_deadline.get("total")
    if start is None:
        return {"total": total, "elapsed": None, "remaining": None}
    elapsed = time.monotonic() - float(start)
    return {
        "total": total,
        "elapsed": elapsed,
        "remaining": (float(total) - elapsed) if total is not None else None,
    }


def finalize_reply_deadline() -> None:
    """本轮结束（含失败路径）时清理，避免跨轮残留。"""
    _reply_deadline["start"] = None
    _reply_deadline["total"] = None


class BudgetExhausted:
    """哨兵：整轮预算已用尽，这一环**不要启动**。

    为什么用哨兵而不是 `0.0`：`call_with_deadline` 约定「None 或 <=0 = 不设上限」，
    所以把 0.0 传给它会得到**无上限**的调用——正好和「没预算了」相反。
    `clamp_timeout` 因此在预算耗尽时返回 `BUDGET_EXHAUSTED`，
    调用方必须用 `is_budget_exhausted()` 判断并跳过该环节。
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<BUDGET_EXHAUSTED>"

    def __bool__(self) -> bool:
        return False


BUDGET_EXHAUSTED = BudgetExhausted()


def is_budget_exhausted(value) -> bool:
    """这个 clamp 结果是不是「预算已用尽、不要启动」？"""
    return value is BUDGET_EXHAUSTED


def clamp_timeout(
    timeout_sec: float | None,
    *,
    fallback: float | None = None,
    reserve_sec: float = 0.0,
):
    """把一个环节的预算夹到「整轮剩余预算」之内。

    整轮预算只剩 12s 时，给一个新模型 240s 的调用上限毫无意义——它注定
    要等满约 12s 才被端到端死线砍掉。这里直接把上限压到剩余值，
    让「到点即切下一档」的判定提前发生。

    返回值：
      * 正数       —— 可直接用作该环节的上限；
      * `None`     —— 未设任何上限（保持原行为）；
      * `BUDGET_EXHAUSTED` —— 整轮预算已用尽，**不要启动该环节**
        （调用方用 `is_budget_exhausted()` 判断；切勿当成 0 传给
        `call_with_deadline`，那会变成「无上限」）。
    """
    base = timeout_sec if timeout_sec is not None else fallback
    remaining = reply_deadline_remaining()
    if remaining is None:
        return base
    usable = remaining - float(reserve_sec or 0.0)
    if usable <= 0:
        return BUDGET_EXHAUSTED
    if base is None or base <= 0:
        return usable
    return min(float(base), usable)


def startable(timeout_sec) -> bool:
    """在启动一个环节之前判断「还值得启动吗」。

    三种情况返回 False：
      * 整轮预算已耗尽（`clamp_timeout` 返回 `BUDGET_EXHAUSTED`）；
      * 传进来的上限本身就是 0 或负数（显式「不要启动」）；
      * 端到端死线已到（`reply_deadline_exceeded`）。
    """
    if is_budget_exhausted(timeout_sec):
        return False
    if reply_deadline_exceeded():
        return False
    if timeout_sec is not None and float(timeout_sec) <= 0:
        return False
    return True
