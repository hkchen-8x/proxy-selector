"""Playwright-based probe implementation"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup
from playwright.async_api import (
    Browser,
    Error as PlaywrightError,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from .config import Probe


@dataclass
class ProbeOutcome:
    ok: bool
    reason: Optional[str] = None
    status: Optional[int] = None
    quality: str = "optimal"  # optimal(最优), suboptimal(次优-有验证码), blocked(最差-被禁止)
    request_latency_ms: int = 0  # 首次请求响应耗时


class PlaywrightProbe:
    def __init__(self, timeout_ms: int, user_agent: Optional[str] = None) -> None:
        self._timeout_ms = timeout_ms
        self._user_agent = user_agent

    async def check(self, probe: Probe, proxy_url: str) -> ProbeOutcome:
        import time
        import asyncio
        
        browser = None
        try:
            async with async_playwright() as p:
                browser = await self._launch_browser(p, proxy_url)
                try:
                    context_options = {}
                    if self._user_agent:
                        context_options["user_agent"] = self._user_agent
                    
                    context = await browser.new_context(**context_options)
                    page = await context.new_page()
                    
                    # 测量纯网络请求延迟（到服务器响应返回）
                    start_time = time.time()
                    response = await page.goto(probe.url, wait_until="commit", timeout=self._timeout_ms)
                    request_latency = int((time.time() - start_time) * 1000)
                    
                    status = response.status if response else None
            
                    if status is None:
                        logging.error("状态码为空: %s", probe.url)
                        return ProbeOutcome(ok=False, reason="状态码为空", status=status, quality="blocked", request_latency_ms=request_latency)

                    # 等待 DOM 加载完成以便后续内容检查
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=self._timeout_ms)
                    except PlaywrightTimeoutError:
                        logging.warning("%s DOM 加载超时，继续检查", probe.name)
                    
                    # 如果配置了等待时间，等待指定秒数（用于等待 JavaScript 动态内容）
                    if probe.wait_seconds is not None and probe.wait_seconds > 0:
                        logging.debug("%s 等待 %d 秒以加载动态内容", probe.name, probe.wait_seconds)
                        await page.wait_for_timeout(probe.wait_seconds * 1000)
                    
                    # 检查质量等级
                    quality_result = await self._check_quality(probe, page, status)
                    quality_result.request_latency_ms = request_latency
                    return quality_result
                    
                except PlaywrightTimeoutError:
                    try:
                        await page.screenshot(path=f"screenshots/{probe.name}-timeout.png")
                    except:
                        pass
                    return ProbeOutcome(ok=False, reason="页面加载超时", quality="blocked")
                finally:
                    # 为 browser.close() 添加超时保护，避免永久阻塞
                    if browser and browser.is_connected():
                        try:
                            await asyncio.wait_for(browser.close(), timeout=5.0)
                        except asyncio.TimeoutError:
                            logging.warning("%s 浏览器关闭超时(5秒)，已跳过", probe.name)
                        except Exception as e:
                            logging.debug("%s 浏览器关闭异常: %s", probe.name, e)

        except PlaywrightError as exc:
            return ProbeOutcome(ok=False, reason=f"Playwright错误: {exc}", quality="blocked")

    async def _launch_browser(self, playwright: Playwright, proxy_url: str) -> Browser:
        return await playwright.chromium.launch(
            headless=True,
            proxy={"server": proxy_url},
            args=[
                '--disable-gpu',  # 禁用GPU，避免GPU进程卡住
                '--disable-dev-shm-usage',  # 避免共享内存问题
                '--disable-hang-monitor',  # 禁用挂起监视器
                '--disable-background-networking',  # 禁用后台网络请求
                '--disable-background-timer-throttling',
                '--disable-renderer-backgrounding',
                '--disable-backgrounding-occluded-windows',
                '--disable-ipc-flooding-protection',
                '--no-first-run',
                '--no-default-browser-check',
            ],
        )

    async def _check_quality(self, probe: Probe, page, status: Optional[int]) -> ProbeOutcome:
        """检查页面质量等级: optimal/suboptimal/blocked"""
        expectation = probe.expect
        content = await page.content()
        
        title = await page.title()
        text = self._extract_text(content)
        
        # 1. 先检查 must_not（禁止特征）- 如果匹配则 blocked
        if expectation.must_not:
            match_result = self._match_dict(expectation.must_not, status, title, content, text)
            if match_result.matched:
                await self._save_screenshot(page, probe.name, "blocked")
                logging.warning("%s 检测到禁止特征: %s", probe.name, match_result.reason)
                return ProbeOutcome(ok=False, reason=match_result.reason, status=status, quality="blocked")

        # 2. 检查基本 expect（最优解）- 如果满足则 optimal
        match_result = self._match_dict(expectation.to_dict(), status, title, content, text)
        if match_result.matched:
            return ProbeOutcome(ok=True, reason="满足期望条件", status=status, quality="optimal")
        else:
            logging.warning("%s 不满足期望条件: %s, dict:%s", probe.name, match_result.reason, f"{expectation.to_dict()}")

        # 3. 检查 fallback_expect（次优解）- 如果满足则 suboptimal
        if expectation.fallback_expect:
            fallback_result = self._match_dict(expectation.fallback_expect, status, title, content, text)
            if fallback_result.matched:
                await self._save_screenshot(page, probe.name, "suboptimal")
                logging.info("%s 满足次优解条件: %s", probe.name, fallback_result.reason)
                return ProbeOutcome(ok=True, reason=fallback_result.reason, status=status, quality="suboptimal")
                    
        # 4. 都不满足，返回 blocked
        await self._save_screenshot(page, probe.name, "blocked")
        return ProbeOutcome(ok=False, reason=match_result.reason, status=status, quality="blocked")
     
    
    def _match_dict(self, config: dict, status: Optional[int], title: str, content: str, text: str) -> "MatchResult":
        """
        匹配字典配置
        
        支持的匹配方式：
        - status: HTTP状态码
        - title: 页面标题（字符串匹配）
        - selector: CSS选择器 + 文本匹配（更精确）
        - contains: 全文本包含（简单匹配）
        """
        # 检查状态码
        matched_reasons = []
        if "status" in config:
            expected_statuses = config["status"] if isinstance(config["status"], list) else [config["status"]]
            if status not in expected_statuses:
                return MatchResult(matched=False, reason=f"状态码不匹配: 期望 {expected_statuses}, 实际 {status}")
            matched_reasons.append(f"status: {status}")
        # 检查标题
        if "title" in config:
            title_match = False
            expected_titles = config["title"] if isinstance(config["title"], list) else [config["title"]]
            for expected_title in expected_titles:
                if expected_title.lower() in title.lower():
                    title_match = True
                    matched_reasons.append(f"title: {expected_title}")
                    break
            if not title_match:
                return MatchResult(matched=False, reason=f"标题不匹配: 期望包含 {expected_titles}")
        
        # 检查 CSS 选择器匹配（精确查找）
        if "selector" in config:
            selector_match =  self._match_selector(content, config["selector"], config.get("text"))
            if not selector_match.matched:
                return selector_match
            else:
                matched_reasons.append(f"selector:{selector_match.reason}")
        
        # 检查全文本包含（简单匹配）
        if "contains" in config:
            contain_matched = False
            expected_texts = config["contains"] if isinstance(config["contains"], list) else [config["contains"]]
            for expected_text in expected_texts:
                if expected_text.lower() in text.lower():
                    matched_reasons.append(f"contains:{expected_text}")
                    contain_matched = True
                    break
            if not contain_matched:
                return MatchResult(matched=False, reason=f"文本不匹配: 期望包含 {expected_texts}") 
        
        # 如果没有任何检查项，认为不匹配
        return MatchResult(matched=True, reason=';'.join(matched_reasons))
    
    def _match_selector(self, html_content: str, selector_config, text_pattern=None) -> "MatchResult":
        """
        使用 CSS 选择器进行精确匹配
        
        Args:
            html_content: HTML内容
            selector_config: 选择器配置，可以是字符串或字典
            text_pattern: 可选的文本匹配模式
        
        Examples:
            # 简单选择器
            {"selector": ".error-message"}
            
            # 选择器 + 文本匹配
            {"selector": ".error-message", "text": "Access Denied"}
            
            # 高级配置
            {"selector": {"css": ".message", "text": "error", "attr": "class"}}
        """
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # 如果是字符串，直接作为CSS选择器
            if isinstance(selector_config, str):
                css_selector = selector_config
                elements = soup.select(css_selector)
                
                if not elements:
                    return MatchResult(matched=False, reason=f"未找到选择器: {css_selector}")
                
                # 如果指定了文本匹配
                if text_pattern:
                    patterns = text_pattern if isinstance(text_pattern, list) else [text_pattern]
                    for element in elements:
                        element_text = element.get_text(strip=True)
                        for pattern in patterns:
                            if pattern.lower() in element_text.lower():
                                return MatchResult(matched=True, reason=f"选择器 {css_selector} 匹配文本: {pattern}")
                    return MatchResult(matched=False, reason=f"选择器 {css_selector} 未找到文本: {patterns}")
                
                # 只要找到元素就算匹配
                return MatchResult(matched=True, reason=f"找到选择器: {css_selector}")
            
            # 如果是字典，支持更高级的配置
            elif isinstance(selector_config, dict):
                css_selector = selector_config.get("css")
                if not css_selector:
                    return MatchResult(matched=False, reason="选择器配置缺少 css 字段")
                
                elements = soup.select(css_selector)
                if not elements:
                    return MatchResult(matched=False, reason=f"未找到选择器: {css_selector}")
                
                # 检查属性
                if "attr" in selector_config:
                    attr_name = selector_config["attr"]
                    attr_value = selector_config.get("attr_value")
                    for element in elements:
                        if element.has_attr(attr_name):
                            if attr_value is None or attr_value in element[attr_name]:
                                return MatchResult(matched=True, reason=f"找到属性 {attr_name}")
                    return MatchResult(matched=False, reason=f"未找到属性: {attr_name}")
                
                # 检查文本
                if "text" in selector_config:
                    patterns = selector_config["text"] if isinstance(selector_config["text"], list) else [selector_config["text"]]
                    for element in elements:
                        element_text = element.get_text(strip=True)
                        for pattern in patterns:
                            if pattern.lower() in element_text.lower():
                                return MatchResult(matched=True, reason=f"选择器 {css_selector} 匹配文本: {pattern}")
                    return MatchResult(matched=False, reason=f"选择器 {css_selector} 未找到文本: {patterns}")
                
                return MatchResult(matched=True, reason=f"找到选择器: {css_selector}")
            
            return MatchResult(matched=False, reason="无效的选择器配置")
            
        except Exception as exc:
            logging.warning("选择器匹配失败: %s", exc)
            return MatchResult(matched=False, reason=f"选择器匹配异常: {exc}")
    
    def _extract_text(self, html_content: str) -> str:
        """使用 BeautifulSoup 提取页面纯文本内容"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            # 移除 script 和 style 标签
            for script in soup(["script", "style"]):
                script.decompose()
            # 获取纯文本并去除多余空白
            text = soup.get_text(separator=' ', strip=True)
            return ' '.join(text.split())
        except Exception as exc:
            logging.warning("提取文本失败: %s", exc)
            return ""
    
    async def _save_screenshot(self, page, probe_name: str, quality: str) -> None:
        """保存截图"""
        try:
            Path("screenshots").mkdir(exist_ok=True)
            await page.screenshot(path=f"screenshots/{probe_name}-{quality}.png")
        except Exception:
            pass


@dataclass
class MatchResult:
    """匹配结果"""
    matched: bool
    reason: str

