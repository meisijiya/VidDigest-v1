r"""`error` 事件的形状守卫（ADR 0034）。

两条判据：

1. **形状**：`sse_events.error_event()` 造出来的负载六键齐全，缺省值是
   `False` / 空串。前端按 `reason` 判类，靠的就是这一层固定形状。
2. **唯一出口**：`backend/` 下（测试与注册模块自身除外）不许再手写一条
   `event="error"` 的 `ServerSentEvent`。改造前 11 处各写各的 `json.dumps`，
   语义相同的两次「今日次数已用完」长得不一样（一处带 `need_vip`、
   一处不带）——这类不一致不会报错，只会让前端的分支永远少接一种。

为什么形状要「总是出现」而不是「按需出现」：按需出现时前端得同时处理
「有这个键」与「没这个键」两种形状，而当时的代码两种都没判，只匹配了
中文文案——判类因此只接得住三种无字幕文案里的一种。
"""
import ast
import json
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
CONSTRUCTOR = BACKEND / "sse_events.py"
SCAN_SKIP_DIRS = {"venv", "__pycache__", ".pytest_cache", "node_modules", "tests",
                  ".scratch", "data"}

os.environ.setdefault("JWT_SECRET", "test-only-jwt-secret-0123456789abcdef0123456789abcdef")
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import sse_events  # noqa: E402

SIX_KEYS = {"message", "need_login", "need_vip", "reason", "asr_reason", "byok"}


def payload_of(event) -> dict:
    return json.loads(event.raw_data)


class TestErrorEventShape:
    def test_defaults_cover_all_six_keys(self):
        body = payload_of(sse_events.error_event("炸了"))
        assert set(body) == SIX_KEYS, f"error 事件的键集不是六键：{sorted(body)}"
        assert body["message"] == "炸了"
        assert body["need_login"] is False
        assert body["need_vip"] is False
        assert body["reason"] == ""
        assert body["asr_reason"] == ""
        assert body["byok"] is False

    def test_explicit_values_are_kept(self):
        body = payload_of(sse_events.error_event(
            "今日次数已用完",
            need_vip=True,
            reason=sse_events.QUOTA_EXHAUSTED,
        ))
        assert body["need_vip"] is True
        assert body["need_login"] is False
        assert body["reason"] == "quota_exhausted"

    def test_chinese_is_not_escaped(self):
        """裸 `ensure_ascii=True` 会把中文转成 \\uXXXX，也改不了行为——
        但两头一致才让「同一条消息」在日志与浏览器里读起来是同一句。"""
        body = payload_of(sse_events.error_event("今日解析已用完"))
        assert body["message"] == "今日解析已用完"
        assert "\\u" not in sse_events.error_event("今日").raw_data

    def test_event_name_is_error(self):
        assert sse_events.error_event("x").event == "error"


def error_emissions(path: Path) -> list:
    """找出这个文件里手写的 `event="error"` 帧（line 号列表）。"""
    tree = ast.parse(Path(path).read_bytes().decode("utf-8"))
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name != "ServerSentEvent":
            continue
        for kw in node.keywords:
            if kw.arg == "event" and isinstance(kw.value, ast.Constant) \
                    and kw.value.value == "error":
                hits.append(node.lineno)
    return hits


class TestErrorHasOneOutlet:
    def test_no_hand_written_error_frame_outside_the_constructor(self):
        offenders = []
        for path in sorted(BACKEND.rglob("*.py")):
            if any(part in SCAN_SKIP_DIRS for part in path.relative_to(BACKEND).parts):
                continue
            if path == CONSTRUCTOR:
                continue
            hits = error_emissions(path)
            if hits:
                offenders.append((path.relative_to(BACKEND).as_posix(), hits))
        assert not offenders, (
            "又出现手写的 error 帧了——它们会各自决定「哪些键出现」，"
            "而同类拒绝长得不一样时前端的分支永远少接一种：\n"
            + "\n".join(f"  {rel}: {lines}" for rel, lines in offenders)
            + "\n\n修法：改用 sse_events.error_event(...)。"
        )

    def test_scanner_would_catch_a_hand_written_frame(self):
        """守卫的正例：没有这条，把 `error_emissions` 改成恒返回 [] 也全绿。"""
        synthetic = BACKEND / "tests" / "_synthetic_sse.py"
        synthetic.write_text(
            "from fastapi.sse import ServerSentEvent\n"
            "ev = ServerSentEvent(raw_data='{}', event='error')\n",
            encoding="utf-8",
        )
        try:
            assert error_emissions(synthetic) == [2]
        finally:
            synthetic.unlink()
