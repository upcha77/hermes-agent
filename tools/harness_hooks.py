#!/usr/bin/env python3
"""
Harness Hooks System - Quality Gates for Multi-Agent Orchestration

레퍼런스: 멀티 에이전트 하네스 설계 (wikidocs)
- SubagentStop 훅 구현
- 품질 게이트 자동화
- 프로젝트별 설정 지원
"""

import json
import logging
import os
import subprocess
import time

# Safe subprocess for security (no shell=True)
from tools.safe_subprocess import run_hook_safe, CommandInjectionError
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class HookResult:
    """훅 실행 결과"""
    success: bool
    hook_name: str
    output: str
    exit_code: int = 0
    duration_ms: int = 0


@dataclass
class HarnessConfig:
    """프로젝트 하네스 설정"""
    project_root: Path
    hooks_dir: Optional[Path] = None
    config_file: Optional[Path] = None
    
    # 훅 설정
    on_staged_complete: List[str] = field(default_factory=list)
    on_verify_fail: List[str] = field(default_factory=list)
    on_exec_complete: List[str] = field(default_factory=list)
    
    # 검증 설정
    lint_commands: List[str] = field(default_factory=list)
    test_commands: List[str] = field(default_factory=list)
    
    # 에이전트 설정 (프로젝트별 오버라이드)
    stage_agents: Dict[str, str] = field(default_factory=dict)
    
    @classmethod
    def from_project(cls, project_path: Optional[str] = None) -> "HarnessConfig":
        """프로젝트 경로에서 설정 로드"""
        if project_path:
            root = Path(project_path).resolve()
        else:
            # 현재 작업 디렉토리에서 .hermes 찾기
            cwd = Path.cwd()
            root = cwd
            while root.parent != root:
                if (root / ".hermes").exists():
                    break
                root = root.parent
        
        config = cls(project_root=root)
        
        # 설정 파일 로드
        config_file = root / ".hermes" / "harness.yaml"
        if config_file.exists():
            config.config_file = config_file
            config._load_yaml(config_file)
        
        # 훅 디렉토리 설정
        hooks_dir = root / ".hermes" / "hooks"
        if hooks_dir.exists():
            config.hooks_dir = hooks_dir
            config._load_hooks(hooks_dir)
        
        return config
    
    def _load_yaml(self, path: Path):
        """YAML 설정 파일 로드"""
        try:
            import yaml
            with open(path, 'r') as f:
                data = yaml.safe_load(f)
            
            if not data:
                return
            
            # 훅 설정
            hooks = data.get('hooks', {})
            self.on_staged_complete = hooks.get('on_staged_complete', [])
            self.on_verify_fail = hooks.get('on_verify_fail', [])
            self.on_exec_complete = hooks.get('on_exec_complete', [])
            
            # 검증 설정
            validation = data.get('validation', {})
            self.lint_commands = validation.get('lint', [])
            self.test_commands = validation.get('test', [])
            
            # 에이전트 설정
            self.stage_agents = data.get('stage_agents', {})
            
        except ImportError:
            logger.warning("PyYAML not installed, skipping YAML config")
        except Exception as e:
            logger.warning(f"Failed to load harness.yaml: {e}")
    
    def _load_hooks(self, hooks_dir: Path):
        """훅 디렉토리에서 스크립트 로드"""
        hook_mapping = {
            'on_staged_complete': self.on_staged_complete,
            'on_verify_fail': self.on_verify_fail,
            'on_exec_complete': self.on_exec_complete,
        }
        
        for hook_name, command_list in hook_mapping.items():
            hook_file = hooks_dir / f"{hook_name}.py"
            if hook_file.exists():
                command_list.insert(0, f"python3 {hook_file}")


class HarnessHookManager:
    """
    하네스 훅 관리자
    
    staged_delegate 완료 후 품질 게이트 실행
    """
    
    def __init__(self, config: Optional[HarnessConfig] = None):
        self.config = config or HarnessConfig.from_project()
        self.results: List[HookResult] = []
    
    def run_hook(
        self,
        hook_name: str,
        commands: List[str],
        context: Dict[str, Any],
        working_dir: Optional[str] = None
    ) -> List[HookResult]:
        """
        훅 실행 (safe_subprocess 사용, shell=True 금지)
        
        Args:
            hook_name: 훅 이름 (로깅용)
            commands: 실행할 명령어 목록
            context: 컨텍스트 데이터 (환경변수로 전달)
            working_dir: 작업 디렉토리
        """
        workdir = working_dir or str(self.config.project_root)
        
        # run_hook_safe가 내부적으로 shlex.split + shell=False + 환경변수 변환을 처리
        safe_results = run_hook_safe(
            hook_name=hook_name,
            commands=commands,
            context=context,
            working_dir=workdir,
            timeout=300
        )
        
        # safe_subprocess의 dict 결과를 HookResult dataclass로 변환
        results = []
        for sr in safe_results:
            hook_result = HookResult(
                success=sr.get('success', False),
                hook_name=sr.get('hook_name', hook_name),
                output=sr.get('output', ''),
                exit_code=sr.get('exit_code', -1),
                duration_ms=sr.get('duration_ms', 0)
            )
            results.append(hook_result)
            
            # 실패 시 중단 (fail-fast) — run_hook_safe 내부에서도 처리하지만 명시적으로
            if not hook_result.success:
                logger.warning(f"Hook {hook_name} failed: {sr.get('command', 'unknown')}")
                break
        
        return results
    
    def on_staged_complete(
        self,
        stage_results: Dict[str, Any],
        final_code: str,
        working_dir: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        staged_delegate 완료 후 훅 실행
        
        Args:
            stage_results: 각 stage의 결과
            final_code: 최종 코드
            working_dir: 작업 디렉토리
        """
        context = {
            'stage_results': json.dumps(stage_results, ensure_ascii=False),
            'final_code': final_code[:1000],  # 처음 1000자만
            'project_root': str(self.config.project_root),
        }
        
        # 설정된 훅 실행
        hook_results = self.run_hook(
            'on_staged_complete',
            self.config.on_staged_complete,
            context,
            working_dir
        )
        
        # 기본 검증 실행 (설정된 검증 명령)
        if not hook_results or all(r.success for r in hook_results):
            validation_results = self._run_default_validation(working_dir)
            hook_results.extend(validation_results)
        
        return {
            'success': all(r.success for r in hook_results),
            'hooks': [self._hook_to_dict(r) for r in hook_results],
            'passed': sum(1 for r in hook_results if r.success),
            'failed': sum(1 for r in hook_results if not r.success),
        }
    
    def on_verify_fail(
        self,
        issues: List[str],
        code: str,
        working_dir: Optional[str] = None
    ) -> Dict[str, Any]:
        """검증 실패 시 훅 실행"""
        context = {
            'issues': json.dumps(issues, ensure_ascii=False),
            'code': code[:1000],
            'project_root': str(self.config.project_root),
        }
        
        hook_results = self.run_hook(
            'on_verify_fail',
            self.config.on_verify_fail,
            context,
            working_dir
        )
        
        return {
            'success': all(r.success for r in hook_results),
            'hooks': [self._hook_to_dict(r) for r in hook_results],
        }
    
    def on_exec_complete(
        self,
        exec_output: str,
        files_modified: List[str],
        working_dir: Optional[str] = None
    ) -> Dict[str, Any]:
        """exec stage 완료 후 훅 실행"""
        context = {
            'exec_output': exec_output[:1000],
            'files_modified': json.dumps(files_modified, ensure_ascii=False),
            'project_root': str(self.config.project_root),
        }
        
        hook_results = self.run_hook(
            'on_exec_complete',
            self.config.on_exec_complete,
            context,
            working_dir
        )
        
        return {
            'success': all(r.success for r in hook_results),
            'hooks': [self._hook_to_dict(r) for r in hook_results],
        }
    
    def _run_default_validation(
        self,
        working_dir: Optional[str] = None
    ) -> List[HookResult]:
        """기본 검증 명령 실행 (lint, test)"""
        results = []
        workdir = working_dir or str(self.config.project_root)
        
        # Lint 실행
        for lint_cmd in self.config.lint_commands:
            result = self.run_hook('lint', [lint_cmd], {}, workdir)
            results.extend(result)
        
        # Test 실행
        for test_cmd in self.config.test_commands:
            result = self.run_hook('test', [test_cmd], {}, workdir)
            results.extend(result)
        
        return results
    
    def _hook_to_dict(self, result: HookResult) -> Dict:
        """HookResult를 dict로 변환"""
        return {
            'success': result.success,
            'hook_name': result.hook_name,
            'output': result.output[:500] + '...' if len(result.output) > 500 else result.output,
            'exit_code': result.exit_code,
            'duration_ms': result.duration_ms,
        }
    
    def get_stage_agent(self, stage: str, default: str = "claude") -> str:
        """프로젝트 설정에서 stage별 에이전트 조회"""
        return self.config.stage_agents.get(stage, default)


# Convenience functions for direct use
def run_staged_complete_hooks(
    stage_results: Dict[str, Any],
    final_code: str,
    working_dir: Optional[str] = None
) -> Dict[str, Any]:
    """staged_delegate 완료 후 훅 실행 (편의 함수)"""
    manager = HarnessHookManager()
    return manager.on_staged_complete(stage_results, final_code, working_dir)


def run_verify_fail_hooks(
    issues: List[str],
    code: str,
    working_dir: Optional[str] = None
) -> Dict[str, Any]:
    """검증 실패 후 훅 실행 (편의 함수)"""
    manager = HarnessHookManager()
    return manager.on_verify_fail(issues, code, working_dir)


def get_project_harness_config(project_path: Optional[str] = None) -> HarnessConfig:
    """프로젝트 하네스 설정 조회"""
    return HarnessConfig.from_project(project_path)


# Register with Hermes tool registry
try:
    from tools.registry import registry
    import time
    
    def _check_harness_hooks() -> bool:
        """harness_hooks requirements check"""
        return True
    
    registry.register(
        name="run_harness_hooks",
        toolset="delegation",
        schema={
            "name": "run_harness_hooks",
            "description": "Run harness quality gates (lint, test, custom hooks) after staged_delegate completes. Supports project-specific .hermes/harness.yaml configuration.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hook_type": {
                        "type": "string",
                        "enum": ["on_staged_complete", "on_verify_fail", "on_exec_complete"],
                        "description": "Type of hook to run"
                    },
                    "context": {
                        "type": "object",
                        "description": "Hook context data (stage_results, final_code, etc.)"
                    },
                    "working_dir": {
                        "type": "string",
                        "description": "Working directory for hook execution"
                    }
                },
                "required": ["hook_type", "context"]
            }
        },
        handler=lambda args, **kw: json.dumps(run_harness_hooks(
            stage_results=args.get('context', {}),
            final_code=args.get('context', {}).get('final_code', ''),
            working_dir=args.get('working_dir')
        ), ensure_ascii=False),
        check_fn=_check_harness_hooks,
        description="Run harness quality gates after subagent completion",
        emoji="🎪",
    )
except ImportError:
    pass  # Registry not available during import
