"""Color outcome events without modifying the log text or saved log files."""
from __future__ import annotations

import re

from PyQt5.QtGui import QColor, QSyntaxHighlighter, QTextCharFormat

LOG_NORMAL = "#000000"
LOG_SUCCESS = "#008000"
LOG_FAILURE = "#CC0000"
LOG_RETRY = "#9A741F"


class LogHighlighter(QSyntaxHighlighter):
    _event = re.compile(
        r"^(?:\[\d{4}-\d{2}-\d{2} [^\]]+\]\s*)?"
        r"(?:\[链接\d+\]\s*)?\[([^\]]+)\]\s*(.*)$"
    )
    _success = {
        "任务": ("领取成功",),
        "领取-响应": ("code=0（成功）",),
        "Cookie": ("登录状态验证通过",),
        "登录": ("扫码登录成功",),
    }
    _failure = {
        "任务": ("执行失败:", "执行失败：", "失败:", "失败："),
        "批次": ("执行中止:", "执行中止："),
        "登录": ("检查失败:", "检查失败："),
        "账号": ("登录身份保存失败",),
        "配置": ("读取失败", "自动保存失败", "保存失败", "导入失败", "导出失败"),
    }
    _retry = {
        "领取-提交": ("失败:", "失败："),
        "领取-任务信息": ("失败:", "失败："),
        "登录验证": ("失败:", "失败："),
        "接口响应": ("失败:", "失败："),
    }

    def __init__(self, document) -> None:
        super().__init__(document)
        self._formats = {}
        for name, color in (("normal", LOG_NORMAL), ("success", LOG_SUCCESS),
                            ("failure", LOG_FAILURE), ("retry", LOG_RETRY)):
            text_format = QTextCharFormat()
            text_format.setForeground(QColor(color))
            self._formats[name] = text_format

    def highlightBlock(self, text: str) -> None:
        kind = "normal"
        match = self._event.match(text)
        if match:
            event, message = match.groups()
            if message.startswith(self._failure.get(event, ())):
                kind = "failure"
            elif message.startswith(self._success.get(event, ())):
                kind = "success"
            elif message.startswith(self._retry.get(event, ())):
                kind = "retry"
        self.setFormat(0, len(text.encode("utf-16-le")) // 2, self._formats[kind])
        if match and event == "批次" and message.startswith("完成 —"):
            for outcome in re.finditer(r"(?:成功|失败) \d+", text):
                start = len(text[:outcome.start()].encode("utf-16-le")) // 2
                kind = "success" if outcome.group().startswith("成功") else "failure"
                self.setFormat(start, len(outcome.group()), self._formats[kind])
