"""Workspace tools exposed to the terminal coding agent."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import signal
import fnmatch
from pathlib import Path
from typing import Callable

OUTPUT_LIMIT = 24000
MATCH_LIMIT = 200


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
    _tool("edit_file", "Replace an exact text snippet in a workspace file. Read the file "
          "first; the snippet must match exactly once unless replace_all is set.",
          {"path": {"type": "string"},
           "old": {"type": "string", "description": "Exact existing text to replace"},
           "new": {"type": "string"},
           "replace_all": {"type": "boolean", "description": "Replace every occurrence"}},
          ["path", "old", "new"]),
    _tool("find_files", "Find workspace files by glob pattern (e.g. '*.py', 'src/**/*.ts').",
          {"pattern": {"type": "string"}}, ["pattern"]),
    _tool("grep_files", "Search workspace file contents with a regular expression; returns "
          "'path:line:text' matches. Optionally restrict to a subdirectory and/or a "
          "filename glob.",
          {"pattern": {"type": "string"},
           "path": {"type": "string", "description": "Relative directory; defaults to ."},
           "glob": {"type": "string", "description": "Filename filter, e.g. '*.py'"}},
          ["pattern"]),
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

    def _workspace_files(self) -> list[Path]:
        """All regular files under the workspace, skipping hidden/protected dirs."""
        PROTECTED = {".git", ".aws", ".codex", ".agents"}
        files: list[Path] = []
        for directory, subdirs, names in os.walk(self.root, onerror=lambda _e: None):
            subdirs[:] = sorted(d for d in subdirs if not d.startswith(".") and d not in PROTECTED)
            for name in sorted(names):
                if name.startswith("."):
                    continue
                files.append(Path(directory) / name)
        return files

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
            elif name == "edit_file":
                path = self.path(args["path"])
                old, new = args["old"], args["new"]
                replace_all = bool(args.get("replace_all"))
                if not isinstance(old, str) or not isinstance(new, str) or not old:
                    raise ValueError("old and new must be nonempty strings")
                content = path.read_text(encoding="utf-8")
                occurrences = content.count(old)
                if not occurrences:
                    raise ValueError("old text not found in the file; re-read it and "
                                     "copy the snippet exactly")
                if occurrences > 1 and not replace_all:
                    raise ValueError(f"old text matches {occurrences} times; add more "
                                     "context to make it unique or set replace_all")
                label = str(path.relative_to(self.root))
                preview = f"old: {old[:400]!r}\nnew: {new[:400]!r}"
                if not self.approve(f"Edit {label} ({occurrences} replacement(s))?\n{preview}"):
                    return "Denied by user. Do not retry this action without new instructions."
                path.write_text(content.replace(old, new) if replace_all
                                else content.replace(old, new, 1), encoding="utf-8")
                result = f"Edited {label} ({occurrences} replacement(s))"
            elif name == "find_files":
                pattern = args["pattern"]
                if not isinstance(pattern, str) or not pattern.strip():
                    raise ValueError("pattern must be a nonempty glob")
                matches = []
                for file in self._workspace_files():
                    relative = file.relative_to(self.root).as_posix()
                    if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(file.name, pattern):
                        matches.append(relative)
                        if len(matches) >= MATCH_LIMIT:
                            break
                result = "\n".join(matches) or "no matches"
            elif name == "grep_files":
                regex_body = args["pattern"]
                if not isinstance(regex_body, str) or not regex_body.strip():
                    raise ValueError("pattern must be a nonempty regular expression")
                try:
                    regex = re.compile(regex_body)
                except re.error as exc:
                    raise ValueError(f"invalid regular expression: {exc}") from None
                base = self.path(args.get("path", "."))
                glob_filter = args.get("glob")
                matches = []
                for file in self._workspace_files():
                    if not file.is_relative_to(base):
                        continue
                    if glob_filter and not fnmatch.fnmatch(file.name, glob_filter):
                        continue
                    try:
                        text = file.read_text(encoding="utf-8")
                    except (UnicodeDecodeError, OSError):
                        continue
                    for lineno, line in enumerate(text.splitlines(), 1):
                        if regex.search(line):
                            matches.append(f"{file.relative_to(self.root).as_posix()}"
                                           f":{lineno}:{line[:500]}")
                            if len(matches) >= MATCH_LIMIT:
                                break
                    if len(matches) >= MATCH_LIMIT:
                        break
                result = "\n".join(matches) or "no matches"
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
