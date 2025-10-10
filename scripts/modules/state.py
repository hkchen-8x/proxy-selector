"""State management for probe results"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional


@dataclass
class ProbeState:
    """单个探测点的状态"""
    probe_name: str
    quality: str  # optimal/suboptimal/blocked
    outbound: Optional[str] = None
    last_check_time: Optional[str] = None  # ISO format timestamp
    reason: Optional[str] = None


class StateManager:
    """状态管理器，用于记录和查询探测状态"""
    def __init__(self, state_file: Path) -> None:
        self._state_file = state_file
        self._states: Dict[str, ProbeState] = {}
        self._load()
    
    def _load(self) -> None:
        """从文件加载状态"""
        if not self._state_file.exists():
            return
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            for name, state_dict in data.items():
                self._states[name] = ProbeState(**state_dict)
            logging.debug("已加载 %d 个探测点状态", len(self._states))
        except Exception as exc:
            logging.warning("状态文件加载失败: %s", exc)
    
    def save(self) -> None:
        """保存状态到文件"""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            data = {name: {
                "probe_name": state.probe_name,
                "quality": state.quality,
                "outbound": state.outbound,
                "last_check_time": state.last_check_time,
                "reason": state.reason,
            } for name, state in self._states.items()}
            self._state_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            logging.debug("已保存 %d 个探测点状态", len(self._states))
        except Exception as exc:
            logging.error("状态文件保存失败: %s", exc)
    
    def update(self, probe_name: str, quality: str, outbound: Optional[str] = None, reason: Optional[str] = None) -> None:
        """更新探测点状态"""
        self._states[probe_name] = ProbeState(
            probe_name=probe_name,
            quality=quality,
            outbound=outbound,
            last_check_time=datetime.now().isoformat(),
            reason=reason,
        )
        self.save()
    
    def get(self, probe_name: str) -> Optional[ProbeState]:
        """获取探测点状态"""
        return self._states.get(probe_name)
    
    def should_skip_suboptimal(self, probe_name: str, skip_hours: int) -> bool:
        """判断次优解是否应该跳过探测"""
        state = self.get(probe_name)
        if not state or state.quality != "suboptimal":
            return False
        
        if not state.last_check_time:
            return False
        
        try:
            last_check = datetime.fromisoformat(state.last_check_time)
            elapsed = datetime.now() - last_check
            hours_elapsed = elapsed.total_seconds() / 3600
            
            if hours_elapsed < skip_hours:
                logging.info("%s 次优解距上次探测 %.1f 小时，跳过本次探测", probe_name, hours_elapsed)
                return True
            else:
                logging.info("%s 次优解距上次探测 %.1f 小时，尝试寻找最优解", probe_name, hours_elapsed)
                return False
        except Exception as exc:
            logging.warning("解析时间戳失败: %s", exc)
            return False

