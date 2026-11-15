import asyncio
import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from astrbot import logger
from astrbot.core import AstrBotConfig
from astrbot.core.platform.astr_message_event import AstrMessageEvent

class PersistenceManager:
    def __init__(self, config: AstrBotConfig, data_dir: Path):
        self.conf = config
        self.data_dir = data_dir
        self.user_counts_file = data_dir / "user_counts.json"
        self.group_counts_file = data_dir / "group_counts.json"
        self.user_daily_counts_file = data_dir / "user_daily_counts.json"
        self.group_daily_counts_file = data_dir / "group_daily_counts.json"
        self.user_checkin_file = data_dir / "user_checkin.json"

        self.user_counts: Dict[str, int] = {}
        self.group_counts: Dict[str, int] = {}
        self.user_daily_counts: Dict[str, Dict[str, Any]] = {}
        self.group_daily_counts: Dict[str, Dict[str, Any]] = {}
        self.user_checkin_data: Dict[str, str] = {}
        self._count_lock = asyncio.Lock()

    async def load_all(self):
        self.user_counts = await self._load_json(self.user_counts_file)
        self.group_counts = await self._load_json(self.group_counts_file)
        self.user_daily_counts = await self._load_json(self.user_daily_counts_file)
        self.group_daily_counts = await self._load_json(self.group_daily_counts_file)
        self.user_checkin_data = await self._load_json(self.user_checkin_file)

    async def _load_json(self, path: Path) -> dict:
        if not path.exists():
            return {}
        loop = asyncio.get_running_loop()
        try:
            content = await loop.run_in_executor(None, path.read_text, "utf-8")
            data = await loop.run_in_executor(None, json.loads, content)
            return {str(k): v for k, v in data.items()} if isinstance(data, dict) else {}
        except Exception as e:
            logger.error(f"加载文件 {path.name} 失败: {e}")
            return {}

    async def _save_json(self, path: Path, data: dict):
        def write_atomically():
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=path.parent,
                    prefix=f".{path.name}.", suffix=".tmp", delete=False,
                ) as stream:
                    temporary = Path(stream.name)
                    json.dump(data, stream, ensure_ascii=False, indent=4)
                temporary.replace(path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

        await asyncio.to_thread(write_atomically)

    async def _commit_count_updates(self, updates: dict[str, dict]) -> None:
        """调用方持有次数锁；写入成功后发布内存状态，失败时回滚已写文件。"""
        async def commit():
            saved = []
            try:
                for attribute, values in updates.items():
                    await self._save_json(getattr(self, f"{attribute}_file"), values)
                    saved.append(attribute)
            except Exception:
                for attribute in reversed(saved):
                    try:
                        await self._save_json(
                            getattr(self, f"{attribute}_file"), getattr(self, attribute)
                        )
                    except Exception as rollback_error:
                        # 回滚也失败时，让内存匹配已落盘状态，避免继续使用旧余额。
                        setattr(self, attribute, updates[attribute])
                        logger.error(f"回滚 {attribute} 失败: {rollback_error}")
                raise
            for attribute, values in updates.items():
                setattr(self, attribute, values)

        task = asyncio.create_task(commit())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # 文件线程不能被取消；等待它完成后再释放次数锁。
            await task
            raise

    def get_user_count(self, user_id: str) -> int:
        permanent = max(0, self.user_counts.get(str(user_id), 0))
        daily_quota = self.conf.get("quota", {}).get("user_daily_fixed_quota", 0)
        if daily_quota <= 0:
            return permanent

        today = datetime.now().strftime("%Y-%m-%d")
        daily_data = self.user_daily_counts.get(str(user_id), {})
        used_today = daily_data.get("count", 0) if daily_data.get("date") == today else 0
        return permanent + max(0, daily_quota - used_today)

    async def decrease_user_count(self, user_id: str):
        await self.adjust_user_count(user_id, -1)

    async def adjust_user_count(self, user_id: str, count: int) -> dict[str, int]:
        """正数增加永久次数；负数依次扣永久、今日额度，最低扣至零。"""
        if isinstance(count, bool) or not isinstance(count, int):
            raise ValueError("次数必须为整数。")
        async with self._count_lock:
            return await self._adjust_user_count_locked(str(user_id), count)

    async def _adjust_user_count_locked(self, uid: str, count: int) -> dict[str, int]:
        permanent = max(0, self.user_counts.get(uid, 0))
        daily_quota = max(0, self.conf.get("quota", {}).get("user_daily_fixed_quota", 0))
        today = datetime.now().strftime("%Y-%m-%d")
        daily_data = self.user_daily_counts.get(uid, {})
        used_today = max(0, daily_data.get("count", 0)) if daily_data.get("date") == today else 0
        daily_remaining = max(0, daily_quota - used_today)
        applied = count if count >= 0 else -min(-count, permanent + daily_remaining)
        permanent_delta = applied if applied >= 0 else -min(-applied, permanent)
        daily_deducted = max(0, permanent_delta - applied)
        updates = {}
        if permanent_delta:
            updates["user_counts"] = {**self.user_counts, uid: permanent + permanent_delta}
        if daily_deducted:
            updates["user_daily_counts"] = {
                **self.user_daily_counts, uid: {"date": today, "count": used_today + daily_deducted},
            }
        if updates:
            await self._commit_count_updates(updates)
        return {
            "applied": applied,
            "permanent": permanent + permanent_delta,
            "daily": daily_remaining - daily_deducted,
            "remaining": permanent + daily_remaining + applied,
        }

    def get_group_count(self, group_id: str) -> int:
        permanent = self.group_counts.get(str(group_id), 0)
        daily_quota = self.conf.get("quota", {}).get("group_daily_fixed_quota", 0)
        if daily_quota <= 0:
            return permanent

        today = datetime.now().strftime("%Y-%m-%d")
        daily_data = self.group_daily_counts.get(str(group_id), {})
        used_today = daily_data.get("count", 0) if daily_data.get("date") == today else 0
        return permanent + max(0, daily_quota - used_today)

    async def decrease_group_count(self, group_id: str):
        async with self._count_lock:
            await self._decrease_group_count_locked(group_id)

    async def _decrease_group_count_locked(self, group_id: str):
        gid = str(group_id)
        if self.group_counts.get(gid, 0) > 0:
            await self._commit_count_updates({"group_counts": {
                **self.group_counts, gid: self.group_counts[gid] - 1,
            }})
            return

        daily_quota = self.conf.get("quota", {}).get("group_daily_fixed_quota", 0)
        if daily_quota > 0:
            today = datetime.now().strftime("%Y-%m-%d")
            daily_data = self.group_daily_counts.get(gid, {})
            used_today = daily_data.get("count", 0) if daily_data.get("date") == today else 0
            if used_today < daily_quota:
                await self._commit_count_updates({"group_daily_counts": {
                    **self.group_daily_counts, gid: {"date": today, "count": used_today + 1},
                }})

    def check_count_available(self, user_id: str, group_id: Optional[str]) -> Optional[str]:
        quota = self.conf.get("quota", {})
        user_limit_on = quota.get("enable_user_limit", True)
        group_limit_on = quota.get("enable_group_limit", False) and group_id

        if group_limit_on and self.get_group_count(group_id) > 0:
            return None
        if user_limit_on and self.get_user_count(user_id) > 0:
            return None
        if not user_limit_on and not group_limit_on:
            return None

        if user_limit_on and group_limit_on:
            return "❌ 本群和您的个人次数均已用完，请等待次日重置或向管理员索要。"
        if user_limit_on:
            return "❌ 您的个人使用次数已用完，请等待次日重置或向管理员索要。"
        if group_limit_on:
            return "❌ 本群的使用次数已用完，请等待次日重置或向管理员索要。"
        return None

    async def check_and_deduct_count(self, user_id: str, group_id: Optional[str]) -> Optional[str]:
        async with self._count_lock:
            try:
                return await self._check_and_deduct_count_locked(user_id, group_id)
            except Exception as exc:
                logger.error(f"保存生图次数失败: {exc}")
                return "❌ 次数保存失败，请联系管理员查询余额并检查数据目录。"

    async def _check_and_deduct_count_locked(self, user_id: str, group_id: Optional[str]) -> Optional[str]:
        if error := self.check_count_available(user_id, group_id):
            return error

        quota = self.conf.get("quota", {})
        user_limit_on = quota.get("enable_user_limit", True)
        group_limit_on = quota.get("enable_group_limit", False) and group_id
        if group_limit_on and self.get_group_count(group_id) > 0:
            await self._decrease_group_count_locked(group_id)
            return None
        if user_limit_on and self.get_user_count(user_id) > 0:
            await self._adjust_user_count_locked(str(user_id), -1)
            return None
        return None

    async def save_user_checkin(self, user_id: str, date_str: str):
        async with self._count_lock:
            values = {**self.user_checkin_data, str(user_id): date_str}
            # 签到文件沿用既有名称。
            task = asyncio.create_task(self._save_json(self.user_checkin_file, values))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                self.user_checkin_data = values
                raise
            self.user_checkin_data = values

    async def add_permanent_user_count(self, user_id: str, count: int):
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("增加永久次数必须使用非负整数。")
        return await self.adjust_user_count(user_id, count)

    async def add_permanent_group_count(self, group_id: str, count: int):
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("增加永久次数必须使用非负整数。")
        gid = str(group_id)
        async with self._count_lock:
            if count:
                await self._commit_count_updates({"group_counts": {
                    **self.group_counts, gid: max(0, self.group_counts.get(gid, 0)) + count,
                }})


class UsageGuard:
    """Permission, cooldown, and quota checks shared by image and selfie flows."""

    def __init__(self, conf, persistence: PersistenceManager, pipeline):
        self.conf = conf
        self.persistence = persistence
        self.pipeline = pipeline

    async def check_can_use(
        self, event: AstrMessageEvent, is_master: bool
    ) -> Optional[str]:
        if is_master:
            return None

        sender_id = event.get_sender_id()
        group_id = event.get_group_id()

        access_control = self.conf.get("access_control", {})
        if sender_id in access_control.get("user_blacklist", []):
            return ""
        if group_id and group_id in access_control.get("group_blacklist", []):
            return ""
        if access_control.get("user_whitelist", []) and sender_id not in access_control.get(
            "user_whitelist", []
        ):
            return ""
        if (
            group_id
            and access_control.get("group_whitelist", [])
            and group_id not in access_control.get("group_whitelist", [])
        ):
            return ""

        if error_msg := await self.pipeline.check_rate_limit():
            return error_msg
        return self.persistence.check_count_available(sender_id, group_id)

    async def deduct_after_success(
        self, event: AstrMessageEvent, is_master: bool
    ) -> Optional[str]:
        if is_master:
            return None
        return await self.persistence.check_and_deduct_count(
            event.get_sender_id(), event.get_group_id()
        )
