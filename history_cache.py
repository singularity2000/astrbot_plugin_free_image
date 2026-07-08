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
                    f"{cleanup['deleted_count']} 张，释放 {cleanup['deleted_bytes']} bytes。"
                )

            return {"record": record, "cache_entries": cache_entries, "cleanup": cleanup}

    async def clear_cache(self, *, reason: str = "manual") -> dict[str, Any]:
        async with self._lock:
            result = self._cleanup_cache_locked(reason=reason, clear_all=True)
            logger.info(
                f"[FreeImage Cache] 已清理全部缓存：删除 {result['deleted_count']} 张，"
                f"释放 {result['deleted_bytes']} bytes。"
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
                    f"{target_id}，释放 {result['deleted_bytes']} bytes。"
                )
            return result

    async def enforce_limits(self, *, reason: str = "startup") -> dict[str, Any]:
        async with self._lock:
            result = self._cleanup_cache_locked(reason=reason)
            if result["deleted_count"]:
                logger.info(
                    "[FreeImage Cache] 缓存限制清理：删除 "
                    f"{result['deleted_count']} 张，释放 {result['deleted_bytes']} bytes。"
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
            all_records = [dict(record) for record in reversed(self.records)]
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
        self, *, page: int = 1, page_size: int = 24
    ) -> dict[str, Any]:
        async with self._lock:
            self._sync_cache_existence()
            images_all = [
                page_item
                for item in reversed(self.cache_images)
                if (page_item := self._cache_entry_for_page(item))
            ]
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
        mode = str(filters.get("mode") or "").strip()
        model = str(filters.get("model") or "").strip()

        result: list[dict[str, Any]] = []
        for record in records:
            date = self._history_record_date(record)
            if start and date < start:
                continue
            if end and date > end:
                continue
            if user and str(record.get("user_id") or "") != user:
                continue
            if mode and str(record.get("mode") or "") != mode:
                continue
            if model and str(record.get("model") or "") != model:
                continue
            result.append(record)
        return result

    @staticmethod
    def _top_counts(records: list[dict[str, Any]], key: str, limit: int = 8) -> list[list[Any]]:
        counts: dict[str, int] = {}
        for record in records:
            value = str(record.get(key) or "")
            if not value:
                continue
            counts[value] = counts.get(value, 0) + 1
        return [
            [value, count]
            for value, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)[:limit]
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
        modes = sorted({str(item.get("mode") or "") for item in records if item.get("mode")})
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
                if last_tab in {"pipeline", "templates", "selfie", "history"}:
                    prefs["last_tab"] = last_tab
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

        if clear_all:
            to_delete.update(str(item.get("id")) for item in self.cache_images if item.get("id"))
        else:
            max_age_hours = self._positive_float("image_cache_max_age_hours")
            if max_age_hours is not None:
                cutoff = datetime.now() - timedelta(hours=max_age_hours)
                for item in self.cache_images:
                    created_at = self._parse_datetime(item.get("created_at"))
                    if created_at and created_at < cutoff:
                        to_delete.add(str(item.get("id")))

            remaining = [item for item in self.cache_images if item.get("id") not in to_delete]
            max_count = self._positive_int("image_cache_max_count")
            if max_count is not None and len(remaining) > max_count:
                overflow = len(remaining) - max_count
                for item in self._oldest_first(remaining)[:overflow]:
                    to_delete.add(str(item.get("id")))

            remaining = [item for item in self.cache_images if item.get("id") not in to_delete]
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
        for item in self.cache_images:
            item_id = str(item.get("id") or "")
            if item_id in target_ids:
                deleted_ids.add(item_id)
                deleted_count += 1
                deleted_bytes += int(item.get("size_bytes") or 0)
                self._safe_unlink(self._entry_path(item))
            else:
                kept.append(item)

        if not deleted_ids:
            return {
                "reason": reason,
                "deleted_count": 0,
                "deleted_bytes": 0,
                "remaining_count": len(self.cache_images),
                "remaining_bytes": sum(int(item.get("size_bytes") or 0) for item in self.cache_images),
            }

        self.cache_images = kept
        history_changed = self._remove_cache_ids_from_history(deleted_ids)
        if save_when_empty or deleted_count or self.cache_index_file.exists() or self.cache_images:
            self._save_cache_index()
        if history_changed:
            self._save_history()
        return {
            "reason": reason,
            "deleted_count": deleted_count,
            "deleted_bytes": deleted_bytes,
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

    def _safe_unlink(self, path: Path | None) -> None:
        if not path:
            return
        try:
            resolved = path.resolve()
            cache_root = self.cache_dir.resolve()
            if cache_root not in resolved.parents and resolved != cache_root:
                logger.warning(f"[FreeImage Cache] 跳过异常缓存路径: {resolved}")
                return
            if resolved.is_file():
                resolved.unlink()
        except OSError as exc:
            logger.warning(f"[FreeImage Cache] 删除缓存失败: {path} - {exc}")

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
        prefs["last_tab"] = last_tab if last_tab in {"pipeline", "templates", "selfie", "history"} else "pipeline"
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
