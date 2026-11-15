"""Tests for command parsing, text strategy and (mocked) execution."""

from __future__ import annotations

import threading

import pytest

from hotstring import executor
from hotstring.executor import (
    CommandError,
    CommandExecutor,
    CommandType,
    PowerShellResult,
    build_powershell_arguments,
    choose_text_strategy,
    describe_command,
    format_result,
    parse_command,
    text_preview,
    validate_command,
)


@pytest.mark.parametrize(
    ("command", "command_type", "payload"),
    [
        ("tHello", CommandType.TEXT, "Hello"),
        ("t Hello", CommandType.TEXT, "Hello"),
        ("t: Hello", CommandType.TEXT, " Hello"),
        ("t", CommandType.TEXT, ""),
        ("sGet-Date", CommandType.POWERSHELL, "Get-Date"),
        ("s Start-Process notepad", CommandType.POWERSHELL, "Start-Process notepad"),
        ("s:dir", CommandType.POWERSHELL, "dir"),
        ("aopen", CommandType.ACTION, "open"),
        ("a open", CommandType.ACTION, "open"),
        ("a:open", CommandType.ACTION, "open"),
    ],
)
def test_parse_command(command: str, command_type: CommandType, payload: str) -> None:
    parsed = parse_command(command)
    assert parsed.type is command_type
    assert parsed.payload == payload
    assert parsed.prefix == command_type.value


def test_only_one_separator_is_stripped() -> None:
    assert parse_command("t:  hello").payload == "  hello"
    assert parse_command("t  hello").payload == " hello"
    assert parse_command("t::hello").payload == ":hello"


@pytest.mark.parametrize("command", ["", "xtext", " ", "?"])
def test_parse_command_rejects_unknown_prefixes(command: str) -> None:
    with pytest.raises(CommandError):
        parse_command(command)


def test_parse_command_requires_text() -> None:
    with pytest.raises(CommandError):
        parse_command(None)  # type: ignore[arg-type]


@pytest.mark.parametrize("command", ["t", "t ", "t:", "s", "s  ", "a", "a:", "a?", "anope"])
def test_validate_command_rejects_empty_or_unknown_payloads(command: str) -> None:
    with pytest.raises(CommandError):
        validate_command(command)


def test_internal_actions_are_case_insensitive_and_trimmed() -> None:
    for command in ("aopen", "a OPEN", "a:Open", "aopen "):
        parsed = validate_command(command)
        assert parsed.type is CommandType.ACTION
        assert parsed.payload.strip().lower() == "open"


def test_text_and_script_commands_are_validated() -> None:
    assert validate_command("tHello").payload == "Hello"
    assert validate_command("sWrite-Output hi").payload == "Write-Output hi"


def test_describe_command() -> None:
    assert describe_command("thello") == "Type text"
    assert describe_command("sGet-Date") == "Run PowerShell"
    assert describe_command("aopen") == "App action: open"


def test_text_preview_truncates() -> None:
    assert text_preview("short", 10) == "short"
    preview = text_preview("abcdef", 3)
    assert preview.startswith("abc")
    assert "3 more characters" in preview


def test_build_powershell_arguments_exact_flags() -> None:
    assert build_powershell_arguments("Get-Date") == [
        "powershell.exe",
        "-NoProfile",
        "-WindowStyle",
        "Hidden",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        "Get-Date",
    ]


def test_choose_text_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOTSTRING_TEXT_MODE", raising=False)

    assert choose_text_strategy("hello") == "type"
    assert choose_text_strategy("x" * 101) == "paste"
    assert choose_text_strategy("line one\nline two") == "paste"
    assert choose_text_strategy("tab\there") == "paste"
    assert choose_text_strategy("สวัสดี") == "paste"

    monkeypatch.setenv("HOTSTRING_TEXT_MODE", "paste")
    assert choose_text_strategy("hello") == "paste"
    monkeypatch.setenv("HOTSTRING_TEXT_MODE", "type")
    assert choose_text_strategy("สวัสดี") == "type"
    monkeypatch.setenv("HOTSTRING_TEXT_MODE", "nonsense")
    assert choose_text_strategy("hello") == "type"


def test_executor_types_text_with_injected_writer() -> None:
    written: list[str] = []
    executor_instance = CommandExecutor(text_writer=written.append)

    executor_instance.execute("tHello world")

    assert written == ["Hello world"]


def test_executor_strips_one_separator() -> None:
    written: list[str] = []
    executor_instance = CommandExecutor(text_writer=written.append)

    executor_instance.execute("t: Hello")

    assert written == [" Hello"]


def test_invalid_command_reports_error_and_does_not_execute() -> None:
    errors: list[str] = []
    actions: list[str] = []
    executor_instance = CommandExecutor(
        on_error=errors.append,
        on_action=actions.append,
        text_writer=lambda _text: pytest.fail("should not type"),
    )

    executor_instance.execute("ohno")
    executor_instance.execute("aunknown")
    executor_instance.execute("t")

    assert len(errors) == 3
    assert all("Invalid command" in message for message in errors)
    assert actions == []


def test_action_is_dispatched() -> None:
    actions: list[str] = []
    executor_instance = CommandExecutor(on_action=actions.append)

    executor_instance.execute("a:OPEN")

    assert actions == ["open"]


def test_action_without_handler_is_logged_not_raised() -> None:
    executor_instance = CommandExecutor()
    executor_instance.execute("aopen")  # no exception


def test_powershell_runs_on_a_worker_thread() -> None:
    finished = threading.Event()
    calls: list[str] = []

    def runner(script: str) -> PowerShellResult:
        calls.append(script)
        finished.set()
        return PowerShellResult(returncode=0, stdout="ok", stderr="")

    executor_instance = CommandExecutor(powershell_runner=runner)
    executor_instance.execute("sWrite-Output ok")

    assert finished.wait(2.0)
    assert calls == ["Write-Output ok"]


def test_powershell_failure_is_reported() -> None:
    reported: list[str] = []
    finished = threading.Event()

    def runner(_script: str) -> PowerShellResult:
        finished.set()
        return PowerShellResult(returncode=1, stdout="", stderr="boom")

    executor_instance = CommandExecutor(
        on_error=reported.append,
        powershell_runner=runner,
    )
    executor_instance.execute("sexit 1")

    assert finished.wait(2.0)
    for _ in range(100):
        if reported:
            break
        threading.Event().wait(0.01)
    assert any("exited with code 1" in message and "boom" in message for message in reported)


def test_powershell_crash_is_reported() -> None:
    reported: list[str] = []
    finished = threading.Event()

    def runner(_script: str) -> PowerShellResult:
        finished.set()
        raise OSError("powershell missing")

    executor_instance = CommandExecutor(
        on_error=reported.append,
        powershell_runner=runner,
    )
    executor_instance.execute("sGet-Date")

    assert finished.wait(2.0)
    for _ in range(100):
        if reported:
            break
        threading.Event().wait(0.01)
    assert any("failed to start" in message for message in reported)


def test_test_previews_text_without_typing() -> None:
    executor_instance = CommandExecutor(text_writer=lambda _text: pytest.fail("should not type"))

    result = executor_instance.test("tHello")

    assert "Preview" in result
    assert "5 characters" in result
    assert "Hello" in result


def test_test_validates_action_without_executing() -> None:
    actions: list[str] = []
    executor_instance = CommandExecutor(on_action=actions.append)

    result = executor_instance.test("aopen")

    assert "open" in result
    assert "Not executed" in result
    assert actions == []


def test_test_runs_powershell_through_runner() -> None:
    calls: list[str] = []

    def runner(script: str) -> PowerShellResult:
        calls.append(script)
        return PowerShellResult(returncode=0, stdout="hello\n", stderr="")

    result = CommandExecutor(powershell_runner=runner).test("sWrite-Output hello")

    assert calls == ["Write-Output hello"]
    assert "Exit code: 0" in result
    assert "hello" in result


def test_format_result_includes_all_sections() -> None:
    result = format_result(PowerShellResult(returncode=2, stdout="out", stderr="err"))

    assert "Exit code: 2" in result
    assert "Output:\nout" in result
    assert "Errors:\nerr" in result


def test_text_writer_receives_unicode_payload() -> None:
    written: list[str] = []
    executor_instance = CommandExecutor(text_writer=written.append)

    executor_instance.execute("tสวัสดี\nโลก")

    assert written == ["สวัสดี\nโลก"]


def test_describe_command_safe_handles_invalid_input() -> None:
    assert executor.describe_command_safe("?bad") == "Command"


def test_text_strategy_override_uses_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOTSTRING_TEXT_MODE", "type")
    text = "สวัสดี"
    assert executor.choose_text_strategy(text) == "type"