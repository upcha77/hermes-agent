#!/usr/bin/env python3
"""
Safe Subprocess Execution

보안 취약점 해결:
- shell=True 제거 (command injection 방지)
- Input validation 추가
- Timeout and resource limits
"""

import logging
import os
import shlex
import subprocess
from typing import List, Optional, Tuple, Dict, Any
from pathlib import Path

logger = logging.getLogger(__name__)


class CommandInjectionError(Exception):
    """잠재적 command injection 감지"""
    pass


def validate_command_args(args: List[str]) -> None:
    """
    명령어 인자 검증
    
    위험한 문자/패턴 검출
    """
    dangerous_patterns = [
        ';', '&&', '||', '|', '`', '$',  # Shell metacharacters
        'rm -rf', '> /', '>> /',         # Dangerous commands
        'curl', 'wget',                  # Download (if unexpected)
    ]
    
    for arg in args:
        for pattern in dangerous_patterns:
            if pattern in arg:
                raise CommandInjectionError(
                    f"Potentially dangerous pattern detected: {pattern!r} in {arg!r}"
                )


def run_command_safe(
    command: List[str],
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: Optional[int] = 300,
    capture_output: bool = True,
    input_data: Optional[str] = None
) -> Tuple[int, str, str]:
    """
    안전한 명령어 실행
    
    Args:
        command: 명령어와 인자 리스트 (shell=False)
        cwd: 작업 디렉토리
        env: 환경변수
        timeout: 타임아웃 (초)
        capture_output: 출력 캡처 여부
        input_data: stdin 입력
    
    Returns:
        (exit_code, stdout, stderr)
    
    Raises:
        CommandInjectionError: 위험한 패턴 감지
        subprocess.TimeoutExpired: 타임아웃
    """
    # Validate inputs
    if not command:
        raise ValueError("Command cannot be empty")
    
    validate_command_args(command)
    
    # Validate working directory
    if cwd:
        cwd_path = Path(cwd).resolve()
        if not cwd_path.exists():
            raise ValueError(f"Working directory does not exist: {cwd}")
        cwd = str(cwd_path)
    
    # Prepare environment
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    
    logger.debug(f"Running command: {' '.join(command)}")
    
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=run_env,
            timeout=timeout,
            capture_output=capture_output,
            text=True,
            shell=False,  # SECURITY: Never use shell=True
            input=input_data
        )
        
        return result.returncode, result.stdout, result.stderr
        
    except subprocess.TimeoutExpired as e:
        logger.error(f"Command timed out after {timeout}s: {' '.join(command)}")
        raise
    except FileNotFoundError:
        logger.error(f"Command not found: {command[0]}")
        raise


def run_hook_safe(
    hook_name: str,
    commands: List[str],
    context: Dict[str, Any],
    working_dir: Optional[str] = None,
    timeout: int = 300
) -> List[Dict[str, Any]]:
    """
    Harness hook 실행 (안전한 버전)
    
    Args:
        hook_name: 훅 이름
        commands: 명령어 문자열 리스트
        context: 컨텍스트 데이터 (환경변수로 전달)
        working_dir: 작업 디렉토리
        timeout: 타임아웃
    
    Returns:
        훅 실행 결과 리스트
    """
    import time
    
    results = []
    
    # Prepare environment
    env = {}
    for key, value in context.items():
        env[f'HARNESS_{key.upper()}'] = str(value)[:1000]  # Limit env var size
    
    for command_str in commands:
        start_time = time.time()
        
        try:
            # Parse command safely (shlex.split handles quoting)
            command_list = shlex.split(command_str)
            
            exit_code, stdout, stderr = run_command_safe(
                command=command_list,
                cwd=working_dir,
                env=env,
                timeout=timeout
            )
            
            results.append({
                'success': exit_code == 0,
                'hook_name': hook_name,
                'command': command_str,
                'exit_code': exit_code,
                'output': stdout + stderr,
                'duration_ms': int((time.time() - start_time) * 1000)
            })
            
            # Fail-fast
            if exit_code != 0:
                break
                
        except CommandInjectionError as e:
            results.append({
                'success': False,
                'hook_name': hook_name,
                'command': command_str,
                'exit_code': -1,
                'output': f"Security error: {e}",
                'duration_ms': 0
            })
            break
            
        except subprocess.TimeoutExpired:
            results.append({
                'success': False,
                'hook_name': hook_name,
                'command': command_str,
                'exit_code': -1,
                'output': f"Timeout after {timeout}s",
                'duration_ms': timeout * 1000
            })
            break
            
        except Exception as e:
            results.append({
                'success': False,
                'hook_name': hook_name,
                'command': command_str,
                'exit_code': -1,
                'output': str(e),
                'duration_ms': int((time.time() - start_time) * 1000)
            })
            break
    
    return results


def check_cli_available(cli_name: str) -> Tuple[bool, str]:
    """
    CLI 도구 사용 가능 여부 확인
    
    Returns:
        (available, version_or_error)
    """
    try:
        exit_code, stdout, stderr = run_command_safe(
            [cli_name, '--version'],
            timeout=5
        )
        
        if exit_code == 0:
            return True, stdout.strip() or stderr.strip()
        else:
            return False, f"Exit code: {exit_code}"
            
    except FileNotFoundError:
        return False, "Command not found"
    except Exception as e:
        return False, str(e)


# Opencode-specific helpers
def run_opencode_safe(
    prompt: str,
    cwd: Optional[str] = None,
    context_file: Optional[str] = None,
    timeout: int = 300,
    model: Optional[str] = None
) -> Tuple[int, str, str]:
    """
    Opencode CLI 안전하게 실행
    
    Args:
        prompt: 실행할 프롬프트
        cwd: 작업 디렉토리
        context_file: 컨텍스트 파일 경로
        timeout: 타임아웃
        model: 특정 모델 지정 (선택적)
    
    Returns:
        (exit_code, stdout, stderr)
    """
    # Build command safely
    command = ['opencode', 'run', prompt]
    
    if model:
        command.extend(['--model', model])
    
    if context_file:
        command.extend(['--file', context_file])
    
    # Add format json for machine-readable output
    command.extend(['--format', 'json'])
    
    return run_command_safe(command, cwd=cwd, timeout=timeout)


def run_codex_safe(
    prompt: str,
    cwd: Optional[str] = None,
    timeout: int = 300
) -> Tuple[int, str, str]:
    """
    Codex CLI 안전하게 실행
    """
    command = ['codex', '--approval-mode', 'auto-edit', '--message', prompt]
    return run_command_safe(command, cwd=cwd, timeout=timeout)


if __name__ == "__main__":
    # Test
    print("Testing safe_subprocess...")
    
    # Test 1: Safe command
    try:
        code, out, err = run_command_safe(['echo', 'hello world'])
        print(f"✓ Safe command: exit={code}, out={out.strip()}")
    except Exception as e:
        print(f"✗ Safe command failed: {e}")
    
    # Test 2: Command validation
    try:
        validate_command_args(['echo', 'hello; rm -rf /'])
        print("✗ Should have detected dangerous pattern")
    except CommandInjectionError as e:
        print(f"✓ Detected dangerous pattern: {e}")
    
    # Test 3: CLI availability
    for cli in ['opencode', 'codex', 'python3']:
        available, info = check_cli_available(cli)
        print(f"{'✓' if available else '✗'} {cli}: {info[:50]}")
    
    print("\n✅ safe_subprocess test complete")
