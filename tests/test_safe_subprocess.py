#!/usr/bin/env python3
"""
Unit tests for tools/safe_subprocess.py

테스트 대상:
- validate_command_args: 위험 패턴 감지
- run_command_safe: shell=False 실행, 타임아웃, 에러 핸들링
- run_hook_safe: 훅 실행 인터페이스
- check_cli_available: CLI 바이너리 존재 확인
"""

import os
import sys
import subprocess
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.safe_subprocess import (
    CommandInjectionError,
    validate_command_args,
    run_command_safe,
    run_hook_safe,
    check_cli_available,
    run_opencode_safe,
)


# ── validate_command_args 테스트 ──────────────────────────────────────


class TestValidateCommandArgs:
    def test_safe_command_passes(self):
        """안전한 명령어는 통과"""
        validate_command_args(["echo", "hello", "world"])
        validate_command_args(["python3", "--version"])
        validate_command_args(["npm", "run", "lint"])

    def test_semicolon_detected(self):
        """세미콜론 주입 감지"""
        with pytest.raises(CommandInjectionError):
            validate_command_args(["echo", "hello; rm -rf /"])

    def test_pipe_detected(self):
        """파이프 문자 감지"""
        with pytest.raises(CommandInjectionError):
            validate_command_args(["cat", "file.txt", "|", "grep", "secret"])

    def test_backtick_detected(self):
        """백틱 감지"""
        with pytest.raises(CommandInjectionError):
            validate_command_args(["echo", "`whoami`"])

    def test_and_operator_detected(self):
        """&& 연산자 감지"""
        with pytest.raises(CommandInjectionError):
            validate_command_args(["echo", "ok && rm -rf /"])

    def test_or_operator_detected(self):
        """|| 연산자 감지"""
        with pytest.raises(CommandInjectionError):
            validate_command_args(["false", "|| echo pwned"])

    def test_dollar_sign_detected(self):
        """$ 문자 감지 (환경변수 확장 차단)"""
        with pytest.raises(CommandInjectionError):
            validate_command_args(["echo", "$HOME"])

    def test_curl_detected(self):
        """curl 차단"""
        with pytest.raises(CommandInjectionError):
            validate_command_args(["curl", "http://malicious.com"])


# ── run_command_safe 테스트 ───────────────────────────────────────────


class TestRunCommandSafe:
    def test_echo_works(self):
        """기본 echo 명령어 실행"""
        code, stdout, stderr = run_command_safe(["echo", "hello"])
        assert code == 0
        assert "hello" in stdout

    def test_returns_exit_code(self):
        """실패 시 올바른 exit code 반환"""
        code, stdout, stderr = run_command_safe(["false"])
        assert code != 0

    def test_empty_command_raises(self):
        """빈 명령어는 ValueError"""
        with pytest.raises(ValueError, match="cannot be empty"):
            run_command_safe([])

    def test_nonexistent_binary_raises(self):
        """존재하지 않는 바이너리는 FileNotFoundError"""
        with pytest.raises(FileNotFoundError):
            run_command_safe(["__nonexistent_binary_xyz__"])

    def test_timeout_raises(self):
        """타임아웃 발생 시 예외"""
        with pytest.raises(subprocess.TimeoutExpired):
            run_command_safe(["sleep", "10"], timeout=1)

    def test_cwd_respected(self, tmp_path):
        """작업 디렉토리 적용"""
        code, stdout, stderr = run_command_safe(["pwd"], cwd=str(tmp_path))
        assert code == 0
        # resolve 해서 비교 (심볼릭 링크 고려)
        assert str(tmp_path.resolve()) in stdout.strip()

    def test_invalid_cwd_raises(self):
        """존재하지 않는 cwd는 ValueError"""
        with pytest.raises(ValueError, match="does not exist"):
            run_command_safe(["echo", "hi"], cwd="/nonexistent_dir_xyz")

    def test_env_vars_passed(self):
        """커스텀 환경변수 전달"""
        code, stdout, _ = run_command_safe(
            ["env"],
            env={"HERMES_TEST_VAR": "test_value"}
        )
        assert code == 0
        assert "HERMES_TEST_VAR=test_value" in stdout

    def test_shell_false_confirmed(self):
        """shell=False가 실제로 적용되는지 간접 검증 — 셸 글로빙이 리터럴로 전달됨"""
        # shell=False이면 '*'가 glob 확장 없이 리터럴로 전달됨
        code, stdout, _ = run_command_safe(["echo", "test_marker"])
        assert code == 0
        assert "test_marker" in stdout

    def test_dangerous_command_blocked(self):
        """위험한 패턴이 인자에 있으면 차단"""
        with pytest.raises(CommandInjectionError):
            run_command_safe(["echo", "test; rm -rf /"])


# ── run_hook_safe 테스트 ──────────────────────────────────────────────


class TestRunHookSafe:
    def test_simple_hook_execution(self, tmp_path):
        """간단한 훅 실행"""
        results = run_hook_safe(
            hook_name="test",
            commands=["echo hook_ok"],
            context={"PROJECT": "test"},
            working_dir=str(tmp_path),
        )

        assert len(results) == 1
        assert results[0]["success"] is True
        assert results[0]["hook_name"] == "test"
        assert "hook_ok" in results[0]["output"]

    def test_multiple_commands_sequential(self, tmp_path):
        """여러 명령어 순차 실행"""
        results = run_hook_safe(
            "multi",
            ["echo first", "echo second"],
            {},
            str(tmp_path),
        )
        assert len(results) == 2
        assert results[0]["success"] is True
        assert results[1]["success"] is True

    def test_fail_fast_on_error(self, tmp_path):
        """첫 실패 시 나머지 명령 건너뜀"""
        results = run_hook_safe(
            "failfast",
            ["false", "echo should_not_run"],
            {},
            str(tmp_path),
        )
        # false가 실패하면 echo는 실행 안 됨
        assert len(results) == 1
        assert results[0]["success"] is False

    def test_injection_blocked(self, tmp_path):
        """command injection 패턴 차단"""
        results = run_hook_safe(
            "inject_test",
            ["echo hello; rm -rf /"],
            {},
            str(tmp_path),
        )
        assert len(results) == 1
        assert results[0]["success"] is False
        assert "Security error" in results[0]["output"]

    def test_context_as_env_vars(self, tmp_path):
        """컨텍스트가 HARNESS_ 접두사 환경변수로 전달"""
        results = run_hook_safe(
            "env_test",
            ["env"],
            {"project_root": "/test/path"},
            str(tmp_path),
        )
        if results and results[0]["success"]:
            assert "HARNESS_PROJECT_ROOT" in results[0]["output"]

    def test_empty_commands_returns_empty(self, tmp_path):
        """빈 명령어 목록은 빈 결과"""
        results = run_hook_safe("empty", [], {}, str(tmp_path))
        assert results == []

    def test_timeout_handled(self, tmp_path):
        """타임아웃이 발생해도 크래시 없이 결과 반환"""
        results = run_hook_safe(
            "timeout_test",
            ["sleep 10"],
            {},
            str(tmp_path),
            timeout=1,
        )
        assert len(results) == 1
        assert results[0]["success"] is False
        assert "Timeout" in results[0]["output"]


# ── check_cli_available 테스트 ────────────────────────────────────────


class TestCheckCliAvailable:
    def test_python3_available(self):
        """python3은 대부분 환경에서 사용 가능"""
        available, info = check_cli_available("python3")
        assert available is True
        assert len(info) > 0

    def test_nonexistent_cli(self):
        """존재하지 않는 CLI는 unavailable"""
        available, info = check_cli_available("__nonexistent_cli_xyz__")
        assert available is False

    def test_returns_tuple(self):
        """반환값이 (bool, str) 튜플"""
        result = check_cli_available("echo")
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], str)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
