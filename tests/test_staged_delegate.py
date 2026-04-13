#!/usr/bin/env python3
"""
Unit tests for tools/staged_delegate_tool.py

테스트 대상:
- StagedDelegateOrchestrator: 내부 로직 (프롬프트 생성, 작업 분할, 완성도 추정, 결과 포맷)
- StageResult / UnifiedResult: 데이터 클래스 직렬화
- 폴백 로직: FALLBACK_CHAIN 구조, 깊이 제한
- 상수 일관성: 중복 없음, 유효 에이전트 타입
"""

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.staged_delegate_tool import (
    AGENT_TYPES,
    DEFAULT_STAGE_AGENTS,
    FALLBACK_CHAIN,
    MAX_FALLBACK_DEPTH,
    FALLBACK_INITIAL_DELAY,
    FALLBACK_MAX_DELAY,
    StageResult,
    UnifiedResult,
    StagedDelegateOrchestrator,
    staged_delegate,
)


# ── 상수 일관성 테스트 ────────────────────────────────────────────────


class TestConstants:
    def test_fallback_chain_not_duplicated(self):
        """상수가 중복 정의되지 않았음을 간접 검증 — 소스 코드에서 확인"""
        import inspect
        source = inspect.getsource(sys.modules['tools.staged_delegate_tool'])
        # FALLBACK_CHAIN = { 가 한 번만 나타나야 함
        count = source.count("FALLBACK_CHAIN = {")
        assert count == 1, f"FALLBACK_CHAIN이 {count}번 정의됨 (1번이어야 함)"

    def test_max_fallback_depth_matches_chain(self):
        """MAX_FALLBACK_DEPTH가 폴백 체인 길이와 일치"""
        # opencode → cline → claude = 3단계
        assert MAX_FALLBACK_DEPTH == 3

    def test_fallback_chain_targets_exist_in_agent_types(self):
        """폴백 체인의 모든 대상이 AGENT_TYPES에 존재"""
        for source, target in FALLBACK_CHAIN.items():
            assert source in AGENT_TYPES, f"Fallback source '{source}' not in AGENT_TYPES"
            assert target in AGENT_TYPES, f"Fallback target '{target}' not in AGENT_TYPES"

    def test_default_stage_agents_valid(self):
        """기본 스테이지 에이전트가 모두 유효한 타입"""
        for stage, agent in DEFAULT_STAGE_AGENTS.items():
            assert agent in AGENT_TYPES, f"Stage '{stage}' uses unknown agent '{agent}'"

    def test_fallback_chain_no_cycle(self):
        """폴백 체인에 순환이 없음"""
        visited = set()
        for start in FALLBACK_CHAIN:
            current = start
            path = []
            while current in FALLBACK_CHAIN:
                if current in visited:
                    break  # 다른 시작점에서 이미 검증됨
                if current in path:
                    pytest.fail(f"Cycle detected: {' → '.join(path)} → {current}")
                path.append(current)
                current = FALLBACK_CHAIN[current]
            visited.update(path)

    def test_fallback_delays_sane(self):
        """폴백 지연 값이 합리적"""
        assert FALLBACK_INITIAL_DELAY > 0
        assert FALLBACK_MAX_DELAY >= FALLBACK_INITIAL_DELAY
        assert FALLBACK_MAX_DELAY <= 60  # 1분 이내


# ── StageResult 테스트 ────────────────────────────────────────────────


class TestStageResult:
    def test_to_dict(self):
        result = StageResult(
            stage="exec",
            success=True,
            output="code output here",
            artifacts={"files": ["a.py"]},
            issues=[],
            duration_seconds=5.2,
            agent_used="opencode",
        )
        d = result.to_dict()

        assert d["stage"] == "exec"
        assert d["success"] is True
        assert d["agent_used"] == "opencode"
        assert d["duration_seconds"] == 5.2

    def test_long_output_truncated(self):
        """긴 output은 500자로 잘림"""
        result = StageResult(
            stage="test",
            success=True,
            output="x" * 1000,
        )
        d = result.to_dict()
        assert len(d["output"]) <= 504  # 500 + "..."

    def test_default_values(self):
        """기본값 설정 확인"""
        result = StageResult(stage="plan", success=False, output="err")
        assert result.artifacts == {}
        assert result.issues == []
        assert result.duration_seconds == 0.0
        assert result.agent_used == ""


# ── UnifiedResult 테스트 ──────────────────────────────────────────────


class TestUnifiedResult:
    def test_is_fallback(self):
        result = UnifiedResult(
            success=True,
            output="ok",
            agent_type="claude",
            execution_method="native",
            original_agent="opencode",
            fallback_depth=1,
        )
        assert result.is_fallback() is True

    def test_not_fallback(self):
        result = UnifiedResult(
            success=True,
            output="ok",
            agent_type="claude",
            execution_method="native",
        )
        assert result.is_fallback() is False

    def test_execution_path_fallback(self):
        result = UnifiedResult(
            success=True,
            output="ok",
            agent_type="claude",
            execution_method="native",
            original_agent="opencode",
            fallback_depth=1,
        )
        path = result.get_execution_path()
        assert "opencode" in path
        assert "claude" in path
        assert "폴백" in path

    def test_to_dict_json_serializable(self):
        result = UnifiedResult(
            success=True,
            output="test",
            agent_type="claude",
            execution_method="native",
        )
        d = result.to_dict()
        # JSON 직렬화 가능한지 확인
        json_str = json.dumps(d)
        assert json_str is not None

    def test_to_json(self):
        result = UnifiedResult(
            success=False,
            output="error",
            agent_type="opencode",
            execution_method="cli",
            errors=["timeout"],
        )
        json_str = result.to_json()
        parsed = json.loads(json_str)
        assert parsed["success"] is False
        assert parsed["errors"] == ["timeout"]


# ── Orchestrator 내부 로직 테스트 ─────────────────────────────────────


class TestOrchestratorInternals:
    @pytest.fixture
    def orchestrator(self):
        return StagedDelegateOrchestrator(verbose=False, use_harness=False)

    def test_build_stage_prompt_plan(self, orchestrator):
        prompt = orchestrator._build_stage_prompt("plan", "implement auth", {})
        assert "계획" in prompt or "plan" in prompt.lower()
        assert "implement auth" in prompt

    def test_build_stage_prompt_exec(self, orchestrator):
        prompt = orchestrator._build_stage_prompt("exec", "build API", {"prd": "specs"})
        assert "build API" in prompt
        assert "specs" in prompt

    def test_build_stage_prompt_verify(self, orchestrator):
        prompt = orchestrator._build_stage_prompt("verify", "check code", {"code": "print('hi')"})
        assert "print('hi')" in prompt
        assert "PASS" in prompt or "NEEDS_FIX" in prompt

    def test_build_stage_prompt_unknown_stage(self, orchestrator):
        """알 수 없는 stage도 크래시 없이 기본 프롬프트 반환"""
        prompt = orchestrator._build_stage_prompt("unknown_stage", "task", {})
        assert "task" in prompt

    def test_split_task_single(self, orchestrator):
        """count=1이면 분할 안 함"""
        tasks = orchestrator._split_task("build app", 1)
        assert len(tasks) == 1
        assert tasks[0] == "build app"

    def test_split_task_multiple(self, orchestrator):
        """여러 파트로 분할"""
        tasks = orchestrator._split_task("build app", 3)
        assert len(tasks) == 3
        for i, t in enumerate(tasks):
            assert f"파트 {i+1}/3" in t

    def test_estimate_completion_perfect(self, orchestrator):
        """이슈 없는 성공은 1.0"""
        result = StageResult(stage="verify", success=True, output="ok", issues=[])
        assert orchestrator._estimate_completion(result) == 1.0

    def test_estimate_completion_with_issues(self, orchestrator):
        """이슈 수에 따라 완성도 감소"""
        result = StageResult(stage="verify", success=False, output="", issues=["a", "b", "c"])
        completion = orchestrator._estimate_completion(result)
        assert 0.0 <= completion < 1.0

    def test_estimate_completion_many_issues_floored(self, orchestrator):
        """이슈가 많아도 0 미만으로 안 감"""
        result = StageResult(stage="verify", success=False, output="", issues=["x"] * 50)
        completion = orchestrator._estimate_completion(result)
        assert completion >= 0.0

    def test_format_final_result_success(self, orchestrator):
        """성공 결과 포맷팅"""
        result = orchestrator._format_final_result(
            success=True,
            accumulated={"plan_result": StageResult(stage="plan", success=True, output="planned")},
            final_code="print('done')",
        )
        assert result["success"] is True
        assert result["final_code"] == "print('done')"
        assert result["mode"] == "team"

    def test_format_final_result_failure(self, orchestrator):
        """실패 결과 포맷팅"""
        result = orchestrator._format_final_result(
            success=False,
            failed_stage="exec",
            error="timeout",
        )
        assert result["success"] is False
        assert result["failed_stage"] == "exec"
        assert result["error"] == "timeout"

    def test_format_autopilot_result(self, orchestrator):
        """autopilot 결과 포맷팅"""
        result = orchestrator._format_autopilot_result(
            success=True,
            mode="ralph",
            history=[{"iteration": 0, "completion": 0.95}],
            final_code="code",
        )
        assert result["mode"] == "ralph"
        assert result["iterations"] == 1

    def test_build_stage_context(self, orchestrator):
        """스테이지 컨텍스트 구축"""
        accumulated = {
            "plan_result": StageResult(stage="plan", success=True, output="my plan"),
        }
        ctx = orchestrator._build_stage_context("exec", accumulated, {"extra": "info"})
        assert ctx["extra"] == "info"
        assert ctx["plan"] == "my plan"


# ── _call_api_agent 미구현 검증 ───────────────────────────────────────


class TestApiAgent:
    def test_api_agent_returns_failure(self):
        """_call_api_agent가 미구현 상태에서 명시적 실패 반환"""
        orch = StagedDelegateOrchestrator(verbose=False, use_harness=False)
        result = orch._call_api_agent("google/gemini-2.5-pro", "test", {})
        assert result["success"] is False
        assert "not yet implemented" in result["issues"][0]


# ── staged_delegate entry point 테스트 ────────────────────────────────


class TestStagedDelegateEntryPoint:
    def test_unknown_mode_returns_error(self):
        """알 수 없는 모드는 에러 JSON 반환"""
        result = staged_delegate(goal="test", mode="nonexistent_mode")
        parsed = json.loads(result)
        assert "error" in parsed
        assert "nonexistent_mode" in parsed["error"]

    def test_team_mode_with_mock(self):
        """team 모드가 올바른 핸들러를 호출하는지 확인"""
        with mock.patch.object(StagedDelegateOrchestrator, 'run_team', return_value={"success": True, "mode": "team"}) as mock_run:
            result = staged_delegate(goal="test task", mode="team")
            mock_run.assert_called_once()
            parsed = json.loads(result)
            assert parsed["success"] is True

    def test_ccg_mode_with_mock(self):
        """ccg 모드가 올바른 핸들러를 호출하는지 확인"""
        with mock.patch.object(StagedDelegateOrchestrator, 'run_ccg', return_value={"success": True, "mode": "ccg"}) as mock_run:
            result = staged_delegate(goal="test", mode="ccg")
            mock_run.assert_called_once()

    def test_autopilot_mode_with_mock(self):
        """autopilot 모드 라우팅"""
        with mock.patch.object(StagedDelegateOrchestrator, 'run_autopilot', return_value={"success": True, "mode": "autopilot"}) as mock_run:
            result = staged_delegate(goal="test", mode="autopilot")
            mock_run.assert_called_once()

    def test_ralph_mode_routes_to_ralph_loop(self):
        """ralph 모드가 _run_ralph_loop을 호출하는지 확인"""
        with mock.patch.object(
            StagedDelegateOrchestrator, '_run_ralph_loop',
            return_value={"success": True, "mode": "ralph", "iterations": 3}
        ) as mock_ralph:
            result = staged_delegate(goal="test ralph", mode="ralph", total_minutes=15)
            mock_ralph.assert_called_once()
            # total_minutes가 전달되었는지 확인
            call_kwargs = mock_ralph.call_args
            assert call_kwargs[1].get("total_minutes") == 15 or call_kwargs[0][1] == 15

    def test_ralph_default_total_minutes(self):
        """ralph 모드 기본 시간은 30분"""
        with mock.patch.object(
            StagedDelegateOrchestrator, '_run_ralph_loop',
            return_value={"success": True, "mode": "ralph", "iterations": 5}
        ) as mock_ralph:
            staged_delegate(goal="test", mode="ralph")  # total_minutes 미지정
            call_kwargs = mock_ralph.call_args
            assert call_kwargs[1].get("total_minutes") == 30 or call_kwargs[0][1] == 30

    def test_autopilot_does_not_route_to_ralph(self):
        """autopilot 모드는 _run_ralph_loop을 호출하지 않음"""
        with mock.patch.object(
            StagedDelegateOrchestrator, '_run_ralph_loop',
        ) as mock_ralph:
            with mock.patch.object(
                StagedDelegateOrchestrator, '_run_stage',
                return_value=StageResult(stage="plan", success=True, output="ok", issues=[])
            ):
                staged_delegate(goal="test", mode="autopilot", max_iterations=1)
                mock_ralph.assert_not_called()

    def test_exception_returns_error_json(self):
        """내부 예외 발생 시에도 JSON 반환"""
        with mock.patch.object(StagedDelegateOrchestrator, 'run_team', side_effect=RuntimeError("boom")):
            result = staged_delegate(goal="test", mode="team")
            parsed = json.loads(result)
            assert "error" in parsed
            assert "boom" in parsed["error"]


# ── Ralph Loop 내부 로직 테스트 ───────────────────────────────────────


class TestRalphLoop:
    """_run_ralph_loop() 메서드의 시간 기반 로직 검증"""

    @pytest.fixture
    def orchestrator(self):
        return StagedDelegateOrchestrator(verbose=False, use_harness=False)

    def test_ralph_loop_returns_required_fields(self, orchestrator):
        """결과에 필수 필드가 존재"""
        with mock.patch.object(orchestrator, '_run_stage') as mock_stage:
            mock_stage.return_value = StageResult(
                stage="plan", success=True, output="ok", issues=[]
            )
            # 0.01분 = 0.6초 루프
            result = orchestrator._run_ralph_loop("test task", total_minutes=0.01)

        assert "success" in result
        assert "mode" in result
        assert result["mode"] == "ralph"
        assert "iterations" in result
        assert "elapsed_minutes" in result
        assert "time_accurate" in result
        assert "history" in result

    def test_ralph_loop_calls_plan_once(self, orchestrator):
        """Plan stage는 정확히 1회 호출"""
        call_count = {"plan": 0, "exec": 0, "verify": 0}

        def track_stage(stage, *args, **kwargs):
            call_count[stage] = call_count.get(stage, 0) + 1
            return StageResult(stage=stage, success=True, output="ok", issues=[])

        with mock.patch.object(orchestrator, '_run_stage', side_effect=track_stage):
            orchestrator._run_ralph_loop("test task", total_minutes=0.01)

        assert call_count["plan"] == 1

    def test_ralph_loop_runs_exec_verify_cycle(self, orchestrator):
        """Exec과 Verify가 최소 1회씩 호출"""
        call_count = {"plan": 0, "exec": 0, "verify": 0}

        def track_stage(stage, *args, **kwargs):
            call_count[stage] = call_count.get(stage, 0) + 1
            return StageResult(stage=stage, success=True, output="ok", issues=[])

        with mock.patch.object(orchestrator, '_run_stage', side_effect=track_stage):
            orchestrator._run_ralph_loop("test task", total_minutes=0.02)

        assert call_count["exec"] >= 1
        assert call_count["verify"] >= 1

    def test_ralph_loop_calls_fix_on_issues(self, orchestrator):
        """Verify에서 이슈 발견 시 Fix stage 호출"""
        call_log = []

        def track_stage(stage, *args, **kwargs):
            call_log.append(stage)
            if stage == "verify":
                return StageResult(
                    stage="verify", success=False, output="issues",
                    issues=["bug1", "bug2"]
                )
            return StageResult(stage=stage, success=True, output="ok", issues=[])

        with mock.patch.object(orchestrator, '_run_stage', side_effect=track_stage):
            result = orchestrator._run_ralph_loop("test task", total_minutes=0.01)

        assert "fix" in call_log

    def test_ralph_loop_context_has_plan(self, orchestrator):
        """Plan 결과가 context에 저장되어 후속 stage에 전달"""
        contexts_received = []

        def track_stage(stage, task, context, agent):
            contexts_received.append((stage, dict(context) if context else {}))
            return StageResult(stage=stage, success=True, output="plan output", issues=[])

        with mock.patch.object(orchestrator, '_run_stage', side_effect=track_stage):
            orchestrator._run_ralph_loop("test task", total_minutes=0.01)

        # exec stage의 context에 plan이 포함되어야 함
        exec_contexts = [c for s, c in contexts_received if s == "exec"]
        if exec_contexts:
            assert "plan" in exec_contexts[0]

    def test_ralph_loop_total_minutes_zero(self, orchestrator):
        """total_minutes=0이면 루프 즉시 종료"""
        with mock.patch.object(orchestrator, '_run_stage') as mock_stage:
            mock_stage.return_value = StageResult(
                stage="plan", success=True, output="ok", issues=[]
            )
            result = orchestrator._run_ralph_loop("test", total_minutes=0)

        # Plan은 루프 전에 실행되므로 1회, 루프 iteration은 0
        assert result["iterations"] == 0
        assert result["success"] is True

    def test_ralph_history_tracks_phases(self, orchestrator):
        """history에 phase_reached 정보 기록"""
        with mock.patch.object(orchestrator, '_run_stage') as mock_stage:
            mock_stage.return_value = StageResult(
                stage="exec", success=True, output="ok", issues=[]
            )
            result = orchestrator._run_ralph_loop("test", total_minutes=0.01)

        for entry in result["history"]:
            assert "iteration" in entry
            assert "phase_reached" in entry or "completion" in entry


# ── SKILL.md 통합 검증 ───────────────────────────────────────────────


class TestSkillIntegration:
    """ralf-loop SKILL.md와 코드 구현의 일관성 검증"""

    def test_ralf_loop_skill_exists(self):
        """통합된 ralf-loop SKILL.md가 존재"""
        skill_path = Path.home() / ".hermes/skills/software-development/ralf-loop/SKILL.md"
        if skill_path.exists():
            content = skill_path.read_text(encoding="utf-8")
            assert "staged_delegate" in content, "SKILL.md에 staged_delegate 호출 지침 없음"
            assert "ralph" in content, "SKILL.md에 ralph 모드 언급 없음"

    def test_enforcer_timer_removed(self):
        """중복 스킬(enforcer, timer)이 제거됨"""
        enforcer = Path.home() / ".hermes/skills/software-development/ralf-loop-enforcer"
        timer = Path.home() / ".hermes/skills/software-development/ralf-loop-timer"
        assert not enforcer.exists(), "ralf-loop-enforcer 아직 존재"
        assert not timer.exists(), "ralf-loop-timer 아직 존재"

    def test_delegate_tool_no_ralfloop_class(self):
        """delegate_tool.py에서 RalfLoop 데드코드 제거 확인"""
        import importlib
        import tools.delegate_tool as dt
        importlib.reload(dt)
        assert not hasattr(dt, 'RalfLoop'), "RalfLoop 클래스가 아직 존재"
        assert not hasattr(dt, 'run_ralf_loop'), "run_ralf_loop 함수가 아직 존재"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])

