"""
main.py
--------
Entry point for the Clinical Safety Event Assistant.
Run with:  python main.py
Or use the packaged EXE built by the GitHub Actions workflow.
"""

from gui_app import SafetyEventAssistantApp

if __name__ == "__main__":
    app = SafetyEventAssistantApp()
    app.mainloop()
