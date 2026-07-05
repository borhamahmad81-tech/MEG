@echo off
REM Launches Chrome with a remote debugging port so the Assistant can attach.
REM A separate temporary profile is used so it never touches your normal
REM Chrome profile/bookmarks/passwords.
start "" "chrome.exe" --remote-debugging-port=9222 --user-data-dir="%USERPROFILE%\.safety_assistant_chrome_profile"
