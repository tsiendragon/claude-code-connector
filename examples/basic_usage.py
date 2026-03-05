"""
basic_usage.py
--------------
Minimal example: create a Claude CLI session, send a message, print the response.

Prerequisites
-------------
1. tmux must be installed.
2. Claude Code CLI must be installed: npm install -g @anthropic-ai/claude-code
3. You must be authenticated: claude auth login

Run
---
    python examples/basic_usage.py
"""

from claude_cli_connector import ClaudeSession

def main() -> None:
    print("Starting Claude CLI session…")

    with ClaudeSession.create(name="demo", cwd=".") as session:
        print(f"Session ready: {session}")

        # First message
        response = session.send_and_wait("Hello! What can you help me with today?")
        print(f"\n[Claude]: {response}\n")

        # Follow-up
        response2 = session.send_and_wait(
            "In one sentence, what is the tmux send-keys command used for?"
        )
        print(f"\n[Claude]: {response2}\n")

    print("Session closed.")


if __name__ == "__main__":
    main()
