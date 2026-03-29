import json
import asyncio
import functools
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional
from astrbot import logger
from astrbot.core import AstrBotConfig

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

    async def load_all(self):
        self.user_counts = await self._load_json(self.user_counts_file)
        self.group_counts = await self._load_json(self.group_counts_file)
        self.user_daily_counts = await self._load_json(self.user_daily_counts_file)
        self.group_daily_counts = await self._load_json(self.group_daily_counts_file)
        self.user_checkin_data = await self._load_json(self.user_checkin_file)

    async def _load_json(self, path: Path) -> dict:
        if not path.exists(): return {}
        loop = asyncio.get_running_loop()
        try:
            content = await loop.run_in_executor(None, path.read_text, "utf-8")
            data = await loop.run_in_executor(None, json.loads, content)
            return {str(k): v for k, v in data.items()} if isinstance(data, dict) else {}
        except Exception as e:
            logger.error(f"加载文件 {path.name} 失败: {e}")
            return {}

    async def _save_json(self, path: Path, data: dict):
        loop = asyncio.get_running_loop()
        try:
            json_data = await loop.run_in_executor(None, functools.partial(json.dumps, data, ensure_ascii=False, indent=4))
            await loop.run_in_executor(None, path.write_text, json_data, "utf-8")
        except Exception as e:
            logger.error(f"保存文件 {path.name} 失败: {e}")

    def get_user_count(self, user_id: str) -> int:
        permanent = self.user_counts.get(str(user_id), 0)
        daily_quota = self.conf.get("user_daily_fixed_quota", 0)
        if daily_quota <= 0: return permanent
        
        today = datetime.now().strftime("%Y-%m-%d")
        daily_data = self.user_daily_counts.get(str(user_id), {})
        used_today = daily_data.get("count", 0) if daily_data.get("date") == today else 0
        return permanent + max(0, daily_quota - used_today)

    async def decrease_user_count(self, user_id: str):
        uid = str(user_id)
        if self.user_counts.get(uid, 0) > 0:
            self.user_counts[uid] -= 1
            await self._save_json(self.user_counts_file, self.user_counts)
            return

        daily_quota = self.conf.get("user_daily_fixed_quota", 0)
        if daily_quota > 0:
            today = datetime.now().strftime("%Y-%m-%d")
            daily_data = self.user_daily_counts.get(uid, {})
            used_today = daily_data.get("count", 0) if daily_data.get("date") == today else 0
            if used_today < daily_quota:
                self.user_daily_counts[uid] = {"date": today, "count": used_today + 1}
                await self._save_json(self.user_daily_counts_file, self.user_daily_counts)

    def get_group_count(self, group_id: str) -> int:
        permanent = self.group_counts.get(str(group_id), 0)
        daily_quota = self.conf.get("group_daily_fixed_quota", 0)
        if daily_quota <= 0: return permanent

        today = datetime.now().strftime("%Y-%m-%d")
        daily_data = self.group_daily_counts.get(str(group_id), {})
        used_today = daily_data.get("count", 0) if daily_data.get("date") == today else 0
        return permanent + max(0, daily_quota - used_today)

    async def decrease_group_count(self, group_id: str):
        gid = str(group_id)
        if self.group_counts.get(gid, 0) > 0:
            self.group_counts[gid] -= 1
            await self._save_json(self.group_counts_file, self.group_counts)
            return

        daily_quota = self.conf.get("group_daily_fixed_quota", 0)
        if daily_quota > 0:
            today = datetime.now().strftime("%Y-%m-%d")
            daily_data = self.group_daily_counts.get(gid, {})
            used_today = daily_data.get("count", 0) if daily_data.get("date") == today else 0
            if used_today < daily_quota:
                self.group_daily_counts[gid] = {"date": today, "count": used_today + 1}
                await self._save_json(self.group_daily_counts_file, self.group_daily_counts)

    async def check_and_deduct_count(self, user_id: str, group_id: Optional[str]) -> Optional[str]:
        user_limit_on = self.conf.get("enable_user_limit", True)
        group_limit_on = self.conf.get("enable_group_limit", False) and group_id

        if group_limit_on and self.get_group_count(group_id) > 0:
            await self.decrease_group_count(group_id)
            return None

        if user_limit_on and self.get_user_count(user_id) > 0:
            await self.decrease_user_count(user_id)
            return None

        if not user_limit_on and not group_limit_on: return None

        if user_limit_on and group_limit_on:
            return "❌ 本群和您的个人次数均已用完，请等待次日重置或向管理员索要。"
        elif user_limit_on:
            return "❌ 您的个人使用次数已用完，请等待次日重置或向管理员索要。"
        elif group_limit_on:
            return "❌ 本群的使用次数已用完，请等待次日重置或向管理员索要。"
        return None

    async def save_user_checkin(self, user_id: str, date_str: str):
        self.user_checkin_data[str(user_id)] = date_str
        await self._save_json(self.user_checkin_file, self.user_checkin_data)

    async def add_permanent_user_count(self, user_id: str, count: int):
        uid = str(user_id)
        self.user_counts[uid] = self.user_counts.get(uid, 0) + count
        await self._save_json(self.user_counts_file, self.user_counts)

    async def add_permanent_group_count(self, group_id: str, count: int):
        gid = str(group_id)
        self.group_counts[gid] = self.group_counts.get(gid, 0) + count
        await self._save_json(self.group_counts_file, self.group_counts)