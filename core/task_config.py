"""Versioned, credential-free task documents, scoped to an account directory."""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from .models import RunSettings

GLOBAL_FIELDS = frozenset({
    "ignore_risk_control",
    "concurrent", "async_dispatch", "request_workers", "global_request_workers", "parallel_links", "link_workers",
    "time_sync_enabled", "time_server", "sync_interval_seconds", "global_interval_ms",
})
TASK_FIELDS = frozenset(asdict(RunSettings())) - GLOBAL_FIELDS


def with_globals(task: RunSettings, global_settings: RunSettings) -> RunSettings:
    return replace(task, **{key: getattr(global_settings, key) for key in GLOBAL_FIELDS})


def settings_from_dict(data: dict) -> RunSettings:
    defaults = asdict(RunSettings())
    if not isinstance(data, dict) or set(data) - defaults.keys():
        raise ValueError("配置包含不支持的运行参数")
    for key, value in data.items():
        expected = type(defaults[key])
        if expected is bool and type(value) is not bool:
            raise ValueError(f"配置参数 {key} 应为开关")
        if expected is int and type(value) is not int:
            raise ValueError(f"配置参数 {key} 应为整数")
        if expected is float and (type(value) not in (int, float)):
            raise ValueError(f"配置参数 {key} 应为数字")
        if expected is str and not isinstance(value, str):
            raise ValueError(f"配置参数 {key} 应为文本")
    return RunSettings(**data)


@dataclass
class SavedTask:
    name: str = "未命名任务"
    url: str = ""
    enabled: bool = True
    settings: RunSettings = field(default_factory=RunSettings)
    key: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass
class TaskDocument:
    tasks: list[SavedTask] = field(default_factory=list)
    templates: dict[str, RunSettings] = field(default_factory=dict)
    global_settings: RunSettings = field(default_factory=RunSettings)

    def payload(self) -> dict:
        return {
            "version": 1,
            "global": {key: getattr(self.global_settings, key) for key in GLOBAL_FIELDS},
            "tasks": [{"id": item.key, "name": item.name, "url": item.url,
                       "enabled": item.enabled,
                       "settings": {key: getattr(item.settings, key) for key in TASK_FIELDS}}
                      for item in self.tasks],
            "templates": {name: {key: getattr(settings, key) for key in TASK_FIELDS}
                          for name, settings in self.templates.items()},
        }

    @classmethod
    def parse(cls, data: dict) -> TaskDocument:
        if (not isinstance(data, dict) or data.get("version") != 1
                or set(data) - {"version", "global", "tasks", "templates"}):
            raise ValueError("任务配置格式或版本不支持")
        raw_tasks, raw_templates = data.get("tasks"), data.get("templates", {})
        if not isinstance(raw_tasks, list) or not 1 <= len(raw_tasks) <= 100:
            raise ValueError("配置应包含 1–100 条任务")
        if not isinstance(raw_templates, dict) or len(raw_templates) > 100:
            raise ValueError("模板格式无效或超过 100 个")
        globals_data = data.get("global", {})
        if not isinstance(globals_data, dict) or set(globals_data) - GLOBAL_FIELDS:
            raise ValueError("全局设置格式无效")
        result = cls(global_settings=settings_from_dict(globals_data))
        seen = set()
        for item in raw_tasks:
            if not isinstance(item, dict) or set(item) != {"id", "name", "url", "enabled", "settings"}:
                raise ValueError("任务记录格式无效")
            key, name, url = item["id"], item["name"], item["url"]
            if (not isinstance(key, str) or len(key) != 32
                    or any(ch not in "0123456789abcdef" for ch in key) or key in seen):
                raise ValueError("任务标识无效或重复")
            if (not isinstance(name, str) or not 1 <= len(name) <= 48
                    or any(ord(ch) < 32 for ch in name)):
                raise ValueError("任务名称应为 1–48 字")
            if not isinstance(url, str) or len(url) > 4096 or type(item["enabled"]) is not bool:
                raise ValueError("任务链接或勾选状态无效")
            params = item["settings"]
            if not isinstance(params, dict) or set(params) - TASK_FIELDS:
                raise ValueError("任务参数包含不支持的字段")
            result.tasks.append(SavedTask(name, url, item["enabled"], settings_from_dict(params), key))
            seen.add(key)
        for name, params in raw_templates.items():
            if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 24
                    or any(ord(ch) < 32 for ch in name)
                    or not isinstance(params, dict) or set(params) - TASK_FIELDS):
                raise ValueError("模板名称或参数无效")
            result.templates[name] = settings_from_dict(params)
        return result


def load_document(path: str) -> TaskDocument | None:
    if not os.path.exists(path):
        return None
    if os.path.getsize(path) > 2_000_000:
        raise ValueError("配置文件超过 2 MB")
    with open(path, encoding="utf-8-sig") as stream:
        return TaskDocument.parse(json.load(stream))


def save_document(path: str, document: TaskDocument) -> None:
    payload = document.payload()
    TaskDocument.parse(payload)
    parent = str(Path(path).absolute().parent)
    os.makedirs(parent, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=parent, delete=False) as stream:
            temporary = stream.name
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.remove(temporary)
