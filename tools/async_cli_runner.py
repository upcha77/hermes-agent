#!/usr/bin/env python3
"""
Async CLI Runner for Opencode/Codex Integration

Opencode CLI를 효율적으로 활용하기 위한 async subprocess wrapper
- Non-blocking execution
- Progress reporting
- Result aggregation
"""

import asyncio
import json
import logging
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Callable
from pathlib import Path

from tools.safe_subprocess import run_command_safe
import os
from tools.safe_subprocess import run_command_safe

logger = logging.getLogger(__name__)


@dataclass
class CLITask:
    """CLI 작업 정의"""
    task_id: str
    cli_name: str  # "opencode" or "codex"
    prompt: str
    working_dir: Optional[str] = None
    context: Optional[Dict] = None
    model: Optional[str] = None
    timeout: int = 300
    priority: int = 0  # Higher = first
    extra_args: Optional[List[str]] = None  # Extra CLI arguments


@dataclass
class CLIResult:
    """CLI 실행 결과"""
    task_id: str
    cli_name: str
    success: bool
    output: str
    exit_code: int
    duration_seconds: float
    artifacts: Dict[str, Any]
    error: Optional[str] = None


class AsyncCLIRunner:
    """
    비동기 CLI 실행기
    
    Opencode/Codex를 병렬로 실행하고 결과를 수집
    """
    
    def __init__(self, max_concurrent: int = 3):
        self.max_concurrent = max_concurrent
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.results: Dict[str, CLIResult] = {}
        self._progress_callbacks: List[Callable] = []
    
    def on_progress(self, callback: Callable[[str, str, float], None]):
        """진행 상황 콜백 등록"""
        self._progress_callbacks.append(callback)
    
    def _notify_progress(self, task_id: str, status: str, progress: float):
        """진행 상황 알림"""
        for callback in self._progress_callbacks:
            try:
                callback(task_id, status, progress)
            except Exception as e:
                logger.warning(f"Progress callback error: {e}")
    
    async def run_task_with_retry(self, task: CLITask, max_retries: int = 1) -> CLIResult:
        """
        단일 CLI 작업 실행 (재시도 지원)
        
        Args:
            task: 실행할 작업
            max_retries: 타임아웃 시 재시도 횟수 (기본: 1)
        
        Returns:
            CLIResult
        """
        for attempt in range(max_retries + 1):
            result = await self._run_single(task)
            
            # 성공 시 즉시 반환
            if result.success:
                return result
            
            # 타임아웃이 아니면 재시도하지 않음
            if result.exit_code != -1:  # -1 = timeout
                return result
            
            # 마지막 시도였으면 반환
            if attempt == max_retries:
                return result
            
            # 재시도 전 대기
            print(f"  ⚠️ {task.task_id} timeout, retrying in 5s... (attempt {attempt + 1}/{max_retries})")
            await asyncio.sleep(5)
        
        return result
    
    async def run_task(self, task: CLITask) -> CLIResult:
        """단일 CLI 작업 실행 (재시도 없음, backward compatible)"""
        return await self._run_single(task)
    
    async def _run_single(self, task: CLITask) -> CLIResult:
        """단일 작업 실행 (내부)"""
        async with self.semaphore:
            start_time = time.time()
            self._notify_progress(task.task_id, "starting", 0.0)
            
            # Create context file if needed
            context_file = None
            if task.context:
                with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                    json.dump(task.context, f, ensure_ascii=False, indent=2)
                    context_file = f.name
            
            try:
                # Build command
                if task.cli_name == "opencode":
                    cmd = ["opencode", "run", task.prompt, "--format", "json"]
                    if context_file:
                        cmd.extend(["--file", context_file])
                    if task.model:
                        cmd.extend(["--model", task.model])
                    # Add extra args for non-interactive mode (e.g., --dangerously-skip-permissions)
                    if task.extra_args:
                        cmd.extend(task.extra_args)
                elif task.cli_name == "cline":
                    # Cline with Fireworks Kimi K2.5 Turbo
                    cmd = ["cline", "--yolo"]  # Auto-approve mode
                    if task.model:
                        cmd.extend(["--model", task.model])
                    else:
                        # Default: Kimi K2.5 Turbo via Fireworks
                        cmd.extend(["--model", "fireworks/accounts/fireworks/routers/kimi-k2p5-turbo"])
                    cmd.append(task.prompt)
                    # Cline reads FIREWORKS_API_KEY from env
                elif task.cli_name == "codex":
                    cmd = ["codex", "--approval-mode", "auto-edit", "--message", task.prompt]
                else:
                    return CLIResult(
                        task_id=task.task_id,
                        cli_name=task.cli_name,
                        success=False,
                        output="",
                        exit_code=-1,
                        duration_seconds=0,
                        artifacts={},
                        error=f"Unknown CLI: {task.cli_name}"
                    )
                
                self._notify_progress(task.task_id, "running", 0.3)
                
                # Run in thread pool (since subprocess is blocking)
                loop = asyncio.get_event_loop()
                exit_code, stdout, stderr = await loop.run_in_executor(
                    None,
                    lambda: run_command_safe(
                        cmd,
                        cwd=task.working_dir,
                        timeout=task.timeout
                    )
                )
                
                duration = time.time() - start_time
                self._notify_progress(task.task_id, "completed", 1.0)
                
                return CLIResult(
                    task_id=task.task_id,
                    cli_name=task.cli_name,
                    success=exit_code == 0,
                    output=stdout if exit_code == 0 else stderr,
                    exit_code=exit_code,
                    duration_seconds=round(duration, 2),
                    artifacts={},  # Parse from stdout if JSON
                    error=None if exit_code == 0 else f"Exit code: {exit_code}"
                )
                
            except Exception as e:
                duration = time.time() - start_time
                self._notify_progress(task.task_id, "failed", 1.0)
                
                return CLIResult(
                    task_id=task.task_id,
                    cli_name=task.cli_name,
                    success=False,
                    output=str(e),
                    exit_code=-1,
                    duration_seconds=round(duration, 2),
                    artifacts={},
                    error=str(e)
                )
            finally:
                if context_file:
                    try:
                        Path(context_file).unlink()
                    except:
                        pass
    
    async def run_parallel(self, tasks: List[CLITask]) -> List[CLIResult]:
        """
        여러 CLI 작업 병렬 실행
        
        Args:
            tasks: 실행할 작업 목록
        
        Returns:
            결과 목록 (입력 순서와 동일)
        """
        # Sort by priority
        sorted_tasks = sorted(tasks, key=lambda t: -t.priority)
        
        # Create futures
        futures = [self.run_task(task) for task in sorted_tasks]
        
        # Execute all
        results = await asyncio.gather(*futures, return_exceptions=True)
        
        # Convert exceptions to error results
        processed_results = []
        for i, result in enumerate(results):
            task_id = sorted_tasks[i].task_id
            if isinstance(result, Exception):
                processed_results.append(CLIResult(
                    task_id=task_id,
                    cli_name=sorted_tasks[i].cli_name,
                    success=False,
                    output=str(result),
                    exit_code=-1,
                    duration_seconds=0,
                    artifacts={},
                    error=str(result)
                ))
            else:
                processed_results.append(result)
        
        return processed_results
    
    def run_sync(self, tasks: List[CLITask]) -> List[CLIResult]:
        """동기 wrapper"""
        return asyncio.run(self.run_parallel(tasks))


# Opencode-specific helpers
class OpencodeRunner:
    """Opencode CLI 전용 러너"""
    
    def __init__(self, default_model: Optional[str] = None):
        self.runner = AsyncCLIRunner()
        self.default_model = default_model
        self._check_available()
    
    def _check_available(self):
        """Opencode 사용 가능 여부 확인"""
        available, info = check_cli_available("opencode")
        if not available:
            logger.warning(f"Opencode CLI not available: {info}")
        else:
            logger.info(f"Opencode CLI available: {info}")
    
    def run_single(
        self,
        prompt: str,
        working_dir: Optional[str] = None,
        context: Optional[Dict] = None,
        timeout: int = 300
    ) -> CLIResult:
        """단일 작업 실행"""
        task = CLITask(
            task_id=f"opencode_{int(time.time())}",
            cli_name="opencode",
            prompt=prompt,
            working_dir=working_dir,
            context=context,
            model=self.default_model,
            timeout=timeout
        )
        return self.runner.run_sync([task])[0]
    
    def run_batch(
        self,
        prompts: List[str],
        working_dir: Optional[str] = None,
        max_concurrent: int = 3
    ) -> List[CLIResult]:
        """배치 작업 실행"""
        tasks = [
            CLITask(
                task_id=f"opencode_{i}_{int(time.time())}",
                cli_name="opencode",
                prompt=prompt,
                working_dir=working_dir,
                model=self.default_model,
                priority=i
            )
            for i, prompt in enumerate(prompts)
        ]
        
        runner = AsyncCLIRunner(max_concurrent=max_concurrent)
        return runner.run_sync(tasks)
    
    def run_with_progress(
        self,
        prompt: str,
        progress_callback: Callable[[str, float], None],
        working_dir: Optional[str] = None
    ) -> CLIResult:
        """진행 상황 콜백과 함께 실행"""
        def adapter(task_id: str, status: str, progress: float):
            progress_callback(status, progress)
        
        self.runner.on_progress(adapter)
        
        task = CLITask(
            task_id=f"opencode_progress_{int(time.time())}",
            cli_name="opencode",
            prompt=prompt,
            working_dir=working_dir
        )
        
        return self.runner.run_sync([task])[0]


# Integration with staged_delegate
def run_opencode_staged(
    stage: str,
    prompt: str,
    context: Dict[str, Any],
    progress_callback: Optional[Callable] = None
) -> Dict[str, Any]:
    """
    staged_delegate에서 사용하는 Opencode 실행
    
    Args:
        stage: 현재 stage (plan, exec, verify, fix)
        prompt: 작업 프롬프트
        context: 컨텍스트 데이터
        progress_callback: 진행 상황 콜백 (선택적)
    
    Returns:
        결과 dict
    """
    runner = OpencodeRunner()
    
    if progress_callback:
        result = runner.run_with_progress(prompt, progress_callback, context.get("working_dir"))
    else:
        result = runner.run_single(prompt, context.get("working_dir"), context)
    
    return {
        "success": result.success,
        "output": result.output,
        "duration_seconds": result.duration_seconds,
        "stage": stage,
        "cli": "opencode"
    }


if __name__ == "__main__":
    # Test
    print("Testing AsyncCLIRunner...")
    
    # Test single opencode (if available)
    runner = OpencodeRunner()
    
    def print_progress(status: str, progress: float):
        print(f"  [{status}] {progress:.0%}")
    
    result = runner.run_with_progress(
        "Say hello and confirm you're working",
        print_progress
    )
    
    print(f"Result: success={result.success}, duration={result.duration_seconds}s")
    print(f"Output preview: {result.output[:100]}...")
    
    print("\n✅ AsyncCLIRunner test complete")
