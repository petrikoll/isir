from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import ctypes
from pathlib import Path


APP_NAME = "ISIR Kontrola"
INSTALL_DIR_NAME = "ISIR-Kontrola"
EXE_NAME = "ISIR-Kontrola.exe"
FILES_TO_COPY = [
    EXE_NAME,
    "uninstall.cmd",
    "reset_data.cmd",
    "README-installer.txt",
]

MB_OK = 0x00000000
MB_YESNO = 0x00000004
MB_ICONINFORMATION = 0x00000040
MB_ICONQUESTION = 0x00000020
MB_ICONERROR = 0x00000010
MB_SYSTEMMODAL = 0x00001000
MB_TOPMOST = 0x00040000
IDYES = 6


def payload_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "payload"  # type: ignore[attr-defined]
    return Path(__file__).resolve().parents[1] / "installer_build"


def install_dir() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / INSTALL_DIR_NAME


def log_line(target_dir: Path, message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with (target_dir / "install.log").open("a", encoding="utf-8") as log_file:
        log_file.write(f"[{timestamp}] {message}\n")


def show_message(text: str, flags: int) -> int:
    return int(ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, flags | MB_TOPMOST | MB_SYSTEMMODAL))


def run_powershell(script: str) -> None:
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def create_shortcuts(target_dir: Path) -> None:
    script = rf"""
$ws=New-Object -ComObject WScript.Shell
$target='{target_dir / EXE_NAME}'
$workdir='{target_dir}'
$desktop=[Environment]::GetFolderPath('Desktop')
$startMenu=Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$lnk=$ws.CreateShortcut((Join-Path $desktop 'ISIR Kontrola.lnk'))
$lnk.TargetPath=$target
$lnk.WorkingDirectory=$workdir
$lnk.Save()
$lnk2=$ws.CreateShortcut((Join-Path $startMenu 'ISIR Kontrola.lnk'))
$lnk2.TargetPath=$target
$lnk2.WorkingDirectory=$workdir
$lnk2.Save()
$uninstall='{target_dir / "uninstall.cmd"}'
$lnk3=$ws.CreateShortcut((Join-Path $startMenu 'Odinstalovat ISIR Kontrola.lnk'))
$lnk3.TargetPath=$env:ComSpec
$lnk3.Arguments='/c ""' + $uninstall + '""'
$lnk3.WorkingDirectory=$env:TEMP
$lnk3.Save()
$reset='{target_dir / "reset_data.cmd"}'
$lnk4=$ws.CreateShortcut((Join-Path $startMenu 'Vymazat data ISIR Kontrola.lnk'))
$lnk4.TargetPath=$env:ComSpec
$lnk4.Arguments='/c ""' + $reset + '""'
$lnk4.WorkingDirectory=$env:TEMP
$lnk4.Save()
"""
    run_powershell(script)


def set_startup_shortcut(target_dir: Path, enabled: bool) -> None:
    script = rf"""
$startup=[Environment]::GetFolderPath('Startup')
$link=Join-Path $startup 'ISIR Kontrola.lnk'
if ({'$true' if enabled else '$false'}) {{
  $ws=New-Object -ComObject WScript.Shell
  $lnk=$ws.CreateShortcut($link)
  $lnk.TargetPath='{target_dir / EXE_NAME}'
  $lnk.WorkingDirectory='{target_dir}'
  $lnk.Save()
}} else {{
  Remove-Item -LiteralPath $link -Force -ErrorAction SilentlyContinue
}}
"""
    run_powershell(script)


def copy_payload(source_dir: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["taskkill", "/IM", EXE_NAME, "/F"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for file_name in FILES_TO_COPY:
        source = source_dir / file_name
        target_name = "README.txt" if file_name == "README-installer.txt" else file_name
        shutil.copy2(source, target_dir / target_name)


def main() -> int:
    source_dir = payload_dir()
    target_dir = install_dir()

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        log_line(target_dir, "Start instalace")
        log_line(target_dir, f"Payload: {source_dir}")
        copy_payload(source_dir, target_dir)
        log_line(target_dir, "Soubory zkopirovany")
        create_shortcuts(target_dir)
        log_line(target_dir, "Zastupci vytvoreni")

        startup = show_message(
            "Spoustet ISIR Kontrola automaticky po startu Windows?",
            MB_YESNO | MB_ICONQUESTION,
        ) == IDYES
        set_startup_shortcut(target_dir, startup)
        log_line(target_dir, f"Automaticke spousteni: {'Ano' if startup else 'Ne'}")

        subprocess.Popen([str(target_dir / EXE_NAME)], cwd=str(target_dir), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log_line(target_dir, "Aplikace spustena")
        show_message("Instalace byla dokoncena. Aplikace se spousti v prohlizeci.", MB_OK | MB_ICONINFORMATION)
        return 0
    except Exception as exc:
        try:
            log_line(target_dir, f"Chyba instalace: {exc}")
        except Exception:
            pass
        show_message(f"Instalace selhala:\n{exc}", MB_OK | MB_ICONERROR)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
