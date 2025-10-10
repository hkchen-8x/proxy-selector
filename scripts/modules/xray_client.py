"""Xray API client for routing rule management"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict

from .config import XraySettings


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
        config_template = {
            'routing': {
                'rules': [rule]
            }
        }
        temp_file = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False)
        try:
            json.dump(config_template, temp_file, ensure_ascii=False)
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

        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            logging.error("xray 命令失败: %s", completed.stderr.strip())
            raise XrayAPIError(completed.stderr.strip())
        if completed.stdout.strip():
            logging.debug("xray 输出: %s", completed.stdout.strip())

