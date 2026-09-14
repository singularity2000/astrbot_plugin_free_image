import asyncio
import json
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Union
from urllib.parse import urlparse
from uuid import uuid4

from astrbot import logger
from astrbot.core import AstrBotConfig

from ..workflow import ImageWorkflow


@lru_cache(maxsize=1)
def _pipeline_template_names() -> dict[str, str]:
    """从 _conf_schema.json 读取各管线模板的中文名，作为模型名缺失时的兜底。"""
    schema_path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
    try:
        data = json.loads(schema_path.read_text("utf-8"))
        templates = data.get("api_pipeline", {}).get("templates", {})
        return {
            str(key): str(value.get("name") or key)
            for key, value in templates.items()
            if isinstance(value, dict)
        }
    except Exception as exc:
        logger.warning(f"[FreeImage] 读取管线模板名称失败，将回退到模板 key: {exc}")
        return {}


def template_display_name(template_key: str) -> str:
    """模板 key → 中文模板名（如 vertex_ai_anonymous → Vertex AI 匿名 (逆向)）。"""
    key = str(template_key or "").strip()
    return _pipeline_template_names().get(key, key)


def node_display_name(node: dict) -> str:
    """管线节点的展示名：优先模型名，其次中文模板名。"""
    model = str(node.get("model", "") or "").strip()
    if model:
        return model
    return template_display_name(node.get("__template_key", "")) or "未命名模型"


@dataclass
class PipelineAttemptProgress:
    """仅统计日志；每次管线执行独占，内部重试不重复计入主尝试。"""

    budget: int
    current: int = 0
    node_attempt: int | None = None
    node_requests: int = 0

    def start_node(self) -> None:
        self.node_attempt = None
        self.node_requests = 0

    def record(self, attempt_no: int) -> tuple[int, bool, bool]:
        first_request = self.node_requests == 0
        attempt_changed = self.node_attempt != attempt_no
        if attempt_changed:
            self.current += 1
            self.node_attempt = attempt_no
        self.node_requests += 1
        return self.current, first_request, attempt_changed


@dataclass(frozen=True)
class ReferenceLogContext:
    """参考图元数据不可变；尝试进度由管线为每个生成任务单独创建。"""

    original_count: int = 0
    mode: str = ""
    persona_count: int | None = None
    count: int = 1
    task_index: int = 1
    request_id: str = field(default_factory=lambda: uuid4().hex[:10])
    attempt_progress: PipelineAttemptProgress | None = field(default=None, repr=False, compare=False)

    def for_task(self, index: int) -> "ReferenceLogContext":
        return replace(self, task_index=index, attempt_progress=None)


class BaseProvider(ABC):
    """API 提供商基类。每个子类实现一种 API 的调用逻辑。"""

    def __init__(
        self, node_config: dict, workflow: ImageWorkflow, global_config: AstrBotConfig
    ):
        self.node = node_config
        self.iwf = workflow
        self.conf = global_config
        self.key_index = 0
        self.key_lock = asyncio.Lock()
        # 管线中的 1-based 序号，由 ImageGenPipeline.build 按原始配置下标注入，
        # 与 `画图模型` 列表和 `文生图-<序号>` 命令使用同一套编号。
        self.pipeline_index: Optional[int] = None

    @property
    def name(self) -> str:
        """类名。仅用于内部标识，日志和文案请使用 label / log_label。"""
        return self.__class__.__name__

    @property
    def model_name(self) -> str:
        return str(self.node.get("model", "") or "").strip()

    @property
    def template_key(self) -> str:
        return str(self.node.get("__template_key", "") or "")

    @property
    def _index_prefix(self) -> str:
        return f"{self.pipeline_index}. " if self.pipeline_index else ""

    @property
    def label(self) -> str:
        """对外文案用的精简名：序号 + 模型名。不含 API 地址，避免泄露自建端点。"""
        return f"{self._index_prefix}{node_display_name(self.node)}"

    @property
    def log_label(self) -> str:
        """日志用的完整名：序号 + 模板名 + 模型名 + 主机名。"""
        parts = f"{self._index_prefix}{template_display_name(self.template_key) or self.name}"
        if self.model_name:
            parts = f"{parts} · {self.model_name}"
        host = self._api_host
        return f"{parts} @{host}" if host else parts

    @property
    def _api_host(self) -> str:
        url = str(self.node.get("api_url", "") or "").strip()
        if not url:
            return ""
        try:
            return urlparse(url).hostname or ""
        except ValueError:
            return ""

    @property
    def enabled(self) -> bool:
        return self.node.get("enabled", True)

    @property
    def capabilities(self) -> set[str]:
        """节点声明的生成能力；旧配置默认保持原有图像能力。"""
        raw = self.node.get("capabilities")
        if not isinstance(raw, list):
            return {"text2image", "image2image"}
        return {str(item).strip() for item in raw if str(item).strip()}

    def supports_capability(self, capability: str | None) -> bool:
        return not capability or capability in self.capabilities

    @property
    def max_retry(self) -> int:
        return self.node.get("max_retry", 3)

    @property
    def api_timeout(self) -> int:
        return self.node.get("api_timeout", 300)

    @property
    def proxy(self) -> Optional[str]:
        """节点级代理。留空则不使用代理。"""
        p = self.node.get("proxy", "")
        return p if p else None

    async def _get_api_key(self) -> Optional[str]:
        keys = self.node.get("api_keys", [])
        if not keys:
            return None
        async with self.key_lock:
            key = keys[self.key_index % len(keys)]
            self.key_index = (self.key_index + 1) % len(keys)
            return key

    def _resource_exhausted_delay(self, attempt_no: int) -> float:
        """统一资源耗尽/429退避：2、4、8、16、16... + 0~3 秒抖动。"""
        return min(2**attempt_no, 16) + random.uniform(0, 3)

    def _normal_retry_delay(self) -> float:
        """统一普通重试：3~5 秒抖动。"""
        return random.uniform(3, 5)

    def _is_resource_exhausted(self, status_code: int | None, detail: str = "") -> bool:
        text = detail.lower()
        return status_code == 429 or any(
            key in text
            for key in (
                "resource exhausted",
                "rate limit",
                "too many requests",
                "quota",
            )
        )

    async def _log_retry_and_sleep(
        self,
        *,
        attempt_no: int,
        last_err: str,
        resource_exhausted: bool,
    ) -> None:
        if attempt_no >= self.max_retry:
            return
        delay = (
            self._resource_exhausted_delay(attempt_no)
            if resource_exhausted
            else self._normal_retry_delay()
        )
        reason = "频率/资源限制退避" if resource_exhausted else "普通重试"
        logger.warning(
            f"[{self.log_label}] 调用失败，准备{reason} ({attempt_no}/{self.max_retry})，"
            f"{delay:.2f}s 后重试: {last_err}"
        )
        await asyncio.sleep(delay)

    def _log_image_request(
        self,
        request_log: ReferenceLogContext,
        *,
        received_count: int,
        sent_count: int,
        attempt_no: int,
        internal_attempt: int | None = None,
        internal_budget: int | None = None,
        first_request: bool | None = None,
    ) -> None:
        """在实际生图 POST 前调用；计数指请求内的图片，不表示服务器已接收。"""
        mode = {"text2image": "文生图", "image2image": "图生图", "selfie": "自拍"}.get(
            request_log.mode, "图生图" if received_count else "文生图"
        )
        if request_log.attempt_progress is not None:
            current_attempt, first_node_request, attempt_changed = (
                request_log.attempt_progress.record(attempt_no)
            )
            attempt_budget = request_log.attempt_progress.budget
        else:
            # 兼容直接调用 Provider.generate 的场景，使用该节点自己的主尝试次数。
            current_attempt, attempt_budget = attempt_no, self.max_retry
            first_node_request = attempt_no == 1 and internal_attempt in (None, 1)
            attempt_changed = True
        if first_request is not None:
            first_node_request = first_request
        log_image_request = (
            logger.debug
            if internal_attempt is not None and not attempt_changed
            else logger.info
        )
        fields = [f"{request_log.request_id} 尝试={current_attempt}/{attempt_budget}"]
        if request_log.count > 1:
            fields.append(f"批量生图{request_log.task_index}/{request_log.count}")
        model_label = self.label.replace("\r", " ").replace("\n", " ")
        fields.extend([f"模式={mode}", f"模型={model_label}"])
        prefix = " | ".join(fields)
        if first_node_request:
            limits = []
            if request_log.original_count > received_count:
                limits.append(f"插件参考图限制：{request_log.original_count}张→{received_count}张")
            if received_count > sent_count:
                limits.append(f"提供商参考图限制：{received_count}张→{sent_count}张")
            if limits:
                log_image_request(f"[参考图限制] {prefix} | {'；'.join(limits)}")
        image_info = f"参考图={sent_count}张"
        if request_log.persona_count is not None:
            # 现有组合和提供商截取均保留前缀，人设图排在额外图之前。
            persona_sent = min(request_log.persona_count, sent_count)
            image_info += f"（人设{persona_sent}张，额外{sent_count - persona_sent}张）"
        if internal_attempt is not None:
            image_info += f" | 内部尝试={internal_attempt}/{internal_budget}"
        log_image_request(f"[生图请求] {prefix} | {image_info}")

    @abstractmethod
    async def generate(
        self, image_bytes_list: List[bytes], prompt: str,
        *, request_log: ReferenceLogContext | None = None,
    ) -> Union[bytes, list[bytes], str, dict[str, str]]:
        """
        执行生图调用。
        返回 bytes / list[bytes] 表示图片成功，返回 dict 表示成功的其他媒体，返回 str 表示失败（错误信息）。
        """
        ...

    async def close(self):
        """可选的资源清理。子类按需覆写。"""
        pass
