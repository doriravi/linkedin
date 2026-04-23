"""
Example: Claude uses Brave Search as a tool to answer prompts.
Usage: python brave_search_example.py "What is the latest news about AI?"
"""

import json
import sys
import os
import requests
import anthropic
from dotenv import load_dotenv

load_dotenv()

BRAVE_API_KEY = "BSAc9AqayObhCi0YkjwyVfEzeyUOBSN"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Tool definition ───────────────────────────────────────────────────────────

tools = [
    {
        "name": "brave_search",
        "description": "Search the web using Brave Search to find current information.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query"
                },
                "count": {
                    "type": "integer",
                    "description": "Number of results to return (1-10, default 5)",
                    "default": 5
                }
            },
            "required": ["query"]
        }
    }
]


def brave_search(query: str, count: int = 5) -> dict:
    """Call the Brave Search API and return results."""
    resp = requests.get(
        "https://api.search.brave.com/res/v1/web/search",
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": BRAVE_API_KEY,
        },
        params={"q": query, "count": count},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    results = []
    for r in data.get("web", {}).get("results", []):
        results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "description": r.get("description", ""),
        })
    return {"results": results}


# ── Agentic loop ──────────────────────────────────────────────────────────────

def ask(prompt: str) -> str:
    messages = [{"role": "user", "content": prompt}]

    while True:
        response = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=1024,
            tools=tools,
            messages=messages,
        )

        # Append assistant turn
        messages.append({"role": "assistant", "content": response.content})

        # Done — return text
        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text
            return ""

        # Execute tool calls
        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    print(f"[tool] {block.name}({block.input})", flush=True)
                    if block.name == "brave_search":
                        result = brave_search(**block.input)
                    else:
                        result = {"error": f"Unknown tool: {block.name}"}

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    })

            messages.append({"role": "user", "content": tool_results})


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    prompt = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "What are the top AI news stories today?"
    print(f"Prompt: {prompt}\n")
    answer = ask(prompt)
    print(f"\nAnswer:\n{answer}".encode("utf-8", errors="replace").decode("utf-8"))
