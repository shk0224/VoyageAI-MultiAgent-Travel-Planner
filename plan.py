"""Run the planner from the terminal with your real API keys.

    python plan.py
    python plan.py "Plan a 4-day trip from Atlanta to New York starting December 10, 2026, 2 adults, budget $1500"

Follow-up messages in the same session continue the same trip
(e.g. "make it nonstop only"). Type 'json' to see the raw data behind
the last answer, 'new' to start a new trip, 'exit' to quit.
"""
import json
import sys
import uuid

from backend import run_travel_agent


def main():
    thread = f"cli_{uuid.uuid4().hex[:8]}"
    last = None
    first = " ".join(sys.argv[1:]).strip()
    while True:
        message = first or input("\nYou: ").strip()
        first = ""
        if message.lower() in ("exit", "quit"):
            break
        if message.lower() == "new":
            thread = f"cli_{uuid.uuid4().hex[:8]}"
            print("Started a new trip.")
            continue
        if message.lower() == "json" and last:
            print(json.dumps({k: v for k, v in last.items() if k != "answer"}, indent=2, default=str))
            continue
        if not message:
            continue
        last = run_travel_agent(message, thread_id=thread)
        print("\n" + last["answer"])
        print(f"\n[LLM calls: {last['llm_calls']} | thread: {thread}]")


if __name__ == "__main__":
    main()
