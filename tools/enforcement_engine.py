#!/usr/bin/env python3
"""
Enforcement Engine for Hermes Harness

강제성 부여 메커니즘:
- 코드량 기반 staged_delegate 강제
- Safe subprocess 사용 강제
- harness.yaml 필수 프로젝트 설정
"""

import logging
import os
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any
import yaml

logger = logging.getLogger(__name__)

@dataclass
class EnforcementResult:
    """강제성 검사 결과"""
    allowed: bool
    reason: str
    suggested_mode: str
    suggested_agents: List[str]
    task_summary: str
    bypassable: bool
    severity: str  # "info", "warning", "error"


class EnforcementEngine:
    """
    Hermes 사용 강제 엔진
    
    설정 기반으로 작업 위험도 평가 및 적절한 도구 강제
    """
    
    DEFAULT_CONFIG = {
        "enforcement": {
            "enabled": True,
            "mode": "warning",  # "warning", "strict", "disabled"
            "min_lines_for_staged": 30,
            "min_files_for_staged": 3,
            "min_complexity_for_staged": 5,  # 예상 파일 변경 수
            "prefer_opencode": True,
            "block_shell_execution": True,
            "require_harness_for": ["*.py", "*.js", "*.ts", "package.json"],
            "bypassable": True,
            "auto_bypass_duration": 300  # 5분간 우회 유지
        }
    }
    
    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path or os.path.expanduser("~/.hermes/config.yaml")
        self.config = self._load_config()
        self._bypass_cache: Dict[str, float] = {}  # 우회 캐시
    
    def _load_config(self) -> Dict:
        """설정 로드"""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r') as f:
                    user_config = yaml.safe_load(f) or {}
                
                # Merge with defaults
                config = self.DEFAULT_CONFIG.copy()
                if "enforcement" in user_config:
                    config["enforcement"].update(user_config["enforcement"])
                return config
            except Exception as e:
                print(f"Warning: Failed to load config: {e}")
                return self.DEFAULT_CONFIG
        return self.DEFAULT_CONFIG
    
    def _get_enforcement_config(self) -> Dict:
        """강제 설정 가져오기"""
        return self.config.get("enforcement", self.DEFAULT_CONFIG["enforcement"])
    
    def estimate_lines(self, goal: str, context: Optional[Dict] = None) -> int:
        """
        작업의 예상 코드량 추정
        
        Args:
            goal: 작업 설명
            context: 컨텍스트 (파일 목록 등)
        
        Returns:
            예상 라인 수
        """
        lines = 0
        
        # 1. 작업 설명 분석 — 키워드 중 가장 높은 값을 사용 (누적 합산 X)
        #    "fix the add button implementation" → max(10, 20, 50) = 50 (이전: 80)
        keywords = {
            # 영어
            "implement": 50, "refactor": 30, "fix": 10,
            "add": 20, "create": 40, "build": 60,
            "migrate": 100, "redesign": 80, "optimize": 25,
            # 한국어
            "구현": 50, "리팩토링": 30, "수정": 10, "고치": 10,
            "추가": 20, "생성": 40, "빌드": 60,
            "마이그레이션": 100, "이관": 80, "최적화": 25,
            "만들어": 40, "개발": 50,
        }
        
        goal_lower = goal.lower()
        matched_scores = [est for kw, est in keywords.items() if kw in goal_lower]
        if matched_scores:
            lines += max(matched_scores)  # 가장 높은 것만 채택
        
        # 2. 복잡도 지표 — 최대 2개만 반영 (과잉 추정 방지)
        complexity_indicators = {
            # 영어
            "auth": 15, "authentication": 15, "oauth": 20, "jwt": 15,
            "database": 15, "migration": 20, "schema": 15,
            "api": 10, "endpoint": 10, "rest": 10, "graphql": 15,
            "frontend": 15, "ui": 10, "component": 10, "page": 10,
            "test": 10, "testing": 10, "coverage": 10,
            # 한국어
            "인증": 15, "데이터베이스": 15, "스키마": 15,
            "프론트엔드": 15, "백엔드": 15, "컴포넌트": 10,
            "테스트": 10,
        }
        
        matched_complexity = sorted(
            [score for ind, score in complexity_indicators.items() if ind in goal_lower],
            reverse=True
        )
        lines += sum(matched_complexity[:2])  # 상위 2개만 합산
        
        # 3. 파일 목록 분석
        if context:
            files = context.get("files", context.get("affected_files", []))
            if files:
                # 파일당 평균 20줄 변경 가정
                lines += len(files) * 20
            
            # Working directory 분석
            working_dir = context.get("working_dir", ".")
            if os.path.exists(working_dir):
                lines += self._analyze_project_complexity(working_dir)
        
        return max(lines, 5)  # 최소 5줄
    
    def _analyze_project_complexity(self, project_path: str) -> int:
        """프로젝트 복잡도 분석 (단일 순회로 성능 최적화)"""
        complexity = 0
        path = Path(project_path)
        
        # 파일 수 기반 — 단일 순회로 확장자별 카운트
        try:
            target_extensions = {".py", ".js", ".ts"}
            file_count = 0
            for f in path.rglob("*"):
                if f.is_file() and f.suffix in target_extensions:
                    file_count += 1
                    if file_count >= 50:  # 조기 종료: 50개 넘으면 더 세지 않음
                        break
            complexity += min(file_count, 50)  # 최대 50점
        except (PermissionError, OSError) as e:
            logger.debug(f"Project complexity scan error: {e}")
        
        # 특정 파일 존재 여부 — 루트 레벨만 확인 (rglob 대신 직접 체크)
        important_files = [
            "package.json", "requirements.txt", "Cargo.toml",
            "Dockerfile", "docker-compose.yml", "Makefile",
        ]
        
        for filename in important_files:
            if (path / filename).exists():
                complexity += 5
        
        return complexity
    
    def count_affected_files(self, context: Optional[Dict]) -> int:
        """영향받는 파일 수 추정"""
        if not context:
            return 0
        if "files" in context:
            return len(context["files"])
        if "affected_files" in context:
            return len(context["affected_files"])
        
        # Git diff로 추정
        working_dir = context.get("working_dir", ".")
        try:
            import subprocess
            result = subprocess.run(
                ["git", "diff", "--name-only"],
                cwd=working_dir,
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                return len(result.stdout.strip().split("\n"))
        except:
            pass
        
        return 0
    
    def check_before_delegate(self, goal: str, context: Optional[Dict] = None) -> EnforcementResult:
        """
        delegate_task 실행 전 강제성 검사
        
        Returns:
            EnforcementResult
        """
        config = self._get_enforcement_config()
        
        # 강제 비활성화
        if not config.get("enabled", True):
            return EnforcementResult(
                allowed=True,
                reason="Enforcement disabled",
                suggested_mode="delegate",
                suggested_agents=[],
                task_summary=goal,
                bypassable=True,
                severity="info"
            )
        
        # 우회 캐시 확인
        cache_key = f"{goal[:50]}_{context.get('working_dir', '') if context else ''}"
        if cache_key in self._bypass_cache:
            elapsed = __import__('time').time() - self._bypass_cache[cache_key]
            if elapsed < config.get("auto_bypass_duration", 300):
                return EnforcementResult(
                    allowed=True,
                    reason=f"Bypass active ({int(300 - elapsed)}s remaining)",
                    suggested_mode="delegate",
                    suggested_agents=[],
                    task_summary=goal,
                    bypassable=True,
                    severity="info"
                )
        
        # 작업 분석 (context가 None일 수 있으므로 방어)
        context = context or {}
        estimated_lines = self.estimate_lines(goal, context)
        affected_files = self.count_affected_files(context)
        
        threshold_lines = config.get("min_lines_for_staged", 30)
        threshold_files = config.get("min_files_for_staged", 3)
        
        # 위험도 평가
        reasons = []
        if estimated_lines > threshold_lines:
            reasons.append(f"{estimated_lines} lines > {threshold_lines} threshold")
        if affected_files > threshold_files:
            reasons.append(f"{affected_files} files > {threshold_files} threshold")
        
        # harness.yaml 필수 검사
        if context:
            working_dir = context.get("working_dir", ".")
            require_patterns = config.get("require_harness_for", [])
            if self._requires_harness(working_dir, require_patterns):
                harness_path = Path(working_dir) / ".hermes" / "harness.yaml"
                if not harness_path.exists():
                    reasons.append("harness.yaml required for this project")
        
        # 강제 여부 결정
        if reasons:
            severity = "error" if config.get("mode") == "strict" else "warning"
            
            # Opencode 사용 권장
            suggested_agents = ["claude", "opencode", "claude"]
            if not self._check_opencode_available():
                suggested_agents = ["claude", "claude", "claude"]
            
            return EnforcementResult(
                allowed=config.get("mode") != "strict",  # strict 모드면 차단
                reason="; ".join(reasons),
                suggested_mode="staged_delegate",
                suggested_agents=suggested_agents,
                task_summary=self._generate_task_summary(goal),
                bypassable=config.get("bypassable", True),
                severity=severity
            )
        
        return EnforcementResult(
            allowed=True,
            reason="Below thresholds",
            suggested_mode="delegate",
            suggested_agents=[],
            task_summary=goal,
            bypassable=True,
            severity="info"
        )
    
    def _requires_harness(self, working_dir: str, patterns: List[str]) -> bool:
        """harness.yaml 필수 여부 검사"""
        path = Path(working_dir)
        
        for pattern in patterns:
            if pattern.startswith("*"):
                ext = pattern[1:]  # *.py -> .py
                if any(path.rglob(f"*{ext}")):
                    return True
            else:
                if any(path.rglob(pattern)):
                    return True
        
        return False
    
    def _check_opencode_available(self) -> bool:
        """Opencode CLI 사용 가능 여부"""
        try:
            import subprocess
            result = subprocess.run(
                ["opencode", "--version"],
                capture_output=True,
                timeout=5
            )
            return result.returncode == 0
        except:
            return False
    
    def _generate_task_summary(self, goal: str) -> str:
        """작업 요약 생성"""
        # 첫 문장 추출
        sentences = goal.split(".")
        summary = sentences[0][:80] + "..." if len(sentences[0]) > 80 else sentences[0]
        return summary
    
    def bypass(self, goal: str, context: Optional[Dict] = None):
        """강제 우회 설정"""
        cache_key = f"{goal[:50]}_{context.get('working_dir', '') if context else ''}"
        self._bypass_cache[cache_key] = __import__('time').time()
    
    def format_message(self, result: EnforcementResult) -> str:
        """사용자 메시지 포맷팅"""
        lines = [
            "",
            "=" * 60,
            "🔍 Hermes Enforcement Check",
            "=" * 60,
            f"Reason: {result.reason}",
            f"Severity: {result.severity.upper()}",
            "",
            f"Suggested: {result.suggested_mode}",
        ]
        
        if result.suggested_mode == "staged_delegate":
            lines.extend([
                "",
                "💡 Usage:",
                f"   staged_delegate(",
                f"       goal='{result.task_summary}',",
                f"       mode='team',",
                f"       stage_agents={{",
                f"           'plan': '{result.suggested_agents[0]}',",
                f"           'exec': '{result.suggested_agents[1]}',",
                f"           'verify': '{result.suggested_agents[2]}'",
                f"       }}",
                f"   )",
            ])
        
        if result.bypassable and not result.allowed:
            lines.extend([
                "",
                "⚙️  To bypass:",
                "   enforcement.bypass(goal, context)",
                "   # or set HERMES_ENFORCE_MODE=warning",
            ])
        
        lines.append("=" * 60)
        return "\n".join(lines)


def enforce_delegate(goal: str, context: Optional[Dict] = None, skip_enforcement: bool = False):
    """
    delegate_task용 강제성 wrapper
    
    Usage:
        from tools.enforcement_engine import enforce_delegate
        
        # 자동 검사
        result = enforce_delegate("implement feature", context)
        if not result.allowed:
            print(result.message)
            # 또는 자동 변환
            return convert_to_staged(result)
    """
    engine = EnforcementEngine()
    
    if skip_enforcement:
        engine.bypass(goal, context)
    
    result = engine.check_before_delegate(goal, context)
    
    # strict 모드에서 차단 시 예외
    if not result.allowed and result.severity == "error":
        raise RuntimeError(
            f"HERMES_ENFORCE_STRICT: delegate_task blocked\n"
            f"Reason: {result.reason}\n"
            f"Use: staged_delegate(goal='{result.task_summary}', mode='{result.suggested_mode}')"
        )
    
    # warning 모드에서 경고 출력
    if result.severity == "warning":
        message = engine.format_message(result)
        warnings.warn(message, UserWarning)
    
    return result


def convert_to_staged(result: EnforcementResult) -> Dict[str, Any]:
    """
    EnforcementResult를 staged_delegate 파라미터로 변환
    """
    return {
        "goal": result.task_summary,
        "mode": result.suggested_mode,
        "stage_agents": {
            "plan": result.suggested_agents[0] if len(result.suggested_agents) > 0 else "claude",
            "prd": result.suggested_agents[0] if len(result.suggested_agents) > 0 else "claude",
            "exec": result.suggested_agents[1] if len(result.suggested_agents) > 1 else "opencode",
            "verify": result.suggested_agents[2] if len(result.suggested_agents) > 2 else "claude",
        }
    }


# Convenience function for automatic conversion
def smart_delegate(goal: str, context: Optional[Dict] = None, **kwargs) -> Any:
    """
    똑똑한 위임: 상황에 따라 delegate_task 또는 staged_delegate 선택
    
    Usage:
        result = smart_delegate("implement feature", context)
        # 자동으로 staged_delegate로 변환 가능
    """
    from tools.delegate_tool import delegate_task
    from tools.staged_delegate_tool import staged_delegate
    
    result = enforce_delegate(goal, context, skip_enforcement=kwargs.get("skip_enforcement", False))
    
    if result.suggested_mode == "staged_delegate":
        # staged_delegate로 변환
        staged_params = convert_to_staged(result)
        staged_params.update({k: v for k, v in kwargs.items() if k not in staged_params})
        return staged_delegate(**staged_params)
    else:
        # 일반 delegate
        return delegate_task(goal=goal, context=context, **kwargs)


if __name__ == "__main__":
    # 테스트
    engine = EnforcementEngine()
    
    test_cases = [
        ("fix typo", {"working_dir": "."}),  # 간단한 작업
        ("implement user authentication with OAuth2 and JWT", {"working_dir": "."}),  # 복잡한 작업
        ("refactor database layer", {"files": ["models.py", "migrations/001.py", "api.py"]}),  # 파일 변경
    ]
    
    print("Enforcement Engine Tests:")
    print("=" * 60)
    
    for goal, context in test_cases:
        result = engine.check_before_delegate(goal, context)
        print(f"\nGoal: {goal[:50]}...")
        print(f"  Lines estimate: {engine.estimate_lines(goal, context)}")
        print(f"  Allowed: {result.allowed}")
        print(f"  Suggested: {result.suggested_mode}")
        print(f"  Severity: {result.severity}")
        if result.reason:
            print(f"  Reason: {result.reason}")
    
    print("\n" + "=" * 60)
    print("✅ Enforcement Engine operational")
