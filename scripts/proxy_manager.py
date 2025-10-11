#!/usr/bin/env python3
"""Proxy manager orchestrating Playwright health checks and Xray routing updates."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Sequence
from urllib.parse import urlparse

from modules import (
    AppConfig,
    ConfigError,
    ConfigLoader,
    PlaywrightProbe,
    Probe,
    ProbeOutcome,
    StateManager,
    TelegramNotifier,
    XrayAPIClient,
    XrayAPIError,
    should_send_alert,
)


class ProbeManager:
    def __init__(self, config: AppConfig, xray_test_client: XrayAPIClient, xray_prod_client: XrayAPIClient, timeout_ms: int) -> None:
        self._config = config
        self._xray_test = xray_test_client  # 用于测试候选出站
        self._xray_prod = xray_prod_client  # 用于生产环境规则
        self._playwright = PlaywrightProbe(timeout_ms=timeout_ms, user_agent=config.user_agent)
        self._telegram = TelegramNotifier(config.telegram)
        self._state = StateManager(Path(config.state_file).expanduser().resolve())

    async def run(self) -> None:
        if self._config.user_agent:
            logging.info("使用自定义 User-Agent: %s", self._config.user_agent)
        
        for probe in self._config.probes:
            logging.info("开始拨测: %s", probe.name)
            outcome = await self._playwright.check(probe, self._config.proxy.prod)
            
            if outcome.quality == "optimal":
                await self._handle_optimal(probe, outcome)
            elif outcome.quality == "suboptimal":
                await self._handle_suboptimal(probe, outcome)
            else:  # blocked
                await self._handle_blocked(probe, outcome)

    async def _handle_optimal(self, probe: Probe, outcome: ProbeOutcome) -> None:
        """处理最优解情况"""
        logging.info("✅ 拨测成功 (%s): 状态码 %s - 最优解", probe.name, outcome.status)
        self._state.update(probe.name, "optimal", reason=outcome.reason)

    async def _handle_suboptimal(self, probe: Probe, outcome: ProbeOutcome) -> None:
        """处理次优解情况"""
        logging.warning("⚠️  拨测次优 (%s): %s", probe.name, outcome.reason)
        
        # 检查是否应该跳过次优解的切换尝试
        if self._state.should_skip_suboptimal(probe.name, self._config.suboptimal_skip_hours):
            self._state.update(probe.name, "suboptimal", reason=outcome.reason)
            return
        
        # 时间已到，尝试寻找最优解
        logging.info("次优解已持续较长时间，尝试寻找更好的出站")
        candidate = await self._find_optimal_candidate(probe)
        
        if candidate:
            logging.info("%s 找到最优解出站 %s，切换上线", probe.name, candidate)
            self._state.update(probe.name, "optimal", outbound=candidate, reason=f"从次优解切换到最优解 {candidate}")
            await self._send_outbound_change_alert(probe, candidate, success=True, from_suboptimal=True)
        else:
            logging.warning("%s 未找到最优解，保持当前次优解", probe.name)
            self._state.update(probe.name, "suboptimal", reason=outcome.reason)
            # 检查是否需要发送告警
            if self._should_alert(probe, "suboptimal"):
                await self._send_quality_alert(probe, outcome)

    async def _handle_blocked(self, probe: Probe, outcome: ProbeOutcome) -> None:
        """处理被禁止情况"""
        logging.error("❌ 拨测失败 (%s): %s - 需要切换出站", probe.name, outcome.reason)
        
        candidate = await self._recover_with_candidates(probe)
        if candidate:
            logging.info("%s 使用新出站 %s 已切换上线", probe.name, candidate)
            self._state.update(probe.name, "optimal", outbound=candidate, reason=f"已切换到 {candidate}")
            await self._send_outbound_change_alert(probe, candidate, success=True)
        else:
            logging.error("%s 未找到可用出站, 请人工介入", probe.name)
            self._state.update(probe.name, "blocked", reason="所有候选出站均不可用")
            # 检查是否需要发送告警（blocked 情况总是需要告警）
            if self._should_alert(probe, "blocked"):
                await self._send_outbound_change_alert(probe, None, success=False)

    def _should_alert(self, probe: Probe, current_quality: str) -> bool:
        """判断当前质量等级是否应该发送告警"""
        # 优先使用 probe 级别的 alert_level，否则使用全局配置
        alert_level = probe.alert_level or self._config.alert_level
        return should_send_alert(current_quality, alert_level)

    async def _find_optimal_candidate(self, probe: Probe) -> Optional[str]:
        """只寻找最优解出站（无验证码），用于次优解升级"""
        return await self._find_candidate(probe, accept_suboptimal=False)

    async def _recover_with_candidates(self, probe: Probe) -> Optional[str]:
        """寻找可用出站（接受最优解或次优解），用于 blocked 状态恢复"""
        return await self._find_candidate(probe, accept_suboptimal=True)

    async def _find_candidate(self, probe: Probe, accept_suboptimal: bool) -> Optional[str]:
        """
        寻找可用的候选出站
        
        Args:
            probe: 探测配置
            accept_suboptimal: 是否接受次优解（有验证码但可用）
        
        Returns:
            找到的出站名称，如果没找到则返回 None
        """
        candidates = probe.outbound_plan.priority(self._config.default_outbounds)
        if not candidates:
            logging.warning("%s 未配置候选出站", probe.name)
            return None

        test_tag = f"probe-{probe.name}-test"
        prod_tag = f"probe-{probe.name}-prod"
        domain = extract_domain(probe.url)
        rule_template = {
            "type": "field",
            "domain": [f"domain:{domain}"]
        }
        
        # 如果 probe 配置了自定义 rules，合并到模板中
        if probe.rules:
            logging.debug("使用 probe %s 的自定义 rules: %s", probe.name, probe.rules)
            rule_template.update(probe.rules)

        # 记录找到的最优解和次优解
        best_optimal = None
        best_suboptimal = None

        for outbound in candidates:
            logging.info("尝试候选出站 %s", outbound)
            try:
                self._xray_test.remove_routing_rule(test_tag)
            except XrayAPIError:
                logging.debug("测试规则 %s 不存在, 忽略", test_tag)

            test_rule = dict(rule_template)
            test_rule.update({"ruleTag": test_tag, "outboundTag": outbound})
            try:
                self._xray_test.add_routing_rule(test_rule)
            except XrayAPIError as exc:
                logging.error("添加测试规则失败 (%s): %s", outbound, exc)
                continue

            outcome = await self._playwright.check(probe, self._config.proxy.test)
            
            if outcome.quality == "optimal":
                logging.info("✅ 找到最优解: %s", outbound)
                best_optimal = outbound
                # 找到最优解立即使用
                break
            elif outcome.quality == "suboptimal" and accept_suboptimal and not best_suboptimal:
                logging.info("⚠️  找到次优解: %s ", outbound)
                best_suboptimal = outbound
                # 继续寻找是否有更好的最优解
            else:
                logging.warning("❌ 候选出站 %s 测试结果: %s - %s", outbound, outcome.quality, outcome.reason)

        # 清理测试规则
        try:
            self._xray_test.remove_routing_rule(test_tag)
        except XrayAPIError:
            logging.debug("测试规则清理失败, 可能不存在")
        
        # 优先使用最优解，其次使用次优解（如果接受的话）
        selected = best_optimal or (best_suboptimal if accept_suboptimal else None)
        if selected:
            quality_desc = "最优解" if selected == best_optimal else "次优解"
            logging.info("选择出站 %s (%s) 切换生产", selected, quality_desc)
            rule_prod = {}
            rule_prod.update(rule_template)
            rule_prod.pop("inboundTag", None)
            self._promote_outbound(prod_tag, rule_prod, selected)
            return selected
        
        return None

    def _promote_outbound(self, prod_tag: str, rule_template: Dict[str, Any], outbound: str) -> None:
        try:
            self._xray_prod.remove_routing_rule(prod_tag)
            logging.info("已删除旧生产规则: %s", prod_tag)
        except XrayAPIError:
            logging.info("生产规则 %s 不存在, 直接添加", prod_tag)  

        new_rule = dict(rule_template)
        new_rule.update({"tag": prod_tag, "outboundTag": outbound})
        self._xray_prod.add_routing_rule(new_rule)
        logging.info("已添加生产规则 %s -> %s", prod_tag, outbound)

    async def _send_quality_alert(self, probe: Probe, outcome: ProbeOutcome) -> None:
        """发送次优解告警到 Telegram"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        message = (
            f"⚠️ <b>代理质量降级提醒</b>\n\n"
            f"📍 站点: <code>{probe.name}</code>\n"
            f"🌐 URL: <code>{probe.url}</code>\n"
            f"⚡ 状态: 次优解\n"
            f"📝 详情: {outcome.reason}\n"
            f"🕐 时间: {timestamp}\n\n"
            f"当前可访问但需要通过人机验证，建议关注是否影响自动化流程。"
        )
        
        await self._telegram.send_alert(message)

    async def _send_outbound_change_alert(self, probe: Probe, new_outbound: Optional[str], success: bool, from_suboptimal: bool = False) -> None:
        """发送出站切换告警到 Telegram"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        if success and new_outbound:
            if from_suboptimal:
                message = (
                    f"⬆️ <b>代理质量升级成功</b>\n\n"
                    f"📍 站点: <code>{probe.name}</code>\n"
                    f"🌐 URL: <code>{probe.url}</code>\n"
                    f"✅ 新出站: <code>{new_outbound}</code>\n"
                    f"🎯 状态: 次优解 → 最优解\n"
                    f"🕐 时间: {timestamp}\n\n"
                    f"已从次优解切换到最优解。"
                )
            else:
                message = (
                    f"🔄 <b>代理出站已自动切换</b>\n\n"
                    f"📍 站点: <code>{probe.name}</code>\n"
                    f"🌐 URL: <code>{probe.url}</code>\n"
                    f"✅ 新出站: <code>{new_outbound}</code>\n"
                    f"🕐 时间: {timestamp}\n\n"
                    f"原出站拨测失败，已自动切换到可用出站。"
                )
        else:
            message = (
                f"⚠️ <b>代理出站切换失败 - 需人工介入</b>\n\n"
                f"📍 站点: <code>{probe.name}</code>\n"
                f"🌐 URL: <code>{probe.url}</code>\n"
                f"❌ 状态: 所有候选出站均不可用\n"
                f"🕐 时间: {timestamp}\n\n"
                f"请尽快检查网络状态和出站配置！"
            )
        
        await self._telegram.send_alert(message)


def extract_domain(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise ConfigError(f"URL 无法解析域名: {url}")
    return host


def setup_logging(log_file: Path, verbose: bool) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler(sys.stdout)]
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Playwright 拨测与 Xray 出站自动切换")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument("--log-file", default="logs/proxy_manager.log", help="日志文件路径")
    parser.add_argument("--timeout", type=int, default=20000, help="页面加载超时时间(ms)")
    parser.add_argument("--dry-run", action="store_true", help="仅打印命令不执行 xray")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    return parser.parse_args(argv)


async def async_main(args: argparse.Namespace) -> None:
    config_path = Path(args.config).expanduser().resolve()
    config = ConfigLoader.load(config_path)
 
    xray_test_client = XrayAPIClient(config.xray_test, dry_run=args.dry_run)
    xray_prod_client = XrayAPIClient(config.xray_prod, dry_run=args.dry_run)
    logging.info("测试 Xray API: %s", config.xray_test.api)
    logging.info("生产 Xray API: %s", config.xray_prod.api)
    
    manager = ProbeManager(config, xray_test_client, xray_prod_client, timeout_ms=args.timeout)
    await manager.run()


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(Path(args.log_file).expanduser().resolve(), verbose=args.verbose)
    try:
        asyncio.run(async_main(args))
    except ConfigError as exc:
        logging.error("配置错误: %s", exc)
        return 2
    except XrayAPIError as exc:
        logging.error("Xray API 调用失败: %s", exc)
        return 3
    except KeyboardInterrupt:
        logging.warning("用户中断")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

