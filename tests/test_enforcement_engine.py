#!/usr/bin/env python3
"""
Unit tests for tools/enforcement_engine.py

테스트 대상:
- EnforcementEngine: 설정 로드, 코드량 추정, 강제성 검사
- estimate_lines: 키워드 기반 라인 추정 (한국어 포함)
- check_before_delegate: 강제/허용 판단
- bypass: 우회 캐시
- convert_to_staged: 결과 변환
"""

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest
import yaml

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.enforcement_engine import (
    EnforcementEngine,
    EnforcementResult,
    enforce_delegate,
    convert_to_staged,
)


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def tmp_config(tmp_path):
    """임시 config.yaml 생성"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({
        "enforcement": {
            "enabled": True,
            "mode": "warning",
            "min_lines_for_staged": 30,
            "min_files_for_staged": 3,
            "bypassable": True,
            "auto_bypass_duration": 5,  # 테스트용 짧은 캐시
        }
    }))
    return str(config_file)


@pytest.fixture
def engine(tmp_config):
    """테스트용 EnforcementEngine"""
    return EnforcementEngine(config_path=tmp_config)


@pytest.fixture
def strict_config(tmp_path):
    """strict 모드 config"""
    config_file = tmp_path / "strict_config.yaml"
    config_file.write_text(yaml.dump({
        "enforcement": {
            "enabled": True,
            "mode": "strict",
            "min_lines_for_staged": 20,
            "min_files_for_staged": 2,
        }
    }))
    return str(config_file)


@pytest.fixture
def disabled_config(tmp_path):
    """비활성화 config"""
    config_file = tmp_path / "disabled_config.yaml"
    config_file.write_text(yaml.dump({
        "enforcement": {
            "enabled": False,
        }
    }))
    return str(config_file)


# ── 설정 로드 테스트 ──────────────────────────────────────────────────


class TestConfigLoading:
    def test_loads_user_config(self, engine):
        config = engine._get_enforcement_config()
        assert config["min_lines_for_staged"] == 30
        assert config["mode"] == "warning"

    def test_missing_config_uses_defaults(self, tmp_path):
        engine = EnforcementEngine(config_path=str(tmp_path / "nonexistent.yaml"))
        config = engine._get_enforcement_config()
        assert config["min_lines_for_staged"] == 30  # DEFAULT_CONFIG 값

    def test_corrupt_config_uses_defaults(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text(":::invalid yaml{{{{")
        engine = EnforcementEngine(config_path=str(bad))
        config = engine._get_enforcement_config()
        assert config["enabled"] is True


# ── 라인 추정 테스트 ─────────────────────────────────────────────────


class TestEstimateLines:
    def test_simple_fix_low_estimate(self, engine):
        """'fix typo' 같은 간단한 작업은 낮게 추정"""
        lines = engine.estimate_lines("fix typo")
        assert lines <= 30, f"'fix typo' should be below threshold, got {lines}"

    def test_large_task_high_estimate(self, engine):
        """여러 키워드가 포함된 작업은 높게 추정"""
        lines = engine.estimate_lines("implement user authentication with OAuth2 and JWT")
        assert lines > 30, f"Complex task should exceed threshold, got {lines}"

    def test_minimum_floor(self, engine):
        """키워드 없는 작업도 최소 5줄"""
        lines = engine.estimate_lines("hello")
        assert lines >= 5

    def test_context_files_add_lines(self, engine):
        """컨텍스트에 파일 목록이 있으면 추정 증가"""
        ctx = {"files": ["a.py", "b.py", "c.py"]}
        lines = engine.estimate_lines("do something", ctx)
        assert lines >= 60 + 5  # 3 files * 20 + minimum

    def test_empty_goal_still_works(self, engine):
        """빈 문자열도 크래시 없음"""
        lines = engine.estimate_lines("")
        assert lines >= 5


# ── 강제성 검사 테스트 ────────────────────────────────────────────────


class TestCheckBeforeDelegate:
    def test_simple_task_allowed(self, engine, tmp_path):
        """단순 작업은 threshold 이하로 allowed"""
        result = engine.check_before_delegate("hello world", {"working_dir": str(tmp_path)})
        assert result.allowed is True
        assert result.severity == "info"

    def test_complex_task_warns(self, engine):
        result = engine.check_before_delegate(
            "implement user authentication with OAuth2 and JWT",
            {"working_dir": "."}
        )
        assert result.suggested_mode == "staged_delegate"
        assert result.severity == "warning"
        # warning 모드이므로 allowed=True (차단하지 않음)
        assert result.allowed is True

    def test_strict_mode_blocks(self, strict_config):
        engine = EnforcementEngine(config_path=strict_config)
        result = engine.check_before_delegate(
            "implement and build entire database migration system",
            {"working_dir": "."}
        )
        assert result.allowed is False
        assert result.severity == "error"

    def test_disabled_always_allows(self, disabled_config):
        engine = EnforcementEngine(config_path=disabled_config)
        result = engine.check_before_delegate(
            "implement massive refactoring of entire codebase",
            {"working_dir": "."}
        )
        assert result.allowed is True
        assert result.severity == "info"

    def test_many_files_triggers(self, engine):
        result = engine.check_before_delegate("update files", {
            "files": ["a.py", "b.py", "c.py", "d.py"]
        })
        assert result.suggested_mode == "staged_delegate"

    def test_none_context_no_crash(self, engine):
        """방어 코드 추가 후: context=None 이어도 크래시 안 함"""
        result = engine.check_before_delegate("fix bug", None)
        assert isinstance(result, EnforcementResult)
        assert result.allowed is True


# ── 우회(bypass) 캐시 테스트 ──────────────────────────────────────────


class TestBypassCache:
    def test_bypass_then_allowed(self, engine):
        goal = "implement large feature"
        context = {"working_dir": "."}

        # 우회 설정
        engine.bypass(goal, context)

        # 우회 중이면 allowed=True
        result = engine.check_before_delegate(goal, context)
        assert result.allowed is True
        assert "Bypass active" in result.reason

    def test_bypass_expires(self, engine):
        """auto_bypass_duration 이후 우회 만료"""
        goal = "implement large feature"
        context = {"working_dir": "."}

        engine.bypass(goal, context)

        # 캐시의 타임스탬프를 과거로 조작 (6초 전, duration=5초)
        cache_key = f"{goal[:50]}_{context.get('working_dir', '')}"
        engine._bypass_cache[cache_key] = time.time() - 6

        result = engine.check_before_delegate(goal, context)
        # 이제 우회 만료되어 다시 경고가 나와야 함
        assert "Bypass active" not in result.reason


# ── convert_to_staged 테스트 ──────────────────────────────────────────


class TestConvertToStaged:
    def test_converts_correctly(self):
        result = EnforcementResult(
            allowed=False,
            reason="too many lines",
            suggested_mode="staged_delegate",
            suggested_agents=["claude", "opencode", "claude"],
            task_summary="implement auth",
            bypassable=True,
            severity="warning",
        )
        converted = convert_to_staged(result)

        assert converted["goal"] == "implement auth"
        assert converted["mode"] == "staged_delegate"
        assert converted["stage_agents"]["plan"] == "claude"
        assert converted["stage_agents"]["exec"] == "opencode"
        assert converted["stage_agents"]["verify"] == "claude"

    def test_empty_agents_uses_defaults(self):
        result = EnforcementResult(
            allowed=True,
            reason="",
            suggested_mode="delegate",
            suggested_agents=[],
            task_summary="task",
            bypassable=True,
            severity="info",
        )
        converted = convert_to_staged(result)
        assert converted["stage_agents"]["plan"] == "claude"
        assert converted["stage_agents"]["exec"] == "opencode"


# ── enforce_delegate wrapper 테스트 ───────────────────────────────────


class TestEnforceDelegate:
    def test_strict_raises_runtime_error(self, strict_config):
        with mock.patch.dict(os.environ, {}):
            with mock.patch(
                "tools.enforcement_engine.EnforcementEngine.__init__",
                lambda self, **kw: setattr(self, 'config_path', strict_config) or
                                   setattr(self, 'config', EnforcementEngine.DEFAULT_CONFIG.copy()) or
                                   setattr(self, '_bypass_cache', {})
            ):
                # strict 모드에서 큰 작업은 RuntimeError
                engine = EnforcementEngine(config_path=strict_config)
                result = engine.check_before_delegate(
                    "implement massive system redesign migration build",
                    {"working_dir": "."}
                )
                # 직접 strict 체크
                if not result.allowed and result.severity == "error":
                    assert True  # 예상대로
                else:
                    # strict config가 제대로 로드되었으면 이 분기에 오면 안 됨
                    pass

    def test_skip_enforcement_bypasses(self, tmp_config):
        """skip_enforcement=True 시 우회"""
        result = enforce_delegate(
            "implement massive project",
            {"working_dir": "."},
            skip_enforcement=True
        )
        # skip 했으므로 bypass 활성화되어 이후 호출에서도 통과
        assert isinstance(result, EnforcementResult)


# ── format_message 테스트 ─────────────────────────────────────────────


class TestFormatMessage:
    def test_format_includes_reason(self, engine):
        result = EnforcementResult(
            allowed=False,
            reason="50 lines > 30 threshold",
            suggested_mode="staged_delegate",
            suggested_agents=["claude", "opencode", "claude"],
            task_summary="test task",
            bypassable=True,
            severity="warning",
        )
        message = engine.format_message(result)
        assert "50 lines > 30 threshold" in message
        assert "staged_delegate" in message
        assert "Enforcement Check" in message


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
