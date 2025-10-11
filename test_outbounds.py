#!/usr/bin/env python3

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from scripts.modules.config import ConfigLoader, Expectation, Probe, XraySettings
from scripts.modules.probe import PlaywrightProbe
from scripts.modules.xray_client import XrayAPIClient, XrayAPIError


@dataclass
class TestResult:
    outbound: str
    address: str
    probe_name: str
    success: bool
    quality: str
    latency_ms: int
    request_latency_ms: int = 0
    reason: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'outbound': self.outbound,
            'address': self.address,
            'probe_name': self.probe_name,
            'success': self.success,
            'quality': self.quality,
            'latency_ms': self.latency_ms,
            'request_latency_ms': self.request_latency_ms,
            'reason': self.reason
        }


class OutboundTester:
    def __init__(self, config_path: str, outbounds_config_path: str, output_file: str = "test_results.json"):
        self.config = ConfigLoader.load(Path(config_path))
        with open(outbounds_config_path, 'r', encoding='utf-8') as f:
            self.outbounds_config = json.load(f)
        # 使用测试环境的 xray 配置
        self.xray = XrayAPIClient(self.config.xray_test)
        self.playwright = PlaywrightProbe(timeout_ms=30000, user_agent=self.config.user_agent)
        self.results: List[TestResult] = []
        self.output_file = output_file

    def _get_test_outbounds(self) -> List[Dict[str, Any]]:
        return self.outbounds_config.get('outbounds', [])

    def _add_outbound(self, outbound: Dict[str, Any]) -> None:
        import subprocess
        import tempfile
        
        config = {'outbounds': [outbound]}
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', suffix='.json', delete=False) as f:
            json.dump(config, f)
            temp_path = f.name
        
        try:
            cmd = [self.config.xray_test.exe, 'api', 'ado', f'--server={self.config.xray_test.api}',  temp_path]
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                raise XrayAPIError(f"添加 outbound 失败: {result.stderr}")
        finally:
            Path(temp_path).unlink(missing_ok=True)

    def _remove_outbound(self, tag: str) -> None:
        import subprocess
        
        cmd = [self.config.xray_test.exe, 'api', 'rmo', f'--server={self.config.xray_test.api}', tag]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            logging.warning(f"删除 outbound {tag} 失败: {result.stderr}")

    def _get_outbound_address(self, outbound_tag: str) -> str:
        for ob in self.outbounds_config.get('outbounds', []):
            if ob.get('tag') == outbound_tag:
                settings = ob.get('settings', {})
                servers = settings.get('servers', [])
                if servers:
                    addr = servers[0].get('address', '')
                    port = servers[0].get('port', '')
                    return f"{addr}:{port}"
        return ""

    async def test_outbound(self, outbound_tag: str, address: str) -> None:
        test_tag = f"test-rule-{outbound_tag}"
        
        for probe in self.config.probes:
            logging.info(f"测试 {outbound_tag} -> {probe.name}")
            
            rule = {
                'type': 'field',
                'ruleTag': test_tag,
                'outboundTag': outbound_tag
            }
            
            if probe.rules:
                rule.update(probe.rules)
            else:
                from urllib.parse import urlparse
                domain = urlparse(probe.url).hostname
                rule['domain'] = [f'domain:{domain}']
            
            try:
                self.xray.remove_routing_rule(test_tag)
            except XrayAPIError:
                pass
            
            try:
                self.xray.add_routing_rule(rule)
            except XrayAPIError as e:
                logging.error(f"添加测试规则失败: {e}")
                self.results.append(TestResult(
                    outbound=outbound_tag,
                    address=address,
                    probe_name=probe.name,
                    success=False,
                    quality='blocked',
                    latency_ms=0,
                    request_latency_ms=0,
                    reason=f"路由规则失败: {e}"
                ))
                continue
            
            start = time.time()
            outcome = await self.playwright.check(probe, self.config.proxy.test)
            latency = int((time.time() - start) * 1000)
            
            self.results.append(TestResult(
                outbound=outbound_tag,
                address=address,
                probe_name=probe.name,
                success=outcome.ok,
                quality=outcome.quality,
                latency_ms=latency,
                request_latency_ms=outcome.request_latency_ms,
                reason=outcome.reason if not outcome.ok else ""
            ))
            
            try:
                self.xray.remove_routing_rule(test_tag)
            except XrayAPIError:
                pass

    async def run(self) -> None:
        test_outbounds = self._get_test_outbounds()
        logging.info(f"找到 {len(test_outbounds)} 个待测试 outbound")
        
        added_tags = []
        
        for ob in test_outbounds:
            tag = ob['tag']
            try:
                self._add_outbound(ob)
                added_tags.append(tag)
                logging.info(f"添加 outbound: {tag}")
            except XrayAPIError as e:
                logging.error(f"添加 outbound {tag} 失败: {e}")
                continue
        
        for tag in added_tags:
            address = self._get_outbound_address(tag)
            await self.test_outbound(tag, address)
        
        for tag in added_tags:
            self._remove_outbound(tag)
            logging.info(f"删除 outbound: {tag}")
        
        self._print_results()

    def _save_json_results(self) -> None:
        from datetime import datetime
        
        output = {
            'test_time': datetime.now().isoformat(),
            'results': [r.to_dict() for r in self.results]
        }
        
        with open(self.output_file, 'w', encoding='utf-8') as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        
        logging.info(f"详细结果已保存到: {self.output_file}")

    def _print_results(self) -> None:
        by_outbound: Dict[str, List[TestResult]] = {}
        for r in self.results:
            if r.outbound not in by_outbound:
                by_outbound[r.outbound] = []
            by_outbound[r.outbound].append(r)
        
        # 保存详细结果为JSON
        self._save_json_results()
        
        print("\n" + "="*150)
        print("测试结果汇总")
        print("="*150)
        
        header = f"{'节点标签':<35} {'地址':<25} {'最优':<8} {'次优':<8} {'失败':<8} {'平均请求延迟':<15} {'平均总延迟':<12}"
        print(header)
        print("-"*150)
        
        for outbound, results in by_outbound.items():
            optimal_count = sum(1 for r in results if r.quality == 'optimal')
            suboptimal_count = sum(1 for r in results if r.quality == 'suboptimal')
            blocked_count = sum(1 for r in results if r.quality == 'blocked')
            
            success_results = [r for r in results if r.success]
            avg_request_latency = int(sum(r.request_latency_ms for r in success_results) / len(success_results)) if success_results else 0
            avg_total_latency = int(sum(r.latency_ms for r in success_results) / len(success_results)) if success_results else 0
            address = results[0].address if results else ""
            
            print(f"{outbound:<35} {address:<25} {optimal_count:<8} {suboptimal_count:<8} {blocked_count:<8} {avg_request_latency}ms{'':<10} {avg_total_latency}ms")
        

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='测试 Xray outbound 节点质量')
    parser.add_argument('--config', '-c', default='config.json', help='配置文件路径 (默认: config.json)')
    parser.add_argument('--outbounds', '-o', default='test_outbounds_config.json', help='待测试 outbound 配置文件路径 (默认: test_outbounds_config.json)')
    parser.add_argument('--output', '-O', default='test_results.json', help='JSON 输出文件路径 (默认: test_results.json)')
    args = parser.parse_args()
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s'
    )
    
    tester = OutboundTester(args.config, args.outbounds, args.output)
    asyncio.run(tester.run())


if __name__ == '__main__':
    main()

