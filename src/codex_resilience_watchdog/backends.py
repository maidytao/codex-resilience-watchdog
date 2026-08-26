from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
from typing import Any, Callable


@dataclass(frozen=True)
class CapabilityReport:
    compatible: bool
    version: str | None
    reason: str


@dataclass(frozen=True)
class BackendResult:
    success: bool
    returncode: int
    reason: str
    stdout: str = ""
    stderr: str = ""


Runner = Callable[..., Any]


class CodexCliBackend:
    def __init__(
        self,
        codex_executable: str = "codex",
        *,
        runner: Runner = subprocess.run,
        timeout_seconds: int = 900,
    ) -> None:
        self.codex_executable = codex_executable
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def capabilities(self) -> CapabilityReport:
        executable = self.codex_executable
        if Path(executable).is_absolute():
            if not Path(executable).is_file():
                return CapabilityReport(False, None, "codex executable is missing")
        elif shutil.which(executable) is None:
            return CapabilityReport(False, None, "codex executable is not on PATH")

        version_result = self._run([executable, "--version"], timeout=30)
        resume_result = self._run(
            [executable, "exec", "resume", "--help"], timeout=30
        )
        version = version_result.stdout.strip() if version_result.returncode == 0 else None
        compatible = resume_result.returncode == 0 and "SESSION_ID" in resume_result.stdout
        return CapabilityReport(
            compatible=compatible,
            version=version,
            reason="ok" if compatible else "codex exec resume is unavailable",
        )

    def resume_read_only(self, session_id: str, prompt: str) -> BackendResult:
        if not session_id:
            return BackendResult(False, 2, "session id is required")
        args = [
            self.codex_executable,
            "-s",
            "read-only",
            "-a",
            "never",
            "exec",
            "resume",
            session_id,
            prompt,
            "--json",
        ]
        completed = self._run(args, timeout=self.timeout_seconds)
        return BackendResult(
            success=completed.returncode == 0,
            returncode=completed.returncode,
            reason="resumed" if completed.returncode == 0 else "codex resume failed",
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def _run(self, args: list[str], *, timeout: int):
        try:
            return self.runner(
                args,
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return subprocess.CompletedProcess(args, 1, "", str(error))


class CodexProcessBackend:
    def __init__(
        self,
        *,
        runner: Runner = subprocess.run,
        launcher: Runner = subprocess.Popen,
        codex_launcher: str = "codex",
    ) -> None:
        self.runner = runner
        self.launcher = launcher
        self.codex_launcher = codex_launcher

    def verified_executable(self) -> Path | None:
        script = (
            "$current=[Security.Principal.WindowsIdentity]::GetCurrent().Name;"
            "$paths=@();"
            "Get-Process | Where-Object { $_.MainWindowHandle -ne 0 -and $_.Path -and "
            "(($_.ProcessName -match '^Codex') -or "
            "($_.ProcessName -eq 'ChatGPT' -and "
            "$_.Path -match '\\\\WindowsApps\\\\OpenAI\\.Codex_[^\\\\]+\\\\app\\\\ChatGPT\\.exe$')) "
            "} | ForEach-Object {"
            "$cim=Get-CimInstance Win32_Process -Filter ('ProcessId={0}' -f $_.Id);"
            "$owner=Invoke-CimMethod -InputObject $cim -MethodName GetOwner;"
            "$identity=('{0}\\{1}' -f $owner.Domain,$owner.User);"
            "if($identity -eq $current){$paths += [IO.Path]::GetFullPath($_.Path)}};"
            "$unique=@($paths | Sort-Object -Unique);"
            "if($unique.Count -ne 1){exit 4};"
            "Write-Output $unique[0]"
        )
        completed = self._powershell(script)
        if completed.returncode != 0:
            return None
        paths = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        if len(set(paths)) != 1:
            return None
        candidate = Path(paths[0])
        name = candidate.name.lower()
        if not candidate.is_absolute() or not (
            name.startswith("codex") or name == "chatgpt.exe"
        ):
            return None
        return candidate

    def is_unresponsive(self, expected_executable: Path | None) -> bool:
        expected = expected_executable or self.verified_executable()
        if expected is None or not expected.is_absolute():
            return False
        script = (
            "$expected=[IO.Path]::GetFullPath($args[0]);"
            "$windows=@(Get-Process | Where-Object { $_.Path -and "
            "([IO.Path]::GetFullPath($_.Path) -eq $expected) -and "
            "$_.ProcessName -match '^(Codex|ChatGPT)' -and $_.MainWindowHandle -ne 0 });"
            "if($windows.Count -eq 0){exit 4};"
            "if(@($windows | Where-Object { -not $_.Responding }).Count -gt 0){"
            "Write-Output 'true'}else{Write-Output 'false'}"
        )
        completed = self._powershell(script, str(expected.resolve()))
        return completed.returncode == 0 and completed.stdout.strip().lower() == "true"

    def restart_once(self, expected_executable: Path | None) -> BackendResult:
        if expected_executable is None or not expected_executable.is_absolute():
            return BackendResult(False, 2, "verified Codex executable path is required")
        expected = str(expected_executable.resolve())
        script = (
            "$expected=[IO.Path]::GetFullPath($args[0]);"
            "$current=[Security.Principal.WindowsIdentity]::GetCurrent().Name;"
            "$matches=@();"
            "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and "
            "([IO.Path]::GetFullPath($_.ExecutablePath) -eq $expected) -and "
            "$_.Name -match '^(Codex|ChatGPT)' } | ForEach-Object {"
            "$owner=Invoke-CimMethod -InputObject $_ -MethodName GetOwner;"
            "$identity=('{0}\\{1}' -f $owner.Domain,$owner.User);"
            "if($identity -eq $current){$matches += $_}};"
            "if($matches.Count -eq 0){exit 4};"
            "$matches | ForEach-Object { Stop-Process -Id $_.ProcessId -Force };"
            "exit 0"
        )
        try:
            stopped = self._powershell(script, expected)
            if stopped.returncode != 0:
                return BackendResult(
                    False,
                    stopped.returncode,
                    "verified Codex process was not restarted",
                    stopped.stdout,
                    stopped.stderr,
                )
            self.launcher(
                [self.codex_launcher, "app"],
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return BackendResult(True, 0, "restarted")
        except (OSError, subprocess.SubprocessError) as error:
            return BackendResult(False, 1, f"restart failed: {error}")

    def _powershell(self, script: str, *arguments: str):
        try:
            return self.runner(
                ["powershell", "-NoProfile", "-Command", script, *arguments],
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return subprocess.CompletedProcess([], 1, "", str(error))
