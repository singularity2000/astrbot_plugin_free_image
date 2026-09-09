"""Generation history and image cache persistence for plugin Pages."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from astrbot import logger

HISTORY_VERSION = 1
CACHE_VERSION = 1
# Pages 左侧导航的合法标签页，需与前端 TAB_VALUES 保持一致。
PAGE_TABS = {"pipeline", "templates", "selfie", "history", "settings"}


def normalize_history_mode(value: Any) -> str:
    """兼容旧版统计名称；只规范读取副本/新记录，不迁移原有历史文件。"""
    mode = str(value or "").strip()
    return {"text2img": "text2image", "image2img": "image2image"}.get(mode, mode)


def format_size(num_bytes: Any) -> str:
    """把字节数格式化为可读体积，规则与 Pages 前端的 formatBytes 保持一致。"""
    try:
        value = max(0.0, float(num_bytes or 0))
    except (TypeError, ValueError):
        value = 0.0
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(round(value))} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class ImageHistoryCache:
    """Persist generation history and optional cached image files."""

    def __init__(self, conf, data_dir: Path):
        self.conf = conf
        self.data_dir = Path(data_dir)
        self.history_file = self.data_dir / "generation_history.json"
        self.cache_dir = self.data_dir / "cache"
        self.cache_images_dir = self.cache_dir / "images"
        self.cache_index_file = self.cache_dir / "index.json"
        self.page_prefs_file = self.data_dir / "pages_prefs.json"
        self._lock = asyncio.Lock()
        self.records: list[dict[str, Any]] = []
        self.cache_images: list[dict[str, Any]] = []
        self.page_prefs: dict[str, dict[str, Any]] = {}

    async def load_all(self) -> None:
        async with self._lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            history_data = self._read_json(self.history_file, {"records": []})
            cache_data = (
                self._read_json(self.cache_index_file, {"images": []})
                if self.cache_index_file.exists()
                else {"images": []}
            )
            prefs_data = self._read_json(self.page_prefs_file, {"users": {}})
            self.records = self._coerce_list(history_data.get("records"))
            self.cache_images = self._coerce_list(cache_data.get("images"))
            self.page_prefs = self._coerce_nested_dict(prefs_data.get("users"))
            self._sync_cache_existence()
            if self.cache_index_file.exists() or self.cache_images:
                self._save_cache_index()
            if self.page_prefs_file.exists() or self.page_prefs:
                self._save_page_prefs()

    async def record_generation(
        self,
        *,
        user_id: str,
        user_name: str = "",
        group_id: str,
        mode: str,
        request_source: str,
        prompt: str,
        elapsed: float,
        model: str | None,
        display_name: str,
        model_index: int | None = None,
        images: list[bytes] | None = None,
        media_type: str = "image",
        media_url: str = "",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)

            mode = normalize_history_mode(mode)
            record_id = uuid.uuid4().hex
            created_at = datetime.now().isoformat(timespec="seconds")
            image_payloads = list(images or [])
            cache_entries: list[dict[str, Any]] = []
            cache_ids: list[str] = []

            if media_type == "image" and image_payloads and self.cache_enabled():
                self.cache_images_dir.mkdir(parents=True, exist_ok=True)
                for index, image_bytes in enumerate(image_payloads, start=1):
                    cache_entry = self._write_cache_image(
                        record_id=record_id,
                        image_bytes=image_bytes,
                        created_at=created_at,
                        index=index,
                        user_id=user_id,
                        user_name=user_name,
                        group_id=group_id,
                        mode=mode,
                        request_source=request_source,
                        prompt=prompt,
                        elapsed=elapsed,
                        model=model,
                        display_name=display_name,
                    )
                    self.cache_images.append(cache_entry)
                    cache_entries.append(cache_entry)
                    cache_ids.append(cache_entry["id"])

            record: dict[str, Any] = {
                "id": record_id,
                "created_at": created_at,
                "user_id": str(user_id or ""),
                "user_name": str(user_name or ""),
                "group_id": str(group_id or ""),
                "mode": str(mode or ""),
                "request_source": str(request_source or ""),
                "prompt": str(prompt or ""),
                "display_name": str(display_name or ""),
                "elapsed": round(float(elapsed or 0), 3),
                "model": str(model or ""),
                "model_index": model_index,
                "status": "success",
                "media_type": media_type,
                "media_url": media_url,
                "image_count": len(image_payloads),
                "cache_ids": cache_ids,
            }
            if extra:
                record.update(extra)

            self.records.append(record)
            self._save_history()
            if cache_entries or self.cache_index_file.exists() or self.cache_images:
                self._save_cache_index()

            cleanup = self._cleanup_cache_locked(reason="auto")
            if cleanup["deleted_count"]:
                logger.info(
                    "[FreeImage Cache] 自动清理缓存：删除 "
                    f"{cleanup['deleted_count']} 张，释放 {format_size(cleanup['deleted_bytes'])}。"
                )

            return {"record": record, "cache_entries": cache_entries, "cleanup": cleanup}

    async def clear_cache(self, *, reason: str = "manual") -> dict[str, Any]:
        async with self._lock:
            result = self._cleanup_cache_locked(reason=reason, clear_all=True)
            logger.info(
                f"[FreeImage Cache] 已清理普通缓存（保留收藏）：删除 {result['deleted_count']} 张，"
                f"释放 {format_size(result['deleted_bytes'])}。"
            )
            return result

    async def delete_cache_image(self, cache_id: str, *, reason: str = "webui") -> dict[str, Any]:
        async with self._lock:
            target_id = str(cache_id or "").strip()
            if not target_id:
                return {
                    "reason": reason,
                    "deleted_count": 0,
                    "deleted_bytes": 0,
                    "remaining_count": len(self.cache_images),
                    "remaining_bytes": sum(int(item.get("size_bytes") or 0) for item in self.cache_images),
                }
            result = self._delete_cache_ids_locked({target_id}, reason=reason)
            if result["deleted_count"]:
                logger.info(
                    "[FreeImage Cache] 已删除单张缓存："
                    f"{target_id}，释放 {format_size(result['deleted_bytes'])}。"
                )
            return result

    async def set_favorite(self, cache_id: str, favorite: bool) -> bool:
        """收藏属于图片本身；所有 Pages 用户共享保护状态。"""
        async with self._lock:
            self._sync_cache_existence()
            for item in self.cache_images:
                if item.get("id") == cache_id:
                    item["favorite"] = favorite
                    self._save_cache_index()
                    return True
            return False

    async def delete_history_records(self, record_ids: list[str]) -> dict[str, Any]:
        """显式删除记录及图片（含收藏）；文件删除失败时保留对应记录。"""
        async with self._lock:
            targets = set(record_ids)
            records = [record for record in self.records if record.get("id") in targets]
            cache_ids = {str(cid) for record in records for cid in (record.get("cache_ids") or [])}
            cache_ids.update(str(item["id"]) for item in self.cache_images
                             if item.get("record_id") in targets and item.get("id"))
            cleanup = self._delete_cache_ids_locked(cache_ids, reason="history")
            failed_ids = set(cleanup.get("failed_ids", []))
            failed_records = {record["id"] for record in records
                              if failed_ids.intersection(record.get("cache_ids") or [])}
            failed_records.update(item.get("record_id") for item in self.cache_images
                                  if item.get("id") in failed_ids)
            deleted_records = {record["id"] for record in records} - failed_records
            self.records = [record for record in self.records if record.get("id") not in deleted_records]
            self._save_history()
            return {**cleanup, "deleted_records": len(deleted_records),
                    "failed_records": len(failed_records.intersection(targets))}

    async def enforce_limits(self, *, reason: str = "startup") -> dict[str, Any]:
        async with self._lock:
            result = self._cleanup_cache_locked(reason=reason)
            if result["deleted_count"]:
                logger.info(
                    "[FreeImage Cache] 缓存限制清理：删除 "
                    f"{result['deleted_count']} 张，释放 {format_size(result['deleted_bytes'])}。"
                )
            return result

    async def get_history_for_page(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            self._sync_cache_existence()
            cache_by_id = {item.get("id"): item for item in self.cache_images}
            all_records = [
                {**record, "mode": normalize_history_mode(record.get("mode"))}
                for record in reversed(self.records)
            ]
            filtered_records = self._filter_history_records(all_records, filters or {})
            total_count = len(filtered_records)
            page, page_size, total_pages, start, end = self._page_window(
                page, page_size, total_count
            )
            records: list[dict[str, Any]] = []
            for record in filtered_records[start:end]:
                page_record = dict(record)
                cache_items = []
                for cache_id in record.get("cache_ids") or []:
                    item = cache_by_id.get(cache_id)
                    if not item:
                        continue
                    page_item = self._cache_entry_for_page(item)
                    if page_item:
                        cache_items.append(page_item)
                page_record["cache_items"] = cache_items
                page_record["has_local_image"] = bool(cache_items)
                records.append(page_record)
            return {
                "records": records,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "total_count": total_count,
                "stats": self._history_stats(filtered_records),
                "facets": self._history_facets(all_records),
            }

    async def get_cache_for_page(
        self, *, page: int = 1, page_size: int = 24, favorites_only: bool = False
    ) -> dict[str, Any]:
        async with self._lock:
            self._sync_cache_existence()
            images_all = [
                page_item
                for item in reversed(self.cache_images)
                if (page_item := self._cache_entry_for_page(item))
            ]
            favorite_images = [item for item in images_all if item.get("favorite") is True]
            regular_images = [item for item in images_all if item.get("favorite") is not True]
            all_count = len(images_all)
            if favorites_only:
                images_all = favorite_images
            total_count = len(images_all)
            page, page_size, total_pages, start, end = self._page_window(
                page, page_size, total_count
            )
            return {
                "enabled": self.cache_enabled(),
                "max_mb": self._raw_limit("image_cache_max_size_mb"),
                "max_hours": self._raw_limit("image_cache_max_age_hours"),
                "max_count": self._raw_limit("image_cache_max_count"),
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "total_count": total_count,
                "total_bytes": sum(int(item.get("size_bytes") or 0) for item in self.cache_images),
                "all_count": all_count,
                "favorite_count": len(favorite_images),
                "favorite_bytes": sum(int(item.get("size_bytes") or 0) for item in favorite_images),
                "regular_count": len(regular_images),
                "regular_bytes": sum(int(item.get("size_bytes") or 0) for item in regular_images),
                "images": images_all[start:end],
            }

    async def get_page_prefs(self, username: str | None = None) -> dict[str, Any]:
        async with self._lock:
            return dict(self._prefs_for_user(username))

    @staticmethod
    def _page_window(
        page: int, page_size: int, total_count: int
    ) -> tuple[int, int, int, int, int]:
        try:
            page = int(page)
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = int(page_size)
        except (TypeError, ValueError):
            page_size = 20
        page = max(1, page)
        page_size = max(1, min(100, page_size))
        total_pages = max(1, (max(0, total_count) + page_size - 1) // page_size)
        page = min(page, total_pages)
        start = (page - 1) * page_size
        end = start + page_size
        return page, page_size, total_pages, start, end

    @staticmethod
    def _history_record_date(record: dict[str, Any]) -> str:
        return str(record.get("created_at") or record.get("time") or "")[:10]

    def _filter_history_records(
        self, records: list[dict[str, Any]], filters: dict[str, Any]
    ) -> list[dict[str, Any]]:
        start = str(filters.get("start") or "").strip()
        end = str(filters.get("end") or "").strip()
        user = str(filters.get("user") or "").strip()
        mode = normalize_history_mode(filters.get("mode"))
        model = str(filters.get("model") or "").strip()

        keyword = str(filters.get("keyword") or "").strip().casefold()
        favorites_only = filters.get("favorites_only") is True
        favorite_ids = {item.get("id") for item in self.cache_images if item.get("favorite") is True}
        result: list[dict[str, Any]] = []
        for record in records:
            if keyword and keyword not in str(record.get("prompt") or "").casefold():
                continue
            if favorites_only and not favorite_ids.intersection(record.get("cache_ids") or []):
                continue
            date = self._history_record_date(record)
            if start and date < start:
                continue
            if end and date > end:
                continue
            if user and str(record.get("user_id") or "") != user:
                continue
            if mode and normalize_history_mode(record.get("mode")) != mode:
                continue
            if model and str(record.get("model") or "") != model:
                continue
            result.append(record)
        return result

    @staticmethod
    def _top_counts(records: list[dict[str, Any]], key: str) -> list[list[Any]]:
        counts: dict[str, int] = {}
        for record in records:
            value = (
                normalize_history_mode(record.get(key))
                if key == "mode" else str(record.get(key) or "")
            )
            if not value:
                continue
            counts[value] = counts.get(value, 0) + 1
        return [
            [value, count]
            for value, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)
        ]

    def _history_stats(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        today = datetime.now().date().isoformat()
        elapsed_values = []
        users = set()
        for record in records:
            try:
                elapsed_values.append(float(record.get("elapsed") or 0))
            except (TypeError, ValueError):
                pass
            user_id = str(record.get("user_id") or "")
            if user_id:
                users.add(user_id)
        return {
            "total": len(records),
            "today": sum(1 for record in records if self._history_record_date(record) == today),
            "avg_elapsed": round(sum(elapsed_values) / len(elapsed_values), 3)
            if elapsed_values
            else 0,
            "users": len(users),
            "mode_counts": self._top_counts(records, "mode"),
            "model_counts": self._top_counts(records, "model"),
        }

    @staticmethod
    def _history_facets(records: list[dict[str, Any]]) -> dict[str, Any]:
        modes = sorted({normalize_history_mode(item.get("mode")) for item in records if item.get("mode")})
        models = sorted({str(item.get("model") or "") for item in records if item.get("model")})
        user_names: dict[str, str] = {}
        for item in records:
            user_id = str(item.get("user_id") or "")
            if not user_id:
                continue
            user_name = str(item.get("user_name") or "")
            # 后出现的昵称更接近当前平台资料；获取失败则保留已有非空昵称。
            if user_name or user_id not in user_names:
                user_names[user_id] = user_name
        users = [
            {"id": user_id, "name": user_names.get(user_id, "")}
            for user_id in sorted(user_names)
        ]
        return {"modes": modes, "models": models, "users": users}

    async def save_page_prefs(
        self, updates: dict[str, Any], username: str | None = None
    ) -> dict[str, Any]:
        async with self._lock:
            prefs = self._prefs_for_user(username)
            allowed_theme = {"system", "light", "dark"}
            if "theme" in updates:
                theme = str(updates.get("theme") or "").strip().lower()
                if theme in allowed_theme:
                    prefs["theme"] = theme
            if "cache_page_size" in updates:
                try:
                    page_size = int(updates.get("cache_page_size"))
                except (TypeError, ValueError):
                    page_size = 0
                if page_size in {12, 24, 48, 96}:
                    prefs["cache_page_size"] = page_size
            if "history_page_size" in updates:
                try:
                    page_size = int(updates.get("history_page_size"))
                except (TypeError, ValueError):
                    page_size = 0
                if page_size in {10, 20, 50, 100}:
                    prefs["history_page_size"] = page_size
            if "last_tab" in updates:
                last_tab = str(updates.get("last_tab") or "").strip()
                if last_tab in PAGE_TABS:
                    prefs["last_tab"] = last_tab
            for key in ("mode_chart_height", "model_chart_height"):
                if key in updates:
                    try:
                        prefs[key] = max(140, min(800, int(updates[key])))
                    except (TypeError, ValueError, OverflowError):
                        pass
            self._set_prefs_for_user(username, prefs)
            self._save_page_prefs()
            return dict(prefs)

    def get_cache_image_path(self, cache_id: str) -> Path | None:
        for item in self.cache_images:
            if item.get("id") != cache_id:
                continue
            path = self._entry_path(item)
            if path and path.is_file():
                return path
        return None

    def cache_enabled(self) -> bool:
        return bool(self.conf.get("cache", {}).get("enable_image_cache", False))

    def _cleanup_cache_locked(
        self, *, reason: str, clear_all: bool = False
    ) -> dict[str, Any]:
        deleted_count = 0
        deleted_bytes = 0

        self._sync_cache_existence()
        to_delete: set[str] = set()

        # 收藏既不作为清理目标，也不参与数量/体积的额度计算。
        regular_images = [item for item in self.cache_images if item.get("favorite") is not True]
        if clear_all:
            to_delete.update(str(item.get("id")) for item in regular_images if item.get("id"))
        else:
            max_age_hours = self._positive_float("image_cache_max_age_hours")
            if max_age_hours is not None:
                cutoff = datetime.now() - timedelta(hours=max_age_hours)
                for item in regular_images:
                    created_at = self._parse_datetime(item.get("created_at"))
                    if created_at and created_at < cutoff:
                        to_delete.add(str(item.get("id")))

            remaining = [item for item in regular_images if item.get("id") not in to_delete]
            max_count = self._positive_int("image_cache_max_count")
            if max_count is not None and len(remaining) > max_count:
                overflow = len(remaining) - max_count
                for item in self._oldest_first(remaining)[:overflow]:
                    to_delete.add(str(item.get("id")))

            remaining = [item for item in regular_images if item.get("id") not in to_delete]
            max_mb = self._positive_float("image_cache_max_size_mb")
            if max_mb is not None:
                max_bytes = int(max_mb * 1024 * 1024)
                total_bytes = sum(int(item.get("size_bytes") or 0) for item in remaining)
                for item in self._oldest_first(remaining):
                    if total_bytes <= max_bytes:
                        break
                    item_id = str(item.get("id"))
                    to_delete.add(item_id)
                    total_bytes -= int(item.get("size_bytes") or 0)

        result = self._delete_cache_ids_locked(to_delete, reason=reason, save_when_empty=clear_all)
        deleted_count = int(result["deleted_count"])
        deleted_bytes = int(result["deleted_bytes"])
        if not deleted_count and (clear_all or self.cache_index_file.exists() or self.cache_images):
            self._save_cache_index()
        return {
            **result,
            "reason": reason,
            "deleted_count": deleted_count,
            "deleted_bytes": deleted_bytes,
            "remaining_count": len(self.cache_images),
            "remaining_bytes": sum(int(item.get("size_bytes") or 0) for item in self.cache_images),
        }

    def _delete_cache_ids_locked(
        self,
        cache_ids: set[str],
        *,
        reason: str,
        save_when_empty: bool = False,
    ) -> dict[str, Any]:
        target_ids = {str(item_id) for item_id in cache_ids if item_id}
        deleted_count = 0
        deleted_bytes = 0
        if not target_ids:
            return {
                "reason": reason,
                "deleted_count": 0,
                "deleted_bytes": 0,
                "remaining_count": len(self.cache_images),
                "remaining_bytes": sum(int(item.get("size_bytes") or 0) for item in self.cache_images),
            }

        kept: list[dict[str, Any]] = []
        deleted_ids: set[str] = set()
        failed_ids: list[str] = []
        for item in self.cache_images:
            item_id = str(item.get("id") or "")
            if item_id in target_ids:
                if not self._safe_unlink(self._entry_path(item)):
                    failed_ids.append(item_id)
                    kept.append(item)
                    continue
                deleted_ids.add(item_id)
                deleted_count += 1
                deleted_bytes += int(item.get("size_bytes") or 0)
            else:
                kept.append(item)

        self.cache_images = kept
        history_changed = self._remove_cache_ids_from_history(deleted_ids)
        if save_when_empty or deleted_count:
            self._save_cache_index()
        if history_changed:
            self._save_history()
        return {
            "reason": reason,
            "deleted_count": deleted_count,
            "deleted_bytes": deleted_bytes,
            "failed_ids": failed_ids,
            "failed_count": len(failed_ids),
            "remaining_count": len(self.cache_images),
            "remaining_bytes": sum(int(item.get("size_bytes") or 0) for item in self.cache_images),
        }

    def _remove_cache_ids_from_history(self, deleted_ids: set[str]) -> bool:
        changed = False
        for record in self.records:
            cache_ids = record.get("cache_ids")
            if not isinstance(cache_ids, list):
                continue
            next_ids = [item for item in cache_ids if str(item) not in deleted_ids]
            if len(next_ids) != len(cache_ids):
                record["cache_ids"] = next_ids
                changed = True
        return changed

    def _write_cache_image(
        self,
        *,
        record_id: str,
        image_bytes: bytes,
        created_at: str,
        index: int,
        user_id: str,
        user_name: str,
        group_id: str,
        mode: str,
        request_source: str,
        prompt: str,
        elapsed: float,
        model: str | None,
        display_name: str,
    ) -> dict[str, Any]:
        cache_id = uuid.uuid4().hex
        mime_type, extension = self._detect_image_type(image_bytes)
        filename = f"{cache_id}.{extension}"
        relative_path = f"images/{filename}"
        path = self.cache_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(image_bytes)
        return {
            "id": cache_id,
            "record_id": record_id,
            "created_at": created_at,
            "relative_path": relative_path,
            "size_bytes": len(image_bytes),
            "mime_type": mime_type,
            "index": index,
            "user_id": str(user_id or ""),
            "user_name": str(user_name or ""),
            "group_id": str(group_id or ""),
            "mode": str(mode or ""),
            "request_source": str(request_source or ""),
            "prompt": str(prompt or ""),
            "elapsed": round(float(elapsed or 0), 3),
            "model": str(model or ""),
            "display_name": str(display_name or ""),
        }

    def _cache_entry_for_page(self, item: dict[str, Any]) -> dict[str, Any] | None:
        path = self._entry_path(item)
        if not path or not path.is_file():
            return None
        result = dict(item)
        result["mode"] = normalize_history_mode(item.get("mode"))
        result["url"] = f"/api/plug/astrbot_plugin_free_image/get_image?cache_id={item.get('id')}"
        return result

    def _sync_cache_existence(self) -> None:
        self.cache_images = [
            item
            for item in self.cache_images
            if isinstance(item, dict) and self._entry_path(item) and self._entry_path(item).is_file()
        ]

    def _entry_path(self, item: dict[str, Any]) -> Path | None:
        rel = str(item.get("relative_path") or "").replace("\\", "/")
        rel_path = Path(rel)
        if not rel or rel.startswith("/") or rel_path.is_absolute() or ".." in rel_path.parts:
            return None
        path = self.cache_dir / rel_path
        try:
            resolved = path.resolve()
            cache_root = self.cache_dir.resolve()
            if resolved != cache_root and cache_root not in resolved.parents:
                return None
        except OSError:
            return None
        return path

    def _safe_unlink(self, path: Path | None) -> bool:
        if not path:
            return False
        try:
            resolved = path.resolve()
            cache_root = self.cache_dir.resolve()
            if cache_root not in resolved.parents and resolved != cache_root:
                logger.warning(f"[FreeImage Cache] 跳过异常缓存路径: {resolved}")
                return False
            if resolved.exists():
                resolved.unlink()
            return True
        except OSError as exc:
            logger.warning(f"[FreeImage Cache] 删除缓存失败: {path} - {exc}")
            return False

    def _read_json(self, path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
        if not path.exists():
            return dict(fallback)
        try:
            data = json.loads(path.read_text("utf-8"))
            return data if isinstance(data, dict) else dict(fallback)
        except Exception as exc:
            logger.error(f"[FreeImage Cache] 读取 {path.name} 失败: {exc}")
            return dict(fallback)

    def _save_history(self) -> None:
        self._write_json_atomic(
            self.history_file,
            {"version": HISTORY_VERSION, "records": self.records},
        )

    def _save_cache_index(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(
            self.cache_index_file,
            {"version": CACHE_VERSION, "images": self.cache_images},
        )

    def _save_page_prefs(self) -> None:
        self._write_json_atomic(
            self.page_prefs_file,
            {"version": 1, "users": self.page_prefs},
        )

    def _write_json_atomic(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        tmp_path.replace(path)

    @staticmethod
    def _coerce_list(value: Any) -> list[dict[str, Any]]:
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    @staticmethod
    def _coerce_nested_dict(value: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for key, item in value.items():
            if isinstance(item, dict):
                result[str(key)] = dict(item)
        return result

    def _prefs_key(self, username: str | None) -> str:
        user = str(username or "").strip()
        return user or "__default__"

    def _prefs_for_user(self, username: str | None) -> dict[str, Any]:
        key = self._prefs_key(username)
        current = self.page_prefs.get(key, {})
        prefs = dict(current) if isinstance(current, dict) else {}
        theme = str(prefs.get("theme") or "").strip().lower()
        if theme not in {"system", "light", "dark"}:
            prefs["theme"] = "system"
        try:
            page_size = int(prefs.get("cache_page_size", 24))
        except (TypeError, ValueError):
            page_size = 24
        prefs["cache_page_size"] = page_size if page_size in {12, 24, 48, 96} else 24
        try:
            history_page_size = int(prefs.get("history_page_size", 20))
        except (TypeError, ValueError):
            history_page_size = 20
        prefs["history_page_size"] = (
            history_page_size if history_page_size in {10, 20, 50, 100} else 20
        )
        last_tab = str(prefs.get("last_tab") or "").strip()
        prefs["last_tab"] = last_tab if last_tab in PAGE_TABS else "pipeline"
        return prefs

    def _set_prefs_for_user(self, username: str | None, prefs: dict[str, Any]) -> None:
        self.page_prefs[self._prefs_key(username)] = dict(prefs)

    @staticmethod
    def _oldest_first(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(items, key=lambda item: str(item.get("created_at") or ""))

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        try:
            return datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None

    def _raw_limit(self, key: str) -> str:
        value = self.conf.get("cache", {}).get(key, "")
        return "" if value is None else str(value)

    def _positive_float(self, key: str) -> float | None:
        value = self._raw_limit(key).strip()
        if not value:
            return None
        try:
            parsed = float(value)
        except ValueError:
            logger.warning(f"[FreeImage Cache] 配置 {key}={value!r} 不是数字，按不限制处理。")
            return None
        return parsed if parsed > 0 else None

    def _positive_int(self, key: str) -> int | None:
        value = self._raw_limit(key).strip()
        if not value:
            return None
        try:
            parsed = int(float(value))
        except ValueError:
            logger.warning(f"[FreeImage Cache] 配置 {key}={value!r} 不是整数，按不限制处理。")
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _detect_image_type(image_bytes: bytes) -> tuple[str, str]:
        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png", "png"
        if image_bytes.startswith(b"\xff\xd8\xff"):
            return "image/jpeg", "jpg"
        if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
            return "image/webp", "webp"
        if image_bytes.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif", "gif"
        mime_type = mimetypes.guess_type("image.png")[0] or "image/png"
        return mime_type, "png"
