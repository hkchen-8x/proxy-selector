#!/usr/bin/env python3
"""Proxy manager orchestrating Playwright health checks and Xray routing updates."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
import tempfile
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlparse

from playwright.async_api import (
    Browser,
    Error as PlaywrightError,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)


@dataclass
class ProxySettings:
    prod: str
    test: str


@dataclass
class Expectation:
    status: Optional[int] = None
    title: Optional[str] = None
    body: Optional[str] = None


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
                    ),
                    outbound_plan=OutboundPlan(
                        candidates=outbound_raw.get("candidates", []) or [],
                        tags=outbound_raw.get("tags", []) or [],
                        replace=bool(outbound_raw.get("replace", False)),
                    ),
                    rules=entry.get("rules"),
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
                xray=xray,
                user_agent=raw.get("user_agent"),
                telegram=telegram,
            )
            return config
        except KeyError as exc:
            raise ConfigError(f"配置缺少关键字段: {exc}") from exc


@dataclass
class ProbeOutcome:
    ok: bool
    reason: Optional[str] = None
    status: Optional[int] = None


class PlaywrightProbe:
    def __init__(self, timeout_ms: int, user_agent: Optional[str] = None) -> None:
        self._timeout_ms = timeout_ms
        self._user_agent = user_agent

    async def check(self, probe: Probe, proxy_url: str) -> ProbeOutcome:
        try:
            async with async_playwright() as p:
                browser = await self._launch_browser(p, proxy_url)
                try:
                    context = await browser.new_context(user_agent=self._user_agent) if self._user_agent else await browser.new_context()
                    page = await context.new_page()
                    response = await page.goto(probe.url, wait_until="domcontentloaded", timeout=self._timeout_ms)
                    status = response.status if response else None
                    failure = await self._validate_expectations(probe, page, status)
                  
                    if failure:
                        await page.screenshot(path=f"screenshots/{probe.name}-screenshot.png")
                        return ProbeOutcome(ok=False, reason=failure, status=status)
                    return ProbeOutcome(ok=True, status=status)
                except PlaywrightTimeoutError:
                    await page.screenshot(path=f"screenshots/{probe.name}-screenshot.png")
                    await browser.close()
                    return ProbeOutcome(ok=False, reason="页面加载超时")
                finally:
                    if browser.is_connected():
                        await browser.close()

        except PlaywrightError as exc:
            return ProbeOutcome(ok=False, reason=f"Playwright错误: {exc}")

    async def _launch_browser(self, playwright: Playwright, proxy_url: str) -> Browser:
        return await playwright.chromium.launch(headless=True, proxy={"server": proxy_url})

    async def _validate_expectations(self, probe: Probe, page, status: Optional[int]) -> Optional[str]:
        expectation = probe.expect
        if expectation.status is not None and status != expectation.status:
            return f"状态码不匹配: 期望 {expectation.status}, 实际 {status}"

        if expectation.title:
            title = await page.title()
            if expectation.title not in title:
                return f"标题不匹配: 期望包含 '{expectation.title}', 实际 '{title}'"

        if expectation.body:
            content = await page.content()
            if expectation.body not in content:
                return f"页面内容缺少关键字 '{expectation.body}'"

        return None


class XrayAPIError(RuntimeError):
    pass


class XrayAPIClient:
    def __init__(self, settings: XraySettings, dry_run: bool = False) -> None:
        self._settings = settings
        self._dry_run = dry_run

    def remove_routing_rule(self, tag: str) -> None:
        if not tag:
            return
        self._run("rmrules", f"--server={self._settings.api}", tag)

    def add_routing_rule(self, rule: Dict[str, Any]) -> None:
        configTemplate = {
            'routing': {
                'rules': [rule]
            }
        }
        temp_file = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False)
        try:
            json.dump(configTemplate, temp_file, ensure_ascii=False)
            # json.dump(rule, temp_file, ensure_ascii=False)
            temp_file.flush()
            temp_path = Path(temp_file.name)
        finally:
            temp_file.close()

        try:
            self._run(
                "adrules",
                f"--server={self._settings.api}",
                "--append",
                str(temp_path)
            )
        finally:
            try:
                Path(temp_path).unlink()
            except FileNotFoundError:
                pass

    def _run(self, *args: str) -> None:
        cmd = [self._settings.exe, "api", *args]
        logging.debug("执行 xray 命令: %s", " ".join(cmd))
        if self._dry_run:
            logging.info("dry-run: %s", " ".join(cmd))
            return

        import subprocess

        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            logging.error("xray 命令失败: %s", completed.stderr.strip())
            raise XrayAPIError(completed.stderr.strip())
        if completed.stdout.strip():
            logging.debug("xray 输出: %s", completed.stdout.strip())


class TelegramNotifier:
    def __init__(self, settings: Optional[TelegramSettings]) -> None:
        self._settings = settings

    async def send_alert(self, message: str) -> None:
        if not self._settings or not self._settings.enabled:
            logging.debug("Telegram 通知未启用")
            return

        url = f"https://api.telegram.org/bot{self._settings.bot_token}/sendMessage"
        payload = {
            "chat_id": self._settings.chat_id,
            "text": message,
            "parse_mode": "HTML",
        }

        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        logging.info("Telegram 告警发送成功")
                    else:
                        error_text = await response.text()
                        logging.error("Telegram 告警发送失败: %s - %s", response.status, error_text)
        except ImportError:
            logging.error("Telegram 通知需要安装 aiohttp: pip install aiohttp")
        except Exception as exc:
            logging.error("Telegram 告警发送异常: %s", exc)


class ProbeManager:
    def __init__(self, config: AppConfig, xray_client: XrayAPIClient, timeout_ms: int) -> None:
        self._config = config
        self._xray = xray_client
        self._playwright = PlaywrightProbe(timeout_ms=timeout_ms, user_agent=config.user_agent)
        self._telegram = TelegramNotifier(config.telegram)

    async def run(self) -> None:
        if self._config.user_agent:
            logging.info("使用自定义 User-Agent: %s", self._config.user_agent)
        for probe in self._config.probes:
            logging.info("开始拨测: %s", probe.name)
            outcome = await self._playwright.check(probe, self._config.proxy.prod)
            if outcome.ok:
                logging.info("拨测成功 (%s): 状态码 %s", probe.name, outcome.status)
                continue

            logging.warning("拨测异常 (%s): %s", probe.name, outcome.reason)
        
            candidate = await self._recover_with_candidates(probe)
            if candidate:
                logging.info("%s 使用新出站 %s 已切换上线", probe.name, candidate)
                # 发送成功切换告警
                await self._send_outbound_change_alert(probe, candidate, success=True)
            else:
                logging.error("%s 未找到可用出站, 请人工介入", probe.name)
                # 发送失败告警
                await self._send_outbound_change_alert(probe, None, success=False)

    async def _recover_with_candidates(self, probe: Probe) -> Optional[str]:
        candidates = probe.outbound_plan.priority(self._config.default_outbounds)
        if not candidates:
            logging.warning("%s 未配置候选出站", probe.name)
            return None

        test_tag = f"probe-{probe.name}-test"
        prod_tag = f"probe-{probe.name}-prod"
        domain = extract_domain(probe.url)
        rule_template = {
            "type": "field",
            "domain": [f"domain:{domain}"],
            "inboundTag": ["socks-probe"]
        }
        
        # 如果 probe 配置了自定义 rules，合并到模板中
        if probe.rules:
            logging.debug("使用 probe %s 的自定义 rules: %s", probe.name, probe.rules)
            rule_template.update(probe.rules)

        for outbound in candidates:
            logging.info("尝试候选出站 %s", outbound)
            try:
                self._xray.remove_routing_rule(test_tag)
            except XrayAPIError:
                logging.debug("测试规则 %s 不存在, 忽略", test_tag)

            test_rule = dict(rule_template)
            test_rule.update({"ruleTag": test_tag, "outboundTag": outbound})
            try:
                self._xray.add_routing_rule(test_rule)
            except XrayAPIError as exc:
                logging.error("添加测试规则失败 (%s): %s", outbound, exc)
                continue

            outcome = await self._playwright.check(probe, self._config.proxy.test)
            if outcome.ok:
                logging.info("测试出站 %s 成功, 准备切换生产", outbound)
                rule_prod = {}
                rule_prod.update(rule_template)
                rule_prod.pop("inboundTag")
                self._promote_outbound(prod_tag, rule_prod, outbound)
                try:
                    self._xray.remove_routing_rule(test_tag)
                except XrayAPIError:
                    logging.debug("清理测试规则失败, 可能不存在")
                return outbound

            logging.warning("候选出站 %s 测试失败: %s", outbound, outcome.reason)   

        try:
            self._xray.remove_routing_rule(test_tag)
        except XrayAPIError:
            logging.debug("测试规则清理失败, 可能不存在")
        return None

    def _promote_outbound(self, prod_tag: str, rule_template: Dict[str, Any], outbound: str) -> None:
        try:
            self._xray.remove_routing_rule(prod_tag)
            logging.info("已删除旧生产规则: %s", prod_tag)
        except XrayAPIError:
            logging.info("生产规则 %s 不存在, 直接添加", prod_tag)  

        new_rule = dict(rule_template)
        new_rule.update({"tag": prod_tag, "outboundTag": outbound})
        self._xray.add_routing_rule(new_rule)
        logging.info("已添加生产规则 %s -> %s", prod_tag, outbound)

    async def _send_outbound_change_alert(self, probe: Probe, new_outbound: Optional[str], success: bool) -> None:
        """发送出站切换告警到 Telegram"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        if success and new_outbound:
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
    handlers: List[logging.Handler] = [logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler(sys.stdout)]
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

    xray_client = XrayAPIClient(config.xray, dry_run=args.dry_run)
    manager = ProbeManager(config, xray_client, timeout_ms=args.timeout)
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


