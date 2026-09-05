import asyncio
import json
import random
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Union
from urllib.parse import urlparse

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

    @abstractmethod
    async def generate(
        self, image_bytes_list: List[bytes], prompt: str
    ) -> Union[bytes, list[bytes], str, dict[str, str]]:
        """
        执行生图调用。
        返回 bytes / list[bytes] 表示图片成功，返回 dict 表示成功的其他媒体，返回 str 表示失败（错误信息）。
        """
        ...

    async def close(self):
        """可选的资源清理。子类按需覆写。"""
        pass
