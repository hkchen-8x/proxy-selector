"""Configuration classes and loader"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


# 质量等级映射：数字越大问题越严重
QUALITY_LEVELS = {
    "optimal": 0,      # 最优解：无问题
    "suboptimal": 1,   # 次优解：有验证码但可用
    "blocked": 2,      # 最差解：被禁止
}


def should_send_alert(current_quality: str, alert_level: str) -> bool:
    """
    判断当前质量等级是否应该发送告警
    
    Args:
        current_quality: 当前质量等级 (optimal/suboptimal/blocked)
        alert_level: 告警阈值等级
    
    Returns:
        True 如果应该发送告警，False 否则
    """
    current_level = QUALITY_LEVELS.get(current_quality, 0)
    threshold_level = QUALITY_LEVELS.get(alert_level, 1)
    return current_level >= threshold_level


@dataclass
class ProxySettings:
    prod: str
    test: str


@dataclass
class Expectation:
    status: Optional[int] = None
    title: Optional[str] = None
    body: Optional[str] = None
    text: Optional[str] = None
    # 人机验证特征检测（次优解）
    captcha_keywords: List[str] = field(default_factory=list)
    # 降级期望（次优解也可接受的条件）
    fallback_expect: Optional[Dict[str, Any]] = None
    # 禁止特征检测（最差解）
    must_not: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        result = {}
        if self.status:
            result["status"] = self.status
        if self.title:
            result["title"] = self.title
        if self.body:
            result["body"] = self.body
        
        return result


@dataclass
class OutboundPlan:
    candidates: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    replace: bool = False

    def priority(self, defaults: Sequence[str]) -> List[str]:
        ordered: List[str] = []
        ordered.extend(self.candidates)
        ordered.extend(self.tags)
        if not self.replace:
            ordered.extend(defaults)
        return dedupe_preserve_order(ordered)


@dataclass
class Probe:
    name: str
    url: str
    expect: Expectation
    outbound_plan: OutboundPlan = field(default_factory=OutboundPlan)
    rules: Optional[Dict[str, Any]] = None
    wait_seconds: Optional[int] = None
    alert_level: Optional[str] = None  # 告警等级阈值：optimal/suboptimal/blocked

@dataclass
class XraySettings:
    api: str
    exe: str = "xray"


@dataclass
class TelegramSettings:
    bot_token: str
    chat_id: str
    enabled: bool = True


@dataclass
class AppConfig:
    proxy: ProxySettings
    probes: List[Probe]
    default_outbounds: List[str]
    default_exclude_outbounds: List[str]
    xray: XraySettings
    user_agent: Optional[str] = None
    telegram: Optional[TelegramSettings] = None
    state_file: str = "state.json"
    suboptimal_skip_hours: int = 1
    alert_level: str = "suboptimal"  # 全局告警等级阈值：optimal/suboptimal/blocked


def dedupe_preserve_order(items: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


class ConfigError(RuntimeError):
    """Raised when the configuration file is invalid."""


class ConfigLoader:
    @staticmethod
    def load(path: Path) -> AppConfig:
        if not path.exists():
            raise ConfigError(f"配置文件不存在: {path}")

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"配置文件解析失败: {exc}") from exc

        try:
            proxy_raw = raw["proxy"]
            proxy = ProxySettings(prod=proxy_raw["prod"], test=proxy_raw["test"])

            # 获取全局alert_level
            global_alert_level = raw.get("alert_level", "suboptimal")
            
            probes_raw = raw.get("playwright_probes", [])
            probes: List[Probe] = []
            for entry in probes_raw:
                expect_raw = entry.get("expect", {})
                outbound_raw = entry.get("outbounds", {})
                probe = Probe(
                    name=entry["name"],
                    url=entry["url"],
                    expect=Expectation(
                        status=expect_raw.get("status"),
                        title=expect_raw.get("title"),
                        body=expect_raw.get("body"),
                        captcha_keywords=expect_raw.get("captcha_keywords", []),
                        fallback_expect=expect_raw.get("fallback_expect"),
                        must_not=expect_raw.get("must_not"),
                    ),
                    outbound_plan=OutboundPlan(
                        candidates=outbound_raw.get("candidates", []) or [],
                        tags=outbound_raw.get("tags", []) or [],
                        replace=bool(outbound_raw.get("replace", False)),
                    ),
                    rules=entry.get("rules"),
                    wait_seconds=entry.get("wait_seconds"),
                    alert_level=entry.get("alert_level", global_alert_level),  # 优先使用probe级别，否则使用全局
                )
                probes.append(probe)

            xray_raw = raw.get("xray", {})
            xray = XraySettings(api=xray_raw.get("api", "127.0.0.1:8000"), exe=xray_raw.get("exe", "xray"))

            telegram = None
            telegram_raw = raw.get("telegram")
            if telegram_raw and telegram_raw.get("bot_token") and telegram_raw.get("chat_id"):
                telegram = TelegramSettings(
                    bot_token=telegram_raw["bot_token"],
                    chat_id=telegram_raw["chat_id"],
                    enabled=telegram_raw.get("enabled", True),
                )

            config = AppConfig(
                proxy=proxy,
                probes=probes,
                default_outbounds=raw.get("default_outbounds", []),
                default_exclude_outbounds=raw.get("default_exclude_outbounds", []),
                alert_level=global_alert_level,
                xray=xray,
                user_agent=raw.get("user_agent"),
                telegram=telegram,
                state_file=raw.get("state_file", "state.json"),
                suboptimal_skip_hours=raw.get("suboptimal_skip_hours", 1),
            )
            return config
        except KeyError as exc:
            raise ConfigError(f"配置缺少关键字段: {exc}") from exc

