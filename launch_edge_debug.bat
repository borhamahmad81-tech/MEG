@echo off
REM Launches Edge with a remote debugging port so the Assistant can attach.
REM A separate temporary profile is used so it never touches your normal
REM Edge profile/bookmarks/passwords.
start "" "msedge.exe" --remote-debugging-port=9222 --user-data-dir="%USERPROFILE%\.safety_assistant_edge_profile"
