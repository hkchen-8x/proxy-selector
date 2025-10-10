"""Proxy Manager modules"""

# 延迟导入，避免在模块加载时就要求所有依赖
__all__ = [
    "AppConfig",
    "ConfigError",
    "ConfigLoader",
    "Expectation",
    "OutboundPlan",
    "Probe",
    "ProbeOutcome",
    "ProbeState",
    "ProxySettings",
    "PlaywrightProbe",
    "StateManager",
    "TelegramNotifier",
    "TelegramSettings",
    "XrayAPIClient",
    "XrayAPIError",
    "XraySettings",
    "should_send_alert",
    "QUALITY_LEVELS",
]


def __getattr__(name):
    """延迟导入模块成员"""
    if name in __all__:
        # Config 模块
        if name in ["AppConfig", "ConfigError", "ConfigLoader", "Expectation", 
                    "OutboundPlan", "Probe", "ProxySettings", "TelegramSettings", "XraySettings",
                    "should_send_alert", "QUALITY_LEVELS"]:
            from .config import (
                AppConfig, ConfigError, ConfigLoader, Expectation,
                OutboundPlan, Probe, ProxySettings, TelegramSettings, XraySettings,
                should_send_alert, QUALITY_LEVELS
            )
            return locals()[name]
        
        # Notifier 模块
        elif name == "TelegramNotifier":
            from .notifier import TelegramNotifier
            return TelegramNotifier
        
        # Probe 模块
        elif name in ["PlaywrightProbe", "ProbeOutcome"]:
            from .probe import PlaywrightProbe, ProbeOutcome
            return locals()[name]
        
        # State 模块
        elif name in ["ProbeState", "StateManager"]:
            from .state import ProbeState, StateManager
            return locals()[name]
        
        # Xray 模块
        elif name in ["XrayAPIClient", "XrayAPIError"]:
            from .xray_client import XrayAPIClient, XrayAPIError
            return locals()[name]
    
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

