import json
import os
import subprocess
import sys

import requests

OPENROUTER_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"  # free-tier model; catalog rotates — check openrouter.ai/models if this 404s
MIN_SCORE = 0.55
MAX_FILES_AS_CONTEXT = 12
MAX_FILE_CHARS = 6000
MAX_FILES_TO_WRITE = 20


def load_context_files(context_path: str) -> str:
    with open(context_path) as f:
        result = json.load(f)

    selected = [f for f in result.get("files", []) if f["score"] >= MIN_SCORE][:MAX_FILES_AS_CONTEXT]

    blocks = []
    for entry in selected:
        path = entry["path"]
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                content = fh.read()[:MAX_FILE_CHARS]
        except OSError:
            continue
        blocks.append(f"--- {path} ---\n{content}")

    return "\n\n".join(blocks)


def is_safe_path(path: str) -> bool:
    if not path or path.startswith("/") or ".." in path.split("/"):
        return False
    if path.startswith(".github/workflows/"):
        return False
    return True


def generate_fix(issue_title: str, issue_body: str, context: str, api_key: str) -> dict:
    system_prompt = (
        "You are a coding agent. Given a GitHub issue and relevant repo context, produce a "
        "fix. Respond with ONLY a JSON object, no markdown fences, no prose outside the JSON: "
        '{"summary": "one-line description of the fix", '
        '"files": [{"path": "relative/path.py", "content": "full new file content"}]}. '
        "Only include files that need to change. Always output the FULL new content of each "
        "file, not a diff. Never touch files under .github/workflows/."
    )
    user_prompt = f"Issue: {issue_title}\n\n{issue_body}\n\nRelevant repo context:\n{context}"

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": OPENROUTER_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
        },
        timeout=180,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()
    content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(content, strict=False)


def write_output(name: str, value: str) -> None:
    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        f.write(f"{name}<<VIBECODE_EOF\n{value}\nVIBECODE_EOF\n")


def main() -> int:
    issue_number = os.environ["ISSUE_NUMBER"]
    issue_title = os.environ["ISSUE_TITLE"]
    issue_body = os.environ.get("ISSUE_BODY") or ""
    api_key = os.environ["OPENROUTER_API_KEY"]
    context = load_context_files(os.environ["CONTEXT_FILE"])

    print(f"Context: {len(context)} chars from cognitive-cache selection")

    result = generate_fix(issue_title, issue_body, context, api_key)
    files = result.get("files", [])
    summary = result.get("summary", f"fix for issue #{issue_number}").replace("\n", " ")

    print(f"Model returned {len(files)} file(s): {[f.get('path') for f in files]}")
    print(f"Summary: {summary}")

    written = []
    for entry in files[:MAX_FILES_TO_WRITE]:
        path = entry.get("path", "")
        if not is_safe_path(path):
            print(f"Skipping unsafe path from model output: {path!r}")
            continue
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(entry["content"])
        written.append(path)

    if not written:
        print("No valid files to write, aborting.")
        return 1

    branch = f"vibecode/issue-{issue_number}"
    subprocess.run(["git", "checkout", "-b", branch], check=True)
    subprocess.run(["git", "config", "user.name", "vibecode-agent"], check=True)
    subprocess.run(["git", "config", "user.email", "vibecode-agent@users.noreply.github.com"], check=True)
    subprocess.run(["git", "add", *written], check=True)
    subprocess.run(["git", "commit", "-m", f"vibecode: {summary} (#{issue_number})"], check=True)

    write_output("branch", branch)
    write_output("summary", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
