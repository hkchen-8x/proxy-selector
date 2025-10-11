#!/usr/bin/env python3
"""测试单个目标在指定 outbound 下的访问情况"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from scripts.modules.config import ConfigLoader, Expectation, Probe
from scripts.modules.probe import PlaywrightProbe
from scripts.modules.xray_client import XrayAPIClient, XrayAPIError


async def test_single(url: str, outbound: str, config_path: str = "config.json", 
                     wait_seconds: int = 5, screenshot_dir: str = "screenshots"):
    """测试单个目标"""
    
    # 加载配置
    config = ConfigLoader.load(Path(config_path))
    xray = XrayAPIClient(config.xray_test)
    playwright = PlaywrightProbe(timeout_ms=30000, user_agent=config.user_agent)
    
    # 解析域名
    domain = urlparse(url).hostname
    if not domain:
        print(f"❌ 无法解析域名: {url}")
        return False
    
    # 创建临时探测配置
    probe = Probe(
        name="test-probe",
        url=url,
        expect=Expectation(status=200),
        wait_seconds=wait_seconds
    )
    
    # 创建测试规则
    test_tag = f"test-single-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    rule = {
        'type': 'field',
        'ruleTag': test_tag,
        'domain': [f'domain:{domain}'],
        'outboundTag': outbound,
        'inboundTag': ['socks-probe']
    }
    
    try:
        # 添加路由规则
        logging.info(f"添加测试规则: {test_tag}")
        xray.add_routing_rule(rule)
        
        # 等待规则生效
        await asyncio.sleep(0.5)
        
        # 执行测试
        logging.info(f"开始访问: {url} (出站: {outbound})")
        outcome = await playwright.check(probe, config.proxy.test)
        
        # 显示结果
        print("\n" + "="*60)
        print(f"URL:      {url}")
        print(f"Outbound: {outbound}")
        print(f"状态码:   {outcome.status if outcome.status else 'N/A'}")
        print(f"质量:     {outcome.quality}")
        print(f"结果:     {'✅ 成功' if outcome.ok else '❌ 失败'}")
        if outcome.reason:
            print(f"原因:     {outcome.reason}")
        
        # 保存截图
        if outcome.screenshot_data:
            Path(screenshot_dir).mkdir(exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_domain = domain.replace(".", "_")
            screenshot_path = Path(screenshot_dir) / f"{safe_domain}_{outbound}_{timestamp}.png"
            screenshot_path.write_bytes(outcome.screenshot_data)
            print(f"截图:     {screenshot_path.absolute()}")
        
        print("="*60 + "\n")
        
        return outcome.ok
        
    except XrayAPIError as e:
        print(f"❌ Xray 错误: {e}")
        return False
    except Exception as e:
        print(f"❌ 错误: {e}")
        return False
    finally:
        # 清理规则
        try:
            xray.remove_routing_rule(test_tag)
            logging.info(f"清理测试规则: {test_tag}")
        except XrayAPIError:
            pass


def main():
    parser = argparse.ArgumentParser(description="测试单个目标在指定 outbound 下的访问情况")
    parser.add_argument("--url", required=True, help="目标 URL")
    parser.add_argument("--outbound", required=True, help="出站名称")
    parser.add_argument("--config", default="config.json", help="配置文件 (默认: config.json)")
    parser.add_argument("--wait", type=int, default=5, help="等待时间/秒 (默认: 5)")
    parser.add_argument("--screenshot-dir", default="screenshots", help="截图目录 (默认: screenshots)")
    parser.add_argument("--verbose", action="store_true", help="详细日志")
    
    args = parser.parse_args()
    
    # 设置日志
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )
    
    # 运行测试
    try:
        success = asyncio.run(test_single(
            args.url,
            args.outbound,
            args.config,
            args.wait,
            args.screenshot_dir
        ))
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n用户中断")
        sys.exit(1)


if __name__ == "__main__":
    main()

