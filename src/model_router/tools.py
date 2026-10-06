"""Workspace tools exposed to the terminal coding agent."""

from __future__ import annotations

import json
import subprocess
import tempfile
import os
import signal
from pathlib import Path
from typing import Callable

OUTPUT_LIMIT = 24000


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required, "additionalProperties": False},
    }}


TOOLS = [
    _tool("list_files", "List files in a workspace directory (excluding hidden entries).",
          {"path": {"type": "string", "description": "Relative directory; defaults to ."}}, []),
    _tool("read_file", "Read a UTF-8 file in the workspace.",
          {"path": {"type": "string"}}, ["path"]),
    _tool("write_file", "Create or replace a UTF-8 workspace file. Read existing files first.",
          {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _tool("run_shell", "Run a shell command in the workspace, with a 120-second timeout.",
          {"command": {"type": "string"}}, ["command"]),
]


class WorkspaceTools:
    def __init__(self, root: Path, approve: Callable[[str], bool]):
        self.root = root.resolve()
        self.approve = approve

    def path(self, raw: str) -> Path:
        path = (self.root / raw).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("path must stay inside the workspace")
        if any(part in (".git", ".aws", ".codex", ".agents") for part in path.relative_to(self.root).parts):
            raise ValueError("protected workspace directory")
        return path

    def execute(self, name: str, arguments: str) -> str:
        try:
            args = json.loads(arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("tool arguments must be an object")
            if name == "list_files":
                directory = self.path(args.get("path", "."))
                result = "\n".join(p.name + ("/" if p.is_dir() else "")
                                   for p in sorted(directory.iterdir()) if not p.name.startswith("."))
            elif name == "read_file":
                with self.path(args["path"]).open(encoding="utf-8") as handle:
                    result = handle.read(OUTPUT_LIMIT + 1)
            elif name == "write_file":
                path = self.path(args["path"])
                content = args["content"]
                if not isinstance(content, str):
                    raise ValueError("content must be a string")
                label = str(path.relative_to(self.root))
                preview = content[:2000]
                if not self.approve(f"Write {label} ({len(content)} characters)?\n{preview}"):
                    return "Denied by user. Do not retry this action without new instructions."
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                result = f"Wrote {label}"
            elif name == "run_shell":
                command = args["command"]
                if not isinstance(command, str) or not command.strip():
                    raise ValueError("command must be a nonempty string")
                if not self.approve(f"Run shell command in {self.root}?\n{command}"):
                    return "Denied by user. Do not retry this action without new instructions."
                # File tools are workspace-bound; an approved shell command has normal OS access.
                with tempfile.TemporaryFile() as output:
                    process = subprocess.Popen(command, shell=True, cwd=self.root,
                                               stdout=output, stderr=subprocess.STDOUT,
                                               start_new_session=True)
                    try:
                        code = process.wait(timeout=120)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                        raise ValueError("shell command timed out after 120 seconds") from None
                    except BaseException:
                        if process.poll() is None:
                            os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                        raise
                    output.seek(0)
                    result = f"Exit code: {code}\n" + output.read(OUTPUT_LIMIT + 1).decode("utf-8", "replace")
            else:
                raise ValueError(f"unknown tool: {name}")
            return result[:OUTPUT_LIMIT] + ("\n[output truncated]" if len(result) > OUTPUT_LIMIT else "")
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return f"Error: {exc}"
