#!/usr/bin/env python3
"""
Staged Delegate Tool -- OMC-style Multi-Agent Orchestration

OMC(oh-my-claudecode) 패턴을 Hermes에 통합:
- Team Mode: plan → prd → exec → verify → fix (loop)
- CCG Mode: Tri-model synthesis (Claude + Codex + Gemini)
- Autopilot/Ralph: End-to-end autonomous with verify/fix loop
- Ultrawork: Maximum parallelism

OpenCode CLI 포함 통합 에이전트 지원:
- claude (hermes native)
- codex (CLI)
- opencode (CLI)
- gemini (API)
"""

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Callable

from tools.delegate_tool import delegate_task as _delegate_task
from tools.harness_hooks import HarnessHookManager, get_project_harness_config

logger = logging.getLogger(__name__)


# 지원하는 에이전트 타입 및 CLI 매핑
AGENT_TYPES = {
    "claude": {"type": "hermes", "model": "anthropic/claude-sonnet-4"},
    "codex": {"type": "cli", "cli": "codex", "model": "openai/codex"},
    "opencode": {"type": "cli", "cli": "opencode", "model": "zai/glm-5.1", "provider": "zai"},
    "cline": {"type": "cli", "cli": "cline", "model": "fireworks-ai/accounts/fireworks/routers/kimi-k2p5-turbo", "provider": "fireworks"},
    "gemini": {"type": "api", "model": "google/gemini-2.5-pro"},
}

# 자동 폴백 체인 (실제 설치된 CLI/API만 포함)
# 현재 연결 상태:
# - opencode: ✅ Z.AI GLM 5.1 (설치됨)
# - cline: ✅ Fireworks Kimi K2.5 (설치됨)  
# - claude: ✅ Hermes Native (연결됨)
# - codex: ❌ 미설치 (추가 시 FALLBACK_CHAIN에 포함 가능)
# - gemini: ❌ API Key 없음 (추가 시 FALLBACK_CHAIN에 포함 가능)
FALLBACK_CHAIN = {
    "opencode": "cline",   # Z.AI 실패 시 Fireworks로
    "cline": "claude",     # Fireworks 실패 시 Claude로
    # "codex": "opencode",  # TODO: Codex CLI 설치 후 활성화
    # "gemini": "claude",   # TODO: Gemini API Key 설정 후 활성화
}

# 폴백 설정
MAX_FALLBACK_DEPTH = 3      # 최대 폴백 깊이 (opencode→cline→claude)
FALLBACK_INITIAL_DELAY = 2  # 첫 폴백 대기 (초)
FALLBACK_MAX_DELAY = 30     # 최대 대기 (초)

# Claude 최종 실패 시 재시도 설정
CLAUDE_MAX_RETRIES = 2      # Native 클라우드 재시도 횟수
CLAUDE_RETRY_DELAY = 3      # 재시도 간 대기 (초)

# Stage별 기본 에이전트 매핑
DEFAULT_STAGE_AGENTS = {
    "plan": "claude",
    "prd": "claude",
    "exec": "opencode",  # OpenCode as default for execution
    "verify": "claude",
    "fix": "opencode",   # OpenCode as default for fixes
    "synthesize": "claude",
}


@dataclass
class StageResult:
    """Stage 실행 결과"""
    stage: str
    success: bool
    output: str
    artifacts: Dict[str, Any] = field(default_factory=dict)
    issues: List[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    agent_used: str = ""

    def to_dict(self) -> Dict:
        return {
            "stage": self.stage,
            "success": self.success,
            "output": self.output[:500] + "..." if len(self.output) > 500 else self.output,
            "artifacts": self.artifacts,
            "issues": self.issues,
            "duration_seconds": self.duration_seconds,
            "agent_used": self.agent_used,
        }


@dataclass
class UnifiedResult:
    """
    통합 실행 결과 객체
    - Native, CLI, Fallback 모든 결과를 통일된 형식으로 반환
    """
    success: bool
    output: str
    agent_type: str                    # 실제 실행된 에이전트
    execution_method: str            # "native", "cli", "api"
    
    # 메타데이터
    duration_seconds: float = 0.0
    timestamp: str = field(default_factory=lambda: __import__('datetime').datetime.now().isoformat())
    
    # CLI 특화 필드
    cli_name: Optional[str] = None     # "opencode", "cline", "codex"
    returncode: Optional[int] = None   # CLI 종료 코드
    
    # Fallback 특화 필드
    original_agent: Optional[str] = None    # 처음 요청한 에이전트
    fallback_agent: Optional[str] = None      # 실제 실행된 폴백 에이전트
    fallback_reason: Optional[str] = None     # 폴백 원인
    fallback_depth: int = 0                 # 폴백 단계 (0 = 폴백 없음)
    fallback_delay: float = 0.0               # 폴백 대기 시간
    
    # 추가 정보
    artifacts: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    
    def is_fallback(self) -> bool:
        """폴백이 발생했는지 확인"""
        return self.fallback_depth > 0 or self.original_agent is not None
    
    def get_execution_path(self) -> str:
        """실행 경로 문자열 반환"""
        if self.is_fallback():
            return f"{self.original_agent} → {self.agent_type} (폴백)"
        return self.agent_type
    
    def to_dict(self) -> Dict[str, Any]:
        """딕셔너리 변환"""
        return {
            "success": self.success,
            "output": self.output[:1000] + "..." if len(self.output) > 1000 else self.output,
            "agent_type": self.agent_type,
            "execution_method": self.execution_method,
            "duration_seconds": self.duration_seconds,
            "timestamp": self.timestamp,
            "cli_name": self.cli_name,
            "returncode": self.returncode,
            "original_agent": self.original_agent,
            "fallback_agent": self.fallback_agent,
            "fallback_reason": self.fallback_reason,
            "fallback_depth": self.fallback_depth,
            "fallback_delay": self.fallback_delay,
            "artifacts": self.artifacts,
            "errors": self.errors,
            "warnings": self.warnings,
        }
    
    def to_json(self) -> str:
        """JSON 문자열 변환"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


class StagedDelegateOrchestrator:
    """
    OMC-style staged pipeline orchestrator
    """

    def __init__(self, parent_agent=None, verbose: bool = True, use_harness: bool = True):
        self.parent_agent = parent_agent
        self.verbose = verbose
        self.use_harness = use_harness
        self.harness = HarnessHookManager() if use_harness else None
        self.results: List[StageResult] = []

    def log(self, message: str, level: str = "INFO"):
        """로깅 with 레벨"""
        if not self.verbose:
            return
        
        import datetime
        timestamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        prefix = f"[{timestamp}] [StagedDelegate:{level}]"
        print(f"{prefix} {message}", flush=True)
    
    def log_fallback(self, from_agent: str, to_agent: str, reason: str, delay: float):
        """폴백 발생 시 상세 로깅"""
        self.log(f"🔄 FALLBACK: {from_agent} → {to_agent}", "WARN")
        self.log(f"   Reason: {reason}", "WARN")
        self.log(f"   Delay: {delay:.1f}s", "WARN")
        
    def log_retry(self, agent: str, attempt: int, max_attempts: int):
        """재시도 로깅"""
        self.log(f"🔁 RETRY: {agent} ({attempt}/{max_attempts})", "WARN")

    def run_team(
        self,
        task: str,
        stages: List[str] = None,
        stage_agents: Dict[str, str] = None,
        max_iterations: int = 5,
        context: Dict = None,
    ) -> Dict[str, Any]:
        """
        Team Mode - Staged Pipeline

        Args:
            task: 작업 설명
            stages: 실행할 스테이지 목록 (기본: plan, prd, exec, verify)
            stage_agents: 스테이지별 에이전트 설정 {"plan": "claude", "exec": "opencode"}
            max_iterations: verify-fix 최대 반복
            context: 추가 컨텍스트

        Returns:
            실행 결과
        """
        stages = stages or ["plan", "prd", "exec", "verify"]
        stage_agents = stage_agents or {}
        context = context or {}

        self.log(f"🚀 TEAM MODE: {task[:60]}...")
        self.log(f"   Stages: {stages}")
        self.log(f"   Max iterations: {max_iterations}")

        accumulated_results = {}
        current_code = ""

        # Stage 1-3: plan → prd → exec
        for stage in stages:
            if stage in ["verify", "fix"]:
                continue  # verify-fix는 별도 루프에서 처리

            agent_type = stage_agents.get(stage, DEFAULT_STAGE_AGENTS.get(stage, "claude"))
            stage_context = self._build_stage_context(stage, accumulated_results, context)

            result = self._run_stage(stage, task, stage_context, agent_type)
            accumulated_results[f"{stage}_result"] = result
            self.results.append(result)

            if not result.success:
                return self._format_final_result(
                    success=False, failed_stage=stage, accumulated=accumulated_results
                )

            if stage == "exec":
                current_code = result.output

        # Stage 4-5: verify → fix loop
        for iteration in range(max_iterations):
            self.log(f"\n📋 Verify-Fix Loop (iteration {iteration + 1}/{max_iterations})")

            # Verify
            verify_agent = stage_agents.get("verify", DEFAULT_STAGE_AGENTS["verify"])
            verify_context = {
                **context,
                "code": current_code,
                "artifacts": accumulated_results,
                "iteration": iteration,
            }
            verify_result = self._run_stage("verify", task, verify_context, verify_agent)
            accumulated_results["verify_result"] = verify_result
            self.results.append(verify_result)

            if verify_result.success and not verify_result.issues:
                self.log("✅ 검증 통과!")
                return self._format_final_result(
                    success=True,
                    accumulated=accumulated_results,
                    final_code=current_code,
                )

            # Fix needed
            self.log(f"🔧 {len(verify_result.issues)}개 이슈 수정 필요")
            fix_agent = stage_agents.get("fix", DEFAULT_STAGE_AGENTS["fix"])
            fix_context = {
                **context,
                "code": current_code,
                "issues": verify_result.issues,
                "verify_feedback": verify_result.output,
                "iteration": iteration,
            }
            fix_result = self._run_stage("fix", task, fix_context, fix_agent)
            accumulated_results["fix_result"] = fix_result
            self.results.append(fix_result)

            if not fix_result.success:
                self.log("⚠️ Fix 실패")
                break

            current_code = fix_result.output

        # Max iterations reached
        self.log(f"⚠️ 최대 반복 횟수({max_iterations}) 도달")
        return self._format_final_result(
            success=False,
            accumulated=accumulated_results,
            final_code=current_code,
            error="Max iterations reached",
        )

    def run_ccg(
        self,
        task: str,
        models: List[str] = None,
        synthesis_model: str = "claude",
        context: Dict = None,
    ) -> Dict[str, Any]:
        """
        CCG Mode - Tri-Model Synthesis (Claude + Codex + OpenCode/Gemini)

        Args:
            task: 작업 설명
            models: 사용할 모델 목록 (기본: ["claude", "codex", "opencode"])
            synthesis_model: 결과 종합 모델
            context: 추가 컨텍스트

        Returns:
            개별 결과 + 종합 결과
        """
        models = models or ["claude", "codex", "opencode"]
        context = context or {}

        self.log(f"🌐 CCG MODE: {task[:60]}...")
        self.log(f"   Models: {models}")

        # 병렬 호출
        individual_results = {}
        with ThreadPoolExecutor(max_workers=len(models)) as executor:
            futures = {
                executor.submit(self._run_single_model, model, task, context): model
                for model in models
            }

            for future in as_completed(futures):
                model = futures[future]
                try:
                    result = future.result()
                    individual_results[model] = result
                    status = "✅" if result["success"] else "❌"
                    self.log(f"   {status} {model}: {result['output'][:80]}...")
                except Exception as e:
                    individual_results[model] = {"success": False, "error": str(e), "output": ""}
                    self.log(f"   ❌ {model}: {e}")

        # 종합
        self.log(f"\n🔄 결과 종합 중 (using {synthesis_model})...")
        synthesis = self._synthesize_results(task, individual_results, synthesis_model, context)

        return {
            "success": synthesis.get("success", True),
            "mode": "ccg",
            "task": task,
            "models_used": models,
            "individual_results": individual_results,
            "synthesis": synthesis,
        }

    def run_autopilot(
        self,
        task: str,
        mode: str = "autopilot",  # "autopilot", "ralph", "ultrawork"
        max_iterations: int = None,
        parallel: int = 3,
        context: Dict = None,
        total_minutes: int = None,
    ) -> Dict[str, Any]:
        """
        Autopilot/Ralph/Ultrawork Mode

        Args:
            task: 작업 설명
            mode: 실행 모드
            max_iterations: 최대 반복 (mode별 기본값 사용)
            parallel: Ultrawork 병렬 에이전트 수
            context: 추가 컨텍스트
            total_minutes: Ralph 모드 시간 제한 (분). 기본 30분.
        """
        context = context or {}

        if mode == "ultrawork":
            return self._run_ultrawork(task, parallel, context)

        if mode == "ralph":
            return self._run_ralph_loop(
                task,
                total_minutes=total_minutes or 30,
                context=context,
            )

        # autopilot (iteration 기반)
        max_iterations = max_iterations or 5
        self.log(f"🤖 AUTOPILOT MODE: {task[:60]}...")
        self.log(f"   Max iterations: {max_iterations}")

        current_code = ""
        history = []

        for iteration in range(max_iterations):
            self.log(f"\n🔄 Iteration {iteration + 1}/{max_iterations}")

            # Plan (iteration 0 only)
            if iteration == 0:
                plan_result = self._run_stage("plan", task, context, "claude")
                if not plan_result.success:
                    return self._format_autopilot_result(
                        success=False, mode=mode, history=history, error="Planning failed"
                    )
                context["plan"] = plan_result.output

            # Exec
            exec_result = self._run_stage("exec", task, context, "opencode")
            if exec_result.success:
                current_code += "\n" + exec_result.output

            # Verify
            verify_context = {**context, "code": current_code, "iteration": iteration}
            verify_result = self._run_stage("verify", task, verify_context, "claude")

            completion = self._estimate_completion(verify_result)
            history.append({
                "iteration": iteration,
                "completion": completion,
                "issues": len(verify_result.issues),
            })

            # 완료 체크
            if completion >= 0.8 and not verify_result.issues:
                self.log(f"✅ 완료! (완성도: {completion:.0%})")
                return self._format_autopilot_result(
                    success=True, mode=mode, history=history, final_code=current_code
                )

            # Fix
            if verify_result.issues:
                self.log(f"🔧 {len(verify_result.issues)}개 이슈 수정")
                fix_context = {
                    **context,
                    "code": current_code,
                    "issues": verify_result.issues,
                }
                fix_result = self._run_stage("fix", task, fix_context, "opencode")
                if fix_result.success:
                    current_code = fix_result.output

        self.log(f"⚠️ 최대 반복 횟수 도달")
        return self._format_autopilot_result(
            success=False,
            mode=mode,
            history=history,
            final_code=current_code,
            error="Max iterations reached",
        )

    def _run_ralph_loop(
        self,
        task: str,
        total_minutes: int = 30,
        context: Dict = None,
    ) -> Dict[str, Any]:
        """
        Ralph 모드: 시간 기반 강제 반복 루프

        time.monotonic()으로 실제 경과 시간을 측정하여 설정 시간까지
        Exec→Verify→Fix 사이클을 강제로 반복합니다.

        핵심 원칙:
        - 설정 시간(±10초)을 정확히 채울 것
        - 1초라도 남으면 계속
        - 완료 체크 없음 (시간이 남으면 무조건 다음 iteration)

        Args:
            task: 작업 설명
            total_minutes: 총 실행 시간 (분). 기본 30분.
            context: 추가 컨텍스트

        Returns:
            결과 딕셔너리 (iterations, elapsed_minutes, history 등)
        """
        import time as _time

        context = context or {}
        total_seconds = total_minutes * 60
        start_time = _time.monotonic()
        end_time = start_time + total_seconds

        self.log(f"🔄 RALPH MODE: {task[:60]}...")
        self.log(f"   강제 반복 시간: {total_minutes}분")
        self.log(f"   종료 예정: {_time.strftime('%H:%M:%S', _time.localtime(_time.time() + total_seconds))}")

        current_code = ""
        history = []
        iteration = 0

        # ── Plan (1회만) ──────────────────────────────────────────────
        plan_result = self._run_stage("plan", task, context, "claude")
        if plan_result.success:
            context["plan"] = plan_result.output
            self.log(f"   📋 Plan 완료")
        else:
            self.log(f"   ⚠️ Plan 실패, Exec로 직접 진행")

        # ── 시간 기반 메인 루프 ──────────────────────────────────────
        while _time.monotonic() < end_time:
            iteration += 1
            elapsed = _time.monotonic() - start_time
            remaining = end_time - _time.monotonic()

            self.log(f"\n🔄 [Iteration {iteration}] {remaining/60:.1f}분 남음 / {elapsed/60:.1f}분 경과")

            # Exec
            exec_result = self._run_stage("exec", task, context, "opencode")
            if exec_result.success:
                current_code += "\n" + exec_result.output

            # 시간 체크: 남은 시간이 없으면 여기서 종료
            if _time.monotonic() >= end_time:
                history.append({
                    "iteration": iteration,
                    "completion": 1.0,
                    "issues": 0,
                    "phase_reached": "exec",
                })
                break

            # Verify
            verify_context = {**context, "code": current_code, "iteration": iteration}
            verify_result = self._run_stage("verify", task, verify_context, "claude")
            completion = self._estimate_completion(verify_result)

            history.append({
                "iteration": iteration,
                "completion": completion,
                "issues": len(verify_result.issues),
                "phase_reached": "verify",
            })

            self.log(f"   완성도: {completion:.0%}, 이슈: {len(verify_result.issues)}개")

            # 시간 체크
            if _time.monotonic() >= end_time:
                break

            # Fix (이슈가 있을 때만)
            if verify_result.issues:
                self.log(f"   🔧 {len(verify_result.issues)}개 이슈 수정")
                fix_context = {
                    **context,
                    "code": current_code,
                    "issues": verify_result.issues,
                }
                fix_result = self._run_stage("fix", task, fix_context, "opencode")
                if fix_result.success:
                    current_code = fix_result.output
                    history[-1]["phase_reached"] = "fix"

            # 시간 체크: 남은 시간이 너무 적으면 종료
            remaining = end_time - _time.monotonic()
            if remaining <= 0.5:
                break

        # ── 종료 처리 ────────────────────────────────────────────────
        final_elapsed = _time.monotonic() - start_time
        time_diff = abs(final_elapsed - total_seconds)

        self.log(f"\n🏁 랄프루프 종료")
        self.log(f"   총 소요: {final_elapsed/60:.1f}분 / 목표: {total_minutes}분")
        self.log(f"   총 반복: {iteration}회")
        if time_diff <= 10:
            self.log(f"   ✅ 시간 정확 충족 (오차: {time_diff:.1f}초)")
        else:
            self.log(f"   ⚠️ 시간 오차: {time_diff:.1f}초")

        return {
            "success": True,
            "mode": "ralph",
            "task": task,
            "total_minutes": total_minutes,
            "elapsed_minutes": round(final_elapsed / 60, 1),
            "iterations": iteration,
            "time_accurate": time_diff <= 10,
            "history": history,
            "final_code": current_code[:500] if current_code else "",
        }

    def _run_ultrawork(self, task: str, parallel: int, context: Dict) -> Dict[str, Any]:
        """Ultrawork - maximum parallelism"""
        self.log(f"⚡ ULTRAWORK MODE: {task[:60]}...")
        self.log(f"   Parallel agents: {parallel}")

        # 작업 분할
        subtasks = self._split_task(task, parallel)

        # 병렬 실행
        results = []
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = [
                executor.submit(self._run_single_model, "opencode", subtask, context)
                for subtask in subtasks
            ]

            for i, future in enumerate(as_completed(futures)):
                try:
                    result = future.result()
                    results.append(result)
                    self.log(f"   ✅ Agent {i+1}/{parallel} 완료")
                except Exception as e:
                    results.append({"success": False, "error": str(e)})
                    self.log(f"   ❌ Agent {i+1}/{parallel} 실패: {e}")

        merged_code = "\n\n".join([r.get("output", "") for r in results])
        success = all(r.get("success", False) for r in results)

        return {
            "success": success,
            "mode": "ultrawork",
            "parallel_agents": parallel,
            "results": results,
            "final_code": merged_code[:1000] + "..." if len(merged_code) > 1000 else merged_code,
        }

    def _run_stage(
        self, stage: str, task: str, context: Dict, agent_type: str
    ) -> StageResult:
        """개별 스테이지 실행"""
        self.log(f"\n▶️ Stage: {stage.upper()} (agent: {agent_type})")

        prompt = self._build_stage_prompt(stage, task, context)
        start_time = time.time()

        try:
            result = self._execute_with_agent(agent_type, prompt, context)
            duration = time.time() - start_time

            success = result.get("success", True)
            if stage == "verify":
                success = "PASS" in result.get("output", "").upper() or not result.get("issues")

            stage_result = StageResult(
                stage=stage,
                success=success,
                output=result.get("output", ""),
                artifacts=result.get("artifacts", {}),
                issues=result.get("issues", []),
                duration_seconds=round(duration, 2),
                agent_used=agent_type,
            )

            status = "✅" if stage_result.success else "❌"
            self.log(f"   {status} {stage} 완료 ({duration:.1f}s)")

            return stage_result

        except Exception as e:
            duration = time.time() - start_time
            self.log(f"   ❌ {stage} 실패: {e}")
            return StageResult(
                stage=stage,
                success=False,
                output=str(e),
                issues=[str(e)],
                duration_seconds=round(duration, 2),
                agent_used=agent_type,
            )

    def _execute_with_agent(
        self, agent_type: str, prompt: str, context: Dict, attempt_fallback: bool = True
    ) -> Dict[str, Any]:
        """
        에이전트 타입에 따라 적절한 실행 방식 선택

        - hermes: delegate_task 사용
        - cli: CLI subprocess 사용 (opencode, codex, cline)
        - api: API 호출 (gemini)

        Args:
            agent_type: 사용할 에이전트 타입
            prompt: 작업 프롬프트
            context: 추가 컨텍스트
            attempt_fallback: 실패 시 자동 폴백 시도 여부
        """
        agent_config = AGENT_TYPES.get(agent_type, AGENT_TYPES["claude"])
        result = None

        if agent_config["type"] == "hermes":
            # Hermes delegate_task 사용
            result = self._call_delegate_task(prompt, agent_config["model"], context)

        elif agent_config["type"] == "cli":
            # CLI 에이전트 사용 (opencode, codex, cline)
            result = self._call_cli_agent(agent_config["cli"], prompt, context)

        elif agent_config["type"] == "api":
            # API 직접 호출 (gemini)
            result = self._call_api_agent(agent_config["model"], prompt, context)

        else:
            return {"success": False, "error": f"Unknown agent type: {agent_type}"}

        # 자동 폴백: 실패 시 FALLBACK_CHAIN의 다음 에이전트로 재시도
        if not result.get("success", False) and attempt_fallback and agent_type in FALLBACK_CHAIN:
            fallback_agent = FALLBACK_CHAIN[agent_type]
            
            # 지수 백오프로 대기 시간 계산 (fallback_depth 기반)
            fallback_depth = context.get("_fallback_depth", 0)
            delay = min(FALLBACK_INITIAL_DELAY * (2 ** fallback_depth), FALLBACK_MAX_DELAY)
            
            self.log(f"⚠️ {agent_type} 실패 → 폴백: {fallback_agent} ({delay}초 후 재시도)...")
            import time
            time.sleep(delay)
            
            # 폴백 깊이 증가시켜 전달
            new_context = context.copy()
            new_context["_fallback_depth"] = fallback_depth + 1
            
            # 최대 폴백 깊이 체크
            if fallback_depth >= MAX_FALLBACK_DEPTH - 1:
                self.log(f"❌ 최대 폴백 깊이 도달 ({MAX_FALLBACK_DEPTH}) - 마지막 시도")
            
            fallback_result = self._execute_with_agent(
                fallback_agent, prompt, new_context, 
                attempt_fallback=(fallback_depth < MAX_FALLBACK_DEPTH - 1)
            )
            
            # 원래 에이전트 정보 보존
            fallback_result["original_agent"] = agent_type
            fallback_result["fallback_agent"] = fallback_agent
            fallback_result["fallback_reason"] = result.get("error", "Unknown error")
            fallback_result["fallback_depth"] = fallback_depth + 1
            fallback_result["fallback_delay"] = delay
            return fallback_result
        
        # Claude(Native) 최종 실패 시 재시도
        if (not result.get("success", False) and 
            agent_type == "claude" and 
            context.get("_claude_retries", 0) < CLAUDE_MAX_RETRIES):
            
            retry_count = context.get("_claude_retries", 0) + 1
            self.log(f"🔄 Claude 재시도 {retry_count}/{CLAUDE_MAX_RETRIES} ({CLAUDE_RETRY_DELAY}초 후)...")
            
            import time
            time.sleep(CLAUDE_RETRY_DELAY)
            
            new_context = context.copy()
            new_context["_claude_retries"] = retry_count
            
            return self._execute_with_agent(
                "claude", prompt, new_context, attempt_fallback=False
            )

        return result

    def _call_delegate_task(self, goal: str, model: str, context: Dict) -> Dict:
        """Hermes delegate_task 호출"""
        try:
            result_json = _delegate_task(
                goal=goal,
                context=context.get("context_str"),
                max_iterations=context.get("max_iterations", 50),
                parent_agent=self.parent_agent,
            )
            result = json.loads(result_json)
            return {
                "success": True,
                "output": json.dumps(result, ensure_ascii=False),
                "artifacts": result.get("results", [{}])[0].get("artifacts", {}),
            }
        except Exception as e:
            return {"success": False, "output": str(e), "issues": [str(e)]}

    def _call_cli_agent(self, cli_name: str, prompt: str, context: Dict) -> Dict:
        """
        CLI 에이전트 호출 (opencode, codex)

        opencode run 'prompt' 또는 codex 'prompt' 실행
        """
        import subprocess
        import tempfile
        import os

        # 컨텍스트 파일 생성
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(f"Task: {prompt}\n\n")
            f.write(f"Context: {json.dumps(context, ensure_ascii=False, indent=2)}\n")
            context_file = f.name

        try:
            if cli_name == "opencode":
                # OpenCode CLI 호출
                cmd = [
                    "opencode",
                    "run",
                    prompt,
                    "--format", "json",
                    "--file", context_file,
                ]
            elif cli_name == "codex":
                # Codex CLI 호출
                cmd = [
                    "codex",
                    "--approval-mode", "auto-edit",
                    "--message", prompt,
                ]
            else:
                return {"success": False, "error": f"Unknown CLI: {cli_name}"}

            # 실행
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5분 타임아웃
                cwd=context.get("working_dir", os.getcwd()),
            )

            output = result.stdout if result.returncode == 0 else result.stderr

            return {
                "success": result.returncode == 0,
                "output": output,
                "artifacts": {},
                "issues": [] if result.returncode == 0 else [f"CLI exit code: {result.returncode}"],
            }

        except subprocess.TimeoutExpired:
            return {"success": False, "output": "", "issues": ["CLI timeout (5min)"]}
        except Exception as e:
            return {"success": False, "output": str(e), "issues": [str(e)]}
        finally:
            # 임시 파일 삭제
            try:
                os.unlink(context_file)
            except:
                pass

    def _call_api_agent(self, model: str, prompt: str, context: Dict) -> Dict:
        """API 직접 호출 (미구현 - 향후 확장)"""
        # TODO: Gemini API 등 직접 호출 구현
        return {"success": False, "output": "", "issues": ["API agent not yet implemented"]}

    def _run_single_model(self, model: str, task: str, context: Dict) -> Dict[str, Any]:
        """CCG용 단일 모델 실행"""
        result = self._execute_with_agent(model, task, context)
        return {
            "success": result.get("success", False),
            "output": result.get("output", ""),
            "model": model,
        }

    def _synthesize_results(
        self, task: str, individual_results: Dict, synthesis_model: str, context: Dict
    ) -> Dict:
        """Tri-model 결과 종합"""
        # Build individual results text (avoid backslash in f-string)
        results_text = ""
        for m, r in individual_results.items():
            results_text += f"---\n[{m.upper()}]\n{r.get('output', '')[:1000]}\n"

        synthesis_prompt = f"""당신은 다중 전문가 의견을 종합하는 수석 아키텍트입니다.

작업: {task}

다음 전문가들의 분석 결과를 검토하고 최적의 종합 솔루션을 제시하세요.

{results_text}

출력:
1. Executive Summary (3문장)
2. Key Insights from each expert
3. Recommended Approach
4. Implementation Roadmap
5. Risk Mitigation
"""

        result = self._execute_with_agent(synthesis_model, synthesis_prompt, context)
        return {
            "success": result.get("success", True),
            "output": result.get("output", ""),
            "synthesis_model": synthesis_model,
        }

    def _build_stage_prompt(self, stage: str, task: str, context: Dict) -> str:
        """스테이지별 프롬프트 생성"""
        prompts = {
            "plan": f"""당신은 소프트웨어 설계 전문가입니다.

작업: {task}

상세한 계획을 수립해주세요:
1. 목표 및 범위 정의
2. 주요 컴포넌트 식별
3. 작업 분할 (WBS)
4. 의존성 및 순서
5. 완료 기준

컨텍스트: {context}""",

            "prd": f"""당신은 기술 문서 작성 전문가입니다.

작업: {task}

상세한 PRD를 작성하세요:
1. 개요
2. 요구사항 상세
3. 인터페이스 정의
4. 데이터 모델
5. 테스트 계획

계획: {context.get('plan', 'N/A')}""",

            "exec": f"""당신은 시니어 소프트웨어 엔지니어입니다.

작업: {task}

코드를 작성하세요:
- 모든 요구사항 구현
- 테스트 코드 포함
- 에러 처리 완벽히

PRD: {context.get('prd', 'N/A')}""",

            "verify": f"""당신은 코드 리뷰 전문가입니다.

코드:
{context.get('code', '')[:3000]}

검증 항목:
1. 기능적 정확성
2. 코드 품질
3. 테스트 커버리지

출력:
- 통과 여부: PASS / NEEDS_FIX
- 발견된 이슈 목록
- 완성도 (0.0-1.0)""",

            "fix": f"""당신은 버그 수정 전문가입니다.

이슈:
{chr(10).join(f'- {issue}' for issue in context.get('issues', []))}

코드:
{context.get('code', '')[:2000]}

수정된 코드만 출력하세요.""",
        }
        return prompts.get(stage, f"Task: {task}\nContext: {context}")

    def _build_stage_context(self, stage: str, accumulated: Dict, context: Dict) -> Dict:
        """스테이지 컨텍스트 구성"""
        result = {**context}
        for key, value in accumulated.items():
            if isinstance(value, StageResult):
                result[key.replace("_result", "")] = value.output
        return result

    def _split_task(self, task: str, count: int) -> List[str]:
        """작업 분할"""
        if count == 1:
            return [task]
        return [f"{task}\n\n[파트 {i+1}/{count}]" for i in range(count)]

    def _estimate_completion(self, verify_result: StageResult) -> float:
        """완성도 추정"""
        if verify_result.success and not verify_result.issues:
            return 1.0
        # 이슈 수 기반 완성도 감소
        issue_penalty = min(len(verify_result.issues) * 0.1, 0.5)
        return max(0.5 - issue_penalty, 0.0)

    def _format_final_result(
        self, success: bool, accumulated: Dict = None, failed_stage: str = None,
        final_code: str = None, error: str = None
    ) -> Dict[str, Any]:
        """최종 결과 포맷팅"""
        return {
            "success": success,
            "mode": "team",
            "failed_stage": failed_stage,
            "stage_results": [r.to_dict() for r in self.results],
            "accumulated": {k: (v.to_dict() if isinstance(v, StageResult) else v)
                           for k, v in (accumulated or {}).items()},
            "final_code": final_code,
            "error": error,
        }

    def _format_autopilot_result(
        self, success: bool, mode: str, history: List[Dict],
        final_code: str = None, error: str = None
    ) -> Dict[str, Any]:
        """Autopilot 결과 포맷팅"""
        return {
            "success": success,
            "mode": mode,
            "history": history,
            "iterations": len(history),
            "final_code": final_code,
            "error": error,
        }


def staged_delegate(
    goal: str,
    mode: str = "team",  # team, ccg, autopilot, ralph, ultrawork
    stages: List[str] = None,
    stage_agents: Dict[str, str] = None,
    models: List[str] = None,
    max_iterations: int = 5,
    parallel: int = 3,
    context: Dict = None,
    parent_agent=None,
    total_minutes: int = None,
) -> str:
    """
    Staged Delegate Tool - Main Entry Point

    Args:
        goal: 작업 목표
        mode: 실행 모드 (team, ccg, autopilot, ralph, ultrawork)
        stages: Team mode용 스테이지 목록
        stage_agents: 스테이지별 에이전트 설정
        models: CCG mode용 모델 목록
        max_iterations: 최대 반복 횟수
        parallel: Ultrawork 병렬 수
        context: 추가 컨텍스트
        parent_agent: 부모 에이전트 참조
        total_minutes: Ralph 모드 시간 제한 (분). 기본 30분.

    Returns:
        JSON 결과 문자열
    """
    orchestrator = StagedDelegateOrchestrator(parent_agent=parent_agent, verbose=True)

    try:
        if mode == "team":
            result = orchestrator.run_team(
                task=goal,
                stages=stages,
                stage_agents=stage_agents,
                max_iterations=max_iterations,
                context=context or {},
            )
        elif mode == "ccg":
            result = orchestrator.run_ccg(
                task=goal,
                models=models or ["claude", "codex", "opencode"],
                context=context or {},
            )
        elif mode in ["autopilot", "ralph", "ultrawork"]:
            result = orchestrator.run_autopilot(
                task=goal,
                mode=mode,
                max_iterations=max_iterations,
                parallel=parallel,
                context=context or {},
                total_minutes=total_minutes,
            )
        else:
            return json.dumps({"error": f"Unknown mode: {mode}"}, ensure_ascii=False)

        return json.dumps(result, ensure_ascii=False, indent=2)

    except Exception as e:
        logger.exception("staged_delegate failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# Legacy alias for compatibility
team_delegate = staged_delegate
ccg_delegate = staged_delegate
autopilot_delegate = staged_delegate


# Register with Hermes tool registry
try:
    from tools.registry import registry

    def _check_staged_delegate() -> bool:
        """Check if staged_delegate requirements are met."""
        return True  # No special requirements

    registry.register(
        name="staged_delegate",
        toolset="delegation",
        schema={
            "name": "staged_delegate",
            "description": "OMC-style multi-agent orchestration with staged pipeline (team/ccg/autopilot/ralph/ultrawork modes). Supports OpenCode, Codex, and Hermes native agents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "Task goal or description"
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["team", "ccg", "autopilot", "ralph", "ultrawork"],
                        "description": "Orchestration mode"
                    },
                    "stages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Stages for team mode (plan, prd, exec, verify, fix)"
                    },
                    "stage_agents": {
                        "type": "object",
                        "description": "Agent mapping per stage (claude, codex, opencode, gemini)"
                    },
                    "models": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Models for CCG mode"
                    },
                    "max_iterations": {
                        "type": "integer",
                        "description": "Max iterations for verify-fix loops",
                        "default": 5
                    },
                    "parallel": {
                        "type": "integer",
                        "description": "Parallel agents for ultrawork mode",
                        "default": 3
                    },
                    "context": {
                        "type": "object",
                        "description": "Additional context"
                    },
                    "total_minutes": {
                        "type": "integer",
                        "description": "Ralph 모드 전용: 강제 반복 시간 (분). 기본 30분.",
                        "default": 30
                    }
                },
                "required": ["goal", "mode"]
            }
        },
        handler=lambda args, **kw: staged_delegate(
            goal=args.get("goal"),
            mode=args.get("mode", "team"),
            stages=args.get("stages"),
            stage_agents=args.get("stage_agents"),
            models=args.get("models"),
            max_iterations=args.get("max_iterations", 5),
            parallel=args.get("parallel", 3),
            context=args.get("context"),
            parent_agent=kw.get("parent_agent"),
            total_minutes=args.get("total_minutes"),
        ),
        check_fn=_check_staged_delegate,
        description="OMC-style multi-agent orchestration with OpenCode/Codex/Hermes integration",
        emoji="🎭",
    )
except ImportError:
    pass  # Registry not available during import
