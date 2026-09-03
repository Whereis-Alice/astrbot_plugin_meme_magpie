#!/usr/bin/env python3
"""\u4ece metadata.yaml + CHANGELOG.md \u63a8\u5bfc\u672c\u6b21 Release \u7684 tag / \u6807\u9898 / \u6b63\u6587\u3002

\u4e4b\u524d\u7684 workflow \u7528 `generate_release_notes: true`\uff0c\u53d1\u51fa\u6765\u7684 Release \u6b63\u6587\u53ea\u6709
\u4e00\u884c commit \u5217\u8868\uff0c\u770b Release \u9875\u7684\u4eba\u5b8c\u5168\u4e0d\u77e5\u9053\u8fd9\u7248\u6539\u4e86\u4ec0\u4e48\u3002\u8fd9\u4e2a\u811a\u672c\u628a
CHANGELOG.md \u91cc\u5bf9\u5e94\u7248\u672c\u7684\u90a3\u4e00\u8282\u539f\u6587\u62ff\u51fa\u6765\u5f53 Release \u6b63\u6587\u3002

\u6545\u610f\u4e0d\u4f9d\u8d56 PyYAML\uff1a\u53ea\u9700\u8981\u8bfb\u4e24\u4e2a\u9876\u5c42\u6807\u91cf\uff0c\u6b63\u5219\u5c31\u591f\uff0c\u907f\u514d runner
\u73af\u5883\u91cc\u6ca1\u88c5\u4f9d\u8d56\u65f6\u6574\u6761\u6d41\u7a0b\u6302\u6389\u3002
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
NOTES_PATH = REPO / "release_notes.md"


def read_scalar(text: str, key: str) -> str:
    """\u8bfb metadata.yaml \u7684\u9876\u5c42\u6807\u91cf\u5b57\u6bb5\uff08\u4e0d\u5904\u7406\u5d4c\u5957\uff0c\u672c\u9879\u76ee\u4e0d\u9700\u8981\uff09\u3002"""
    match = re.search(rf"^{re.escape(key)}:\s*(.+?)\s*$", text, re.MULTILINE)
    if not match:
        return ""
    return match.group(1).strip().strip("\"'")


def extract_section(changelog: str, version: str) -> str:
    """\u62bd\u51fa `## [version]` \u5230\u4e0b\u4e00\u4e2a `## ` \u4e4b\u95f4\u7684\u6b63\u6587\uff08\u4e0d\u542b\u6807\u9898\u884c\uff09\u3002"""
    pattern = re.compile(
        rf"^##\s*\[{re.escape(version)}\][^\n]*\n(.*?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(changelog)
    if not match:
        return ""
    body = match.group(1)
    # \u53bb\u6389\u5c0f\u8282\u672b\u5c3e\u7528\u4f5c\u5206\u9694\u7684 `---`
    body = re.sub(r"\n+-{3,}\s*$", "", body.rstrip())
    return body.strip()


def emit(name: str, value: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    line = f"{name}={value}"
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    print(line)


def tag_exists(tag: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def main() -> int:
    metadata = (REPO / "metadata.yaml").read_text(encoding="utf-8")
    version = read_scalar(metadata, "version")
    if not version:
        print("::error::metadata.yaml \u91cc\u6ca1\u627e\u5230 version \u5b57\u6bb5")
        return 1

    display_name = read_scalar(metadata, "display_name") or read_scalar(metadata, "name")
    tag = f"v{version}"

    changelog_path = REPO / "CHANGELOG.md"
    section = ""
    if changelog_path.is_file():
        section = extract_section(changelog_path.read_text(encoding="utf-8"), version)

    if section:
        NOTES_PATH.write_text(section + "\n", encoding="utf-8")
        has_notes = "true"
    else:
        print(f"::warning::CHANGELOG.md \u91cc\u6ca1\u6709 [{version}] \u8fd9\u4e00\u8282\uff0c\u56de\u9000\u5230\u81ea\u52a8\u751f\u6210\u7684 commit \u5217\u8868")
        NOTES_PATH.write_text("", encoding="utf-8")
        has_notes = "false"

    emit("version", version)
    emit("tag", tag)
    emit("title", f"{tag} \u00b7 {display_name}" if display_name else tag)
    emit("has_notes", has_notes)
    emit("notes_path", str(NOTES_PATH.relative_to(REPO)))
    emit("tag_exists", "true" if tag_exists(tag) else "false")
    return 0


if __name__ == "__main__":
    sys.exit(main())
