#!/usr/bin/env python3
"""
Opencode Team Pipeline

Opencode CLI를 팀 에이전트로 활용하는 완전한 파이프라인
- 자동 팀 구성
- 병렬 실행
- 결과 종합
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from pathlib import Path
import time

from tools.async_cli_runner import AsyncCLIRunner, CLITask, CLIResult

logger = logging.getLogger(__name__)


@dataclass
class AgentTask:
    """에이전트 작업 정의"""
    agent_id: str
    role: str  # frontend, backend, test, review, plan, etc.
    prompt: str
    working_dir: str
    dependencies: List[str] = field(default_factory=list)  # 선행 작업 IDs
    timeout: int = 300
    priority: int = 0  # 높을수록 먼저
    cli_name: str = "opencode"  # "opencode" or "cline"


@dataclass
class TeamResult:
    """팀 실행 결과"""
    success: bool
    results: Dict[str, Dict[str, Any]]  # agent_id -> result
    execution_order: List[str]
    total_duration: float
    synthesis: Optional[str] = None
    errors: List[str] = None
    rollback_performed: bool = False
    failed_agents: List[str] = None
    
    def __post_init__(self):
        if self.errors is None:
            self.errors = []
        if self.failed_agents is None:
            self.failed_agents = []


# 팀 역할 템플릿
TEAM_ROLE_TEMPLATES = {
    "plan": {
        "prompt_template": """작업 분석 및 계획:
{task_description}

다음을 분석하세요:
1. 작업을 3개의 하위 작업으로 분할
2. 각 작업의 담당 역할 (frontend/backend/test)
3. 작업 간 의존성
4. 예상 소요 시간

출력 형식 (JSON):
{{"tasks": [
  {{"role": "frontend", "description": "...", "estimated_minutes": 30}},
  {{"role": "backend", "description": "...", "estimated_minutes": 45}},
  {{"role": "test", "description": "...", "estimated_minutes": 20, "dependencies": ["frontend", "backend"]}}
]}}

위 형식을 참고하여 실제 작업에 맞는 JSON을 출력하세요.""",
        "timeout": 120
    },
    "frontend": {
        "prompt_template": """Frontend 구현:
{task_description}

요구사항:
- React/TypeScript 사용
- 반응형 디자인
- 접근성 고려
- 에러 핸들링

변경할 파일들을 직접 수정하세요.""",
        "timeout": 300
    },
    "backend": {
        "prompt_template": """Backend 구현:
{task_description}

요구사항:
- API 엔드포인트 설계
- 데이터베이스 스키마/마이그레이션
- 에러 처리 및 로깅
- 인증/인가 고려

변경할 파일들을 직접 수정하세요.""",
        "timeout": 300
    },
    "test": {
        "prompt_template": """테스트 작성:
{task_description}

요구사항:
- 단위 테스트
- 통합 테스트
- 엣지 케이스 고려
- 테스트 커버리지 80%+

test 파일을 생성/수정하세요.""",
        "timeout": 200
    },
    "review": {
        "prompt_template": """코드 리뷰:
{task_description}

검토 항목:
1. 코드 품질 및 가독성
2. 보안 취약점
3. 성능 이슈
4. 테스트 커버리지
5. 문서화 상태

각 항목에 대해 점수(1-5)와 개선 제안을 제공하세요.""",
        "timeout": 180
    },
    "synthesize": {
        "prompt_template": """결과 종합:

작업: {task_description}

개별 결과:
{individual_results}

위 결과들을 종합하여:
1. 전체 작업 요약
2. 성공/실패 여부
3. 주요 변경사항
4. 다음 단계 (필요시)

통합된 보고서를 작성하세요.""",
        "timeout": 120
    }
}


class OpencodeTeamPipeline:
    """
    Opencode CLI 기반 팀 파이프라인
    
    사용 예시:
        pipeline = OpencodeTeamPipeline(max_agents=3)
        team = pipeline.create_team("/path", "Build REST API")
        result = pipeline.execute_team(team)
    """
    
    def __init__(self, max_agents: int = 3, use_synthesis: bool = True, default_timeout: int = 180):
        self.max_agents = max_agents
        self.use_synthesis = use_synthesis
        self.default_timeout = default_timeout
        self.runner = AsyncCLIRunner(max_concurrent=max_agents)
        self._progress_callbacks: List[Callable] = []
    
    def on_progress(self, callback: Callable[[str, str, float, str], None]):
        """
        진행 상황 콜백 등록
        
        Args:
            callback: (agent_id, status, progress, role) -> None
        """
        self._progress_callbacks.append(callback)
    
    def _notify_progress(self, agent_id: str, status: str, progress: float, role: str = ""):
        """진행 상황 알림"""
        for callback in self._progress_callbacks:
            try:
                callback(agent_id, status, progress, role)
            except Exception as e:
                logger.warning(f"Progress callback error: {e}")
    
    def create_team(
        self,
        project_path: str,
        task_description: str,
        auto_plan: bool = True
    ) -> List[AgentTask]:
        """
        작업에 따른 팀 자동 구성
        
        Args:
            project_path: 프로젝트 경로
            task_description: 작업 설명
            auto_plan: 자동 분할 사용
        
        Returns:
            AgentTask 리스트
        """
        if auto_plan:
            # Plan 단계: Opencode로 작업 분할
            plan_template = TEAM_ROLE_TEMPLATES["plan"]
            plan_prompt = plan_template["prompt_template"].format(
                task_description=task_description
            )
            
            logger.info("Running planning phase...")
            plan_task = CLITask(
                task_id="opencode-planner",
                cli_name="opencode",
                prompt=plan_prompt,
                working_dir=project_path,
                timeout=plan_template["timeout"]
            )
            
            plan_result = self.runner.run_sync([plan_task])[0]
            
            if plan_result.success:
                try:
                    # Parse JSON from output
                    parsed_tasks = self._parse_plan_output(plan_result.output)
                    return self._build_team_from_plan(
                        parsed_tasks, project_path, task_description
                    )
                except Exception as e:
                    logger.warning(f"Failed to parse plan: {e}, using default team")
        
        # Fallback: 기본 팀 구성
        return self._create_default_team(project_path, task_description)
    
    def _parse_plan_output(self, output: str) -> List[Dict]:
        """Plan 단계 출력 파싱"""
        # JSON 찾기
        try:
            # Markdown code block에서 추출
            if "```json" in output:
                json_start = output.find("```json") + 7
                json_end = output.find("```", json_start)
                json_str = output[json_start:json_end].strip()
            elif "```" in output:
                json_start = output.find("```") + 3
                json_end = output.find("```", json_start)
                json_str = output[json_start:json_end].strip()
            else:
                # Assume entire output is JSON
                json_str = output
            
            data = json.loads(json_str)
            return data.get("tasks", [])
        except:
            # Fallback: simple parsing
            return []
    
    def _build_team_from_plan(
        self,
        planned_tasks: List[Dict],
        project_path: str,
        original_task: str
    ) -> List[AgentTask]:
        """계획 기반 팀 구성"""
        team = []
        role_counts = {}
        
        for i, task_info in enumerate(planned_tasks):
            role = task_info.get("role", "generic")
            description = task_info.get("description", "")
            dependencies = task_info.get("dependencies", [])
            
            # Role별 카운트
            role_counts[role] = role_counts.get(role, 0) + 1
            agent_id = f"opencode-{role}-{role_counts[role]}"
            
            # 템플릿 적용
            template = TEAM_ROLE_TEMPLATES.get(role, TEAM_ROLE_TEMPLATES["frontend"])
            prompt = template["prompt_template"].format(
                task_description=f"{original_task}\n\n세부 작업: {description}"
            )
            
            agent_task = AgentTask(
                agent_id=agent_id,
                role=role,
                prompt=prompt,
                working_dir=project_path,
                dependencies=dependencies,
                timeout=template.get("timeout", 300),
                priority=len(planned_tasks) - i  # 선행 작업이 높은 우선순위
            )
            team.append(agent_task)
        
        return team
    
    def _create_default_team(self, project_path: str, task: str) -> List[AgentTask]:
        """기본 팀 구성 (Plan 실패 시 Fallback)"""
        team = []
        
        # 파일 패턴 기반 역할 결정
        path = Path(project_path)
        has_frontend = any(path.rglob("*.tsx")) or any(path.rglob("*.jsx"))
        has_backend = any(path.rglob("*.py")) or any(path.rglob("api/"))
        
        if has_frontend:
            template = TEAM_ROLE_TEMPLATES["frontend"]
            team.append(AgentTask(
                agent_id="opencode-frontend-1",
                role="frontend",
                prompt=template["prompt_template"].format(task_description=task),
                working_dir=project_path,
                timeout=template["timeout"],
                priority=3
            ))
        
        if has_backend:
            template = TEAM_ROLE_TEMPLATES["backend"]
            team.append(AgentTask(
                agent_id="opencode-backend-1",
                role="backend",
                prompt=template["prompt_template"].format(task_description=task),
                working_dir=project_path,
                timeout=template["timeout"],
                priority=3
            ))
        
        # Test는 항상 추가 (의존성 설정)
        template = TEAM_ROLE_TEMPLATES["test"]
        deps = [a.agent_id for a in team]  # 이전 작업들에 의존
        team.append(AgentTask(
            agent_id="opencode-test-1",
            role="test",
            prompt=template["prompt_template"].format(task_description=task),
            working_dir=project_path,
            dependencies=deps,
            timeout=template["timeout"],
            priority=1  # 낮은 우선순위 (의존성 때문)
        ))
        
        return team
    
    def execute_team(
        self,
        team: List[AgentTask],
        progress_callback: Optional[Callable[[str, str, float, str], None]] = None
    ) -> TeamResult:
        """
        팀 병렬 실행
        
        Args:
            team: 실행할 에이전트 목록
            progress_callback: 진행 상황 콜백
        
        Returns:
            TeamResult
        """
        if progress_callback:
            self.on_progress(progress_callback)
        
        start_time = time.time()
        
        # 의존성 그래프 분석
        execution_order = self._resolve_dependencies(team)
        
        # 결과 저장
        results: Dict[str, CLIResult] = {}
        
        # 의존성 단계별 실행
        for level in execution_order:
            level_tasks = [t for t in team if t.agent_id in level]
            
            logger.info(f"Executing level: {[t.agent_id for t in level_tasks]}")
            
            # Async runner용 CLITask로 변환
            cli_tasks = [
                CLITask(
                    task_id=t.agent_id,
                    cli_name="opencode",
                    prompt=t.prompt,
                    working_dir=t.working_dir,
                    timeout=max(t.timeout, self.default_timeout),  # Use longer timeout (Z.AI needs more time)
                    extra_args=["--dangerously-skip-permissions"]  # Auto-approve for non-interactive mode
                )
                for t in level_tasks
            ]
            
            # 병렬 실행
            level_results = self.runner.run_sync(cli_tasks)
            
            # 결과 저장
            for result in level_results:
                results[result.task_id] = result
                self._notify_progress(
                    result.task_id,
                    "completed" if result.success else "failed",
                    1.0,
                    next((t.role for t in team if t.agent_id == result.task_id), "")
                )
        
        total_duration = time.time() - start_time
        
        # 결과 포맷 변환
        formatted_results = {}
        for agent_id, result in results.items():
            formatted_results[agent_id] = {
                "success": result.success,
                "output": result.output,
                "exit_code": result.exit_code,
                "duration_seconds": result.duration_seconds
            }
        
        # 종합 (선택적)
        synthesis = None
        if self.use_synthesis and all(r.success for r in results.values()):
            synthesis = self._run_synthesis(team[0].working_dir, formatted_results)
        
        success = all(r.success for r in results.values())
        
        return TeamResult(
            success=success,
            results=formatted_results,
            execution_order=execution_order,
            total_duration=total_duration,
            synthesis=synthesis
        )
    
    def _resolve_dependencies(self, team: List[AgentTask]) -> List[List[str]]:
        """
        의존성 그래프를 실행 레벨로 변환
        
        Returns:
            List of agent_id lists, ordered by execution level
        """
        # Kahn's algorithm
        in_degree = {t.agent_id: 0 for t in team}
        graph = {t.agent_id: [] for t in team}
        
        for task in team:
            for dep in task.dependencies:
                if dep in graph:
                    graph[dep].append(task.agent_id)
                    in_degree[task.agent_id] += 1
        
        levels = []
        remaining = set(t.agent_id for t in team)
        
        while remaining:
            # Find nodes with no dependencies
            level = [agent_id for agent_id in remaining if in_degree[agent_id] == 0]
            
            if not level:
                # Cycle detected, break remaining
                levels.append(list(remaining))
                break
            
            levels.append(level)
            remaining -= set(level)
            
            # Update in-degrees
            for agent_id in level:
                for dependent in graph[agent_id]:
                    in_degree[dependent] -= 1
        
        return levels
    
    def _run_synthesis(
        self,
        project_path: str,
        individual_results: Dict[str, Dict]
    ) -> Optional[str]:
        """
        개별 결과 종합
        
        Returns:
            종합된 보고서 또는 None
        """
        try:
            # 결과 텍스트 구성
            result_texts = []
            for agent_id, result in individual_results.items():
                result_texts.append(f"\n=== {agent_id} ===\n{result['output'][:1000]}")
            
            combined = "\n".join(result_texts)
            
            # Synthesis prompt
            template = TEAM_ROLE_TEMPLATES["synthesize"]
            prompt = template["prompt_template"].format(
                task_description="Team execution results",
                individual_results=combined
            )
            
            # Opencode로 종합
            synth_task = CLITask(
                task_id="opencode-synthesizer",
                cli_name="opencode",
                prompt=prompt,
                working_dir=project_path,
                timeout=template["timeout"]
            )
            
            synth_result = self.runner.run_sync([synth_task])[0]
            
            if synth_result.success:
                return synth_result.output
            else:
                return f"Synthesis failed: {synth_result.output}"
                
        except Exception as e:
            logger.error(f"Synthesis error: {e}")
            return None


# Convenience functions
def run_team_pipeline(
    project_path: str,
    task_description: str,
    max_agents: int = 3,
    progress_callback: Optional[Callable] = None
) -> Dict[str, Any]:
    """
    간편한 팀 파이프라인 실행
    
    Args:
        project_path: 프로젝트 경로
        task_description: 작업 설명
        max_agents: 최대 병렬 에이전트 수
        progress_callback: 진행 상황 콜백
    
    Returns:
        결과 dict
    """
    pipeline = OpencodeTeamPipeline(max_agents=max_agents)
    team = pipeline.create_team(project_path, task_description)
    
    result = pipeline.execute_team(team, progress_callback)
    
    return {
        "success": result.success,
        "total_duration": result.total_duration,
        "results": result.results,
        "synthesis": result.synthesis,
        "execution_order": result.execution_order
    }


# CLI interface
if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 3:
        print("Usage: python opencode_team_pipeline.py <project_path> <task_description>")
        sys.exit(1)
    
    project_path = sys.argv[1]
    task_description = sys.argv[2]
    
    print(f"🤖 Opencode Team Pipeline")
    print(f"   Project: {project_path}")
    print(f"   Task: {task_description}")
    print()
    
    def on_progress(agent_id, status, progress, role):
        icon = "⏳" if status == "started" else "🔄" if status == "running" else "✅" if status == "completed" else "❌"
        print(f"   {icon} {agent_id} ({role}): {status} ({progress:.0%})")
    
    result = run_team_pipeline(project_path, task_description, progress_callback=on_progress)
    
    print()
    print(f"{'✅' if result['success'] else '❌'} Result: {'Success' if result['success'] else 'Failed'}")
    print(f"   Total duration: {result['total_duration']:.1f}s")
    print(f"   Agents executed: {len(result['results'])}")
    
    if result['synthesis']:
        print()
        print("📋 Synthesis:")
        print(result['synthesis'][:500] + "...")
