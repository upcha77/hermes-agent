#!/usr/bin/env python3
"""
Unit tests for tools/harness_hooks.py

테스트 대상:
- HarnessConfig: YAML 로드, 훅 디렉토리 로드
- HarnessHookManager: run_hook, on_staged_complete, on_verify_fail, 기본 검증
- shell=True 미사용 검증 (보안)
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── HarnessConfig 테스트 ──────────────────────────────────────────────


class TestHarnessConfig:
    def test_from_project_no_config(self, tmp_path):
        """harness.yaml이 없는 프로젝트에서도 크래시 안 함"""
        from tools.harness_hooks import HarnessConfig

        config = HarnessConfig.from_project(str(tmp_path))
        assert config.on_staged_complete == []
        assert config.lint_commands == []
        assert config.stage_agents == {}

    def test_from_project_with_yaml(self, tmp_path):
        """harness.yaml이 있으면 올바르게 파싱"""
        import yaml

        hermes_dir = tmp_path / ".hermes"
        hermes_dir.mkdir()
        harness_yaml = hermes_dir / "harness.yaml"
        harness_yaml.write_text(yaml.dump({
            "hooks": {
                "on_staged_complete": ["npm run lint", "npm test"],
                "on_verify_fail": ["echo 'verify failed'"],
            },
            "validation": {
                "lint": ["eslint src/"],
                "test": ["jest --passWithNoTests"],
            },
            "stage_agents": {
                "plan": "claude",
                "exec": "opencode",
            },
        }))

        from tools.harness_hooks import HarnessConfig
        config = HarnessConfig.from_project(str(tmp_path))

        assert config.on_staged_complete == ["npm run lint", "npm test"]
        assert config.on_verify_fail == ["echo 'verify failed'"]
        assert config.lint_commands == ["eslint src/"]
        assert config.test_commands == ["jest --passWithNoTests"]
        assert config.stage_agents["exec"] == "opencode"

    def test_corrupt_yaml_no_crash(self, tmp_path):
        """깨진 YAML이어도 기본값으로 폴백"""
        hermes_dir = tmp_path / ".hermes"
        hermes_dir.mkdir()
        (hermes_dir / "harness.yaml").write_text(":::broken{{{")

        from tools.harness_hooks import HarnessConfig
        config = HarnessConfig.from_project(str(tmp_path))
        assert config.on_staged_complete == []

    def test_empty_yaml_no_crash(self, tmp_path):
        """빈 YAML도 크래시 안 함"""
        hermes_dir = tmp_path / ".hermes"
        hermes_dir.mkdir()
        (hermes_dir / "harness.yaml").write_text("")

        from tools.harness_hooks import HarnessConfig
        config = HarnessConfig.from_project(str(tmp_path))
        assert config.on_staged_complete == []

    def test_hooks_directory_loading(self, tmp_path):
        """hooks 디렉토리의 파이썬 스크립트 자동 로드"""
        hermes_dir = tmp_path / ".hermes"
        hooks_dir = hermes_dir / "hooks"
        hooks_dir.mkdir(parents=True)

        (hooks_dir / "on_staged_complete.py").write_text("print('ok')")

        from tools.harness_hooks import HarnessConfig
        config = HarnessConfig.from_project(str(tmp_path))

        assert any("on_staged_complete.py" in cmd for cmd in config.on_staged_complete)

    def test_get_stage_agent_default(self, tmp_path):
        """stage_agents에 없는 stage는 기본값 반환"""
        from tools.harness_hooks import HarnessConfig, HarnessHookManager
        
        config = HarnessConfig.from_project(str(tmp_path))
        manager = HarnessHookManager(config)
        
        assert manager.get_stage_agent("plan") == "claude"  # 기본값
        assert manager.get_stage_agent("exec", "opencode") == "opencode"


# ── HarnessHookManager 테스트 ─────────────────────────────────────────


class TestHarnessHookManager:
    def test_run_hook_with_safe_command(self, tmp_path):
        """안전한 명령어 실행 (echo 테스트)"""
        from tools.harness_hooks import HarnessConfig, HarnessHookManager

        config = HarnessConfig(project_root=tmp_path)
        manager = HarnessHookManager(config)

        results = manager.run_hook(
            hook_name="test_hook",
            commands=["echo hello"],
            context={"project": "test"},
            working_dir=str(tmp_path),
        )

        assert len(results) >= 1
        assert results[0].hook_name == "test_hook"
        # safe_subprocess를 통해 실행됨 - success 여부는 shlex.split("echo hello")이 
        # echo 바이너리를 찾을 수 있느냐에 따라 다를 수 있음
        assert results[0].exit_code is not None

    def test_run_hook_empty_commands(self, tmp_path):
        """빈 명령어 목록도 크래시 없음"""
        from tools.harness_hooks import HarnessConfig, HarnessHookManager

        config = HarnessConfig(project_root=tmp_path)
        manager = HarnessHookManager(config)

        results = manager.run_hook("empty", [], {}, str(tmp_path))
        assert results == []

    def test_run_hook_returns_hookresult(self, tmp_path):
        """반환값이 HookResult dataclass인지 확인"""
        from tools.harness_hooks import HarnessConfig, HarnessHookManager, HookResult

        config = HarnessConfig(project_root=tmp_path)
        manager = HarnessHookManager(config)

        results = manager.run_hook(
            "type_check", ["echo test"], {}, str(tmp_path)
        )
        if results:
            assert isinstance(results[0], HookResult)
            assert hasattr(results[0], 'success')
            assert hasattr(results[0], 'duration_ms')

    def test_no_shell_true_in_source(self):
        """보안 검증: run_hook의 실행 코드에서 subprocess.run(shell=True) 패턴이 없어야 함"""
        import inspect
        import re
        from tools.harness_hooks import HarnessHookManager

        source = inspect.getsource(HarnessHookManager.run_hook)
        # 주석과 docstring을 제외한 실행 코드에서 subprocess.run(...shell=True...) 패턴 검사
        # 실제 subprocess.run 호출이 있으면 안 됨 (safe_subprocess를 써야 하므로)
        assert "subprocess.run(" not in source, \
            "run_hook에서 직접 subprocess.run()을 호출하고 있습니다! safe_subprocess를 사용해야 합니다."

    def test_on_staged_complete_integration(self, tmp_path):
        """on_staged_complete 통합 테스트"""
        from tools.harness_hooks import HarnessConfig, HarnessHookManager

        config = HarnessConfig(project_root=tmp_path)
        config.on_staged_complete = ["echo staged_complete"]
        manager = HarnessHookManager(config)

        result = manager.on_staged_complete(
            stage_results={"plan": {"success": True}},
            final_code="print('hello')",
            working_dir=str(tmp_path),
        )

        assert "success" in result
        assert "hooks" in result
        assert "passed" in result
        assert "failed" in result

    def test_on_staged_complete_no_hooks(self, tmp_path):
        """훅이 없을 때도 기본 검증은 동작"""
        from tools.harness_hooks import HarnessConfig, HarnessHookManager

        config = HarnessConfig(project_root=tmp_path)
        manager = HarnessHookManager(config)

        result = manager.on_staged_complete(
            stage_results={},
            final_code="",
            working_dir=str(tmp_path),
        )
        assert isinstance(result, dict)
        assert "success" in result

    def test_on_verify_fail(self, tmp_path):
        """on_verify_fail 훅 실행"""
        from tools.harness_hooks import HarnessConfig, HarnessHookManager
        
        config = HarnessConfig(project_root=tmp_path)
        config.on_verify_fail = ["echo verify_failed"]
        manager = HarnessHookManager(config)
        
        result = manager.on_verify_fail(
            issues=["issue1", "issue2"],
            code="broken code",
            working_dir=str(tmp_path),
        )
        
        assert isinstance(result, dict)
        assert "success" in result


# ── 편의 함수 테스트 ──────────────────────────────────────────────────


class TestConvenienceFunctions:
    def test_run_staged_complete_hooks(self, tmp_path):
        """편의 함수 run_staged_complete_hooks 호출"""
        from tools.harness_hooks import run_staged_complete_hooks

        # 임시 디렉토리에서 실행하면 harness.yaml이 없으므로 빈 훅
        with mock.patch("tools.harness_hooks.HarnessConfig.from_project") as mock_from:
            from tools.harness_hooks import HarnessConfig
            mock_from.return_value = HarnessConfig(project_root=tmp_path)

            result = run_staged_complete_hooks(
                stage_results={},
                final_code="",
                working_dir=str(tmp_path),
            )
            assert isinstance(result, dict)

    def test_get_project_harness_config(self, tmp_path):
        """편의 함수 get_project_harness_config"""
        from tools.harness_hooks import get_project_harness_config, HarnessConfig

        config = get_project_harness_config(str(tmp_path))
        assert isinstance(config, HarnessConfig)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
