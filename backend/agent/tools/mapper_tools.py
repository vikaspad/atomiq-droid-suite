from __future__ import annotations

import fnmatch
import pathlib
import re
import subprocess
from typing import List, Optional, Dict, Any, Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field, field_validator


# ---------------------------- Pydantic Schemas ----------------------------
#This module defines a toolkit of CrewAI tools (each a BaseTool) that help an agent analyze Java/Spring repositories
class GlobInput(BaseModel):
    """
    Input for RepoGlobTool.

    Attributes:
        root: Filesystem path to the repository root to search.
        patterns: Glob patterns to apply (e.g., ["**/*.java", "**/*.xml"]).
        ignore: Optional list of ignore globs applied to *relative* paths
                (e.g., ["**/target/**", "**/build/**", "**/*.min.js"]).
    """
    root: str = Field(description="Repo root (folder to search)")
    patterns: List[str] = Field(description="Glob patterns, e.g., ['**/*.java']")
    ignore: List[str] = Field(default_factory=list, description="Ignore globs (applied to relative paths)")

    @field_validator('root')
    @classmethod
    def _root_must_exist(cls, v: str) -> str:
        if not v or not pathlib.Path(v).exists():
            raise ValueError(f"Root does not exist: {v}")
        return v

    @field_validator('patterns')
    @classmethod
    def _patterns_not_empty(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("patterns cannot be empty")
        return v


class ReadTextInput(BaseModel):
    """
    Input for ReadTextTool.

    Attributes:
        path: Path to a text file. File must exist. UTF-8 is assumed; decode errors are ignored.
    """
    path: str = Field(description="File path to read")

    @field_validator('path')
    @classmethod
    def _path_must_exist(cls, v: str) -> str:
        p = pathlib.Path(v)
        if not p.is_file():
            raise ValueError(f"File not found: {v}")
        return v


class GrepInput(BaseModel):
    """
    Input for GrepTool.

    Attributes:
        pattern: Regular expression pattern to search for.
        path: File or directory to search. Directories will be walked recursively.
        flags: Optional flags string: currently supports 'i' for IGNORECASE.
               (Extendable to add 'm' or 's' if needed.)
    """
    pattern: str = Field(description="Regex pattern to search")
    path: str = Field(description="File or directory to search in")
    flags: Optional[str] = Field(default="", description="Regex flags string (supports 'i' for IGNORECASE)")

    @field_validator('pattern')
    @classmethod
    def _pattern_not_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("pattern cannot be empty")
        return v

    @field_validator('path')
    @classmethod
    def _path_must_exist(cls, v: str) -> str:
        p = pathlib.Path(v)
        if not p.exists():
            raise ValueError(f"Path not found: {v}")
        return v


class JavaOutlineInput(BaseModel):
    """
    Input for JavaOutlineTool.
    Attributes:
        path: Path to a .java source file (must exist and end with .java).
    """
    path: str = Field(description="Java source file path")

    @field_validator('path')
    @classmethod
    def _must_be_java_file(cls, v: str) -> str:
        p = pathlib.Path(v)
        if not p.is_file():
            raise ValueError(f"File not found: {v}")
        if p.suffix.lower() != ".java":
            raise ValueError("java_outline only accepts .java files")
        return v


class PomInput(BaseModel):
    """
    Input for MavenCoordsTool.

    Attributes:
        pom_path: Path to a maven pom.xml file.
    """
    pom_path: str = Field(description="Path to pom.xml")

    @field_validator('pom_path')
    @classmethod
    def _must_be_pom(cls, v: str) -> str:
        p = pathlib.Path(v)
        if not p.is_file():
            raise ValueError(f"File not found: {v}")
        if p.name != "pom.xml":
            raise ValueError("maven_coords expects a pom.xml file")
        return v


class GitChurnInput(BaseModel):
    """
    Input for GitChurnTool.

    Attributes:
        repo_root: Path to a git repository root (directory containing .git/).
        since: A date/range understood by git, e.g., '90 days ago' or '2024-01-01'.
    """
    repo_root: str = Field(description="Git repo root")
    since: str = Field(description="e.g., '90 days ago' or '2024-01-01'")

    @field_validator('repo_root')
    @classmethod
    def _repo_root_exists(cls, v: str) -> str:
        p = pathlib.Path(v)
        if not p.exists():
            raise ValueError(f"Repo root not found: {v}")
        return v

    @field_validator('since')
    @classmethod
    def _since_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("since must be a non-empty string")
        return v


class GitBlameInput(BaseModel):
    """
    Input for GitBlameTopAuthorsTool.

    Attributes:
        repo_root: Path to a git repository root (directory containing .git/).
        file: File path (inside repo_root) to run blame on.
    """
    repo_root: str = Field(description="Git repo root")
    file: str = Field(description="Path to a file inside repo_root")

    @field_validator('repo_root')
    @classmethod
    def _repo_root_exists(cls, v: str) -> str:
        if not pathlib.Path(v).exists():
            raise ValueError(f"Repo root not found: {v}")
        return v

    @field_validator('file')
    @classmethod
    def _file_exists(cls, v: str) -> str:
        if not pathlib.Path(v).is_file():
            raise ValueError(f"File not found for blame: {v}")
        return v


# ------------------------------- Tool Classes -------------------------------
# Identifies files based on the pattern (**/*.java)
class RepoGlobTool(BaseTool):
    """
    List files relative to a repository root using glob patterns, with optional ignore filters.

    Returns:
        Sorted list of relative paths (POSIX-style separators).
    """
    name: str = "repo_glob"
    description: str = "List files under a repo root matching glob patterns, with optional ignore rules."
    args_schema: Type[BaseModel] = GlobInput

    def _run(self, **kwargs) -> List[str]:
        root = kwargs["root"]
        patterns = kwargs["patterns"]
        ignore = kwargs.get("ignore", [])
        rootp = pathlib.Path(root)
        out: List[str] = []
        for pat in patterns:
            for p in rootp.rglob(pat):
                rel = str(p.relative_to(rootp)).replace("\\", "/")
                if any(fnmatch.fnmatch(rel, ig) for ig in ignore):
                    continue
                out.append(rel)
        return sorted(out)

    # Optional: keeps compatibility if something calls .run()
    def run(self, *args, **kw):
        return self._run(**kw)

# Takes filepath and input and reads the file and returns its content as a string
class ReadTextTool(BaseTool):
    """
    Read a UTF-8 text file and return contents.

    Notes:
        - Decoding errors are ignored to avoid crashes on mixed encodings/binaries.
    """
    name: str = "read_text"
    description: str = "Read a UTF-8 text file and return its contents."
    args_schema: Type[BaseModel] = ReadTextInput

    def _run(self, **kwargs) -> str:
        path = kwargs["path"]
        return pathlib.Path(path).read_text(encoding="utf-8", errors="ignore")

    def run(self, *args, **kw):
        return self._run(**kw)

# Based on the regex and a file or folder; it scans lines and returns where it matched
class GrepTool(BaseTool):
    """
    Regex search across a file or recursively within a directory.

    Input flags:
        - 'i' → re.IGNORECASE

    Returns:
        List of dicts: {"file": path, "line_no": str, "line": str}
    """
    name: str = "grep"
    description: str = "Regex search across a file or directory. Returns [{'file','line_no','line'}]."
    args_schema: Type[BaseModel] = GrepInput

    def _run(self, **kwargs) -> List[Dict[str, str]]:
        pattern = kwargs["pattern"]
        path = kwargs["path"]
        flags = kwargs.get("flags", "") or ""

        fl = re.IGNORECASE if "i" in flags.lower() else 0
        rx = re.compile(pattern, fl)
        result: List[Dict[str, str]] = []
        p = pathlib.Path(path)
        files = [p] if p.is_file() else list(p.rglob("*"))
        for f in files:
            if not f.is_file():
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    result.append({"file": str(f), "line_no": str(i), "line": line.strip()})
        return result

    def run(self, *args, **kw):
        return self._run(**kw)

# Based on .java file, it extracts the package, class names, public method names and annotations
class JavaOutlineTool(BaseTool):
    """
    Produce a very light outline of a Java source file.

    Extracts:
        - package name
        - class names (simple)
        - public method names
        - @Annotations present
        - Convenience booleans: has_transactional, has_scheduled, has_rest

    Note:
        This is regex-based and intentionally shallow; not a complete parser.
    """
    name: str = "java_outline"
    description: str = "Very light outline for a Java file (package, classes, public methods, annotations)."
    args_schema: Type[BaseModel] = JavaOutlineInput

    def _run(self, **kwargs) -> Dict[str, Any]:
        path = kwargs["path"]
        src = pathlib.Path(path).read_text(encoding="utf-8", errors="ignore")
        pkg = re.search(r"^\s*package\s+([a-zA-Z0-9_.]+)\s*;", src, re.M)
        classes = re.findall(r"(?:@[\w.()\"',\s]+)?\s*(public|protected|private)?\s*(final|abstract)?\s*class\s+(\w+)", src)
        methods = re.findall(r"(@\w[\w.()\"',\s]*)?\s+public\s+[<>\w\[\]?.,\s]+?\s+(\w+)\s*\(", src)
        annos = list(set(re.findall(r"@\w+", src)))
        return {
            "package": pkg.group(1) if pkg else None,
            "classes": [c[2] for c in classes],
            "public_methods": [m[1] for m in methods],
            "annotations": annos,
            "has_transactional": "@Transactional" in annos,
            "has_scheduled": "@Scheduled" in annos,
            "has_rest": any(a in annos for a in ["@RestController", "@Controller", "@GetMapping", "@PostMapping"]),
        }

    def run(self, *args, **kw):
        return self._run(**kw)

# Read pom.xml and pulls out groupId, artifactId, version, and lists dependencies, highlighting any Spring Boot starters
class MavenCoordsTool(BaseTool):
    """
    Parse a pom.xml to extract Maven coordinates and dependency info.

    Returns:
        {
            "groupId": str|None,
            "artifactId": str|None,
            "version": str|None,
            "starters": [ "groupId:artifactId", ... ],
            "dependencies": [ "groupId:artifactId", ... ],
        }

    Caveat:
        Uses regex, so it won't resolve ${properties}, parents, or profiles.
    """
    name: str = "maven_coords"
    description: str = "Parse a pom.xml for groupId, artifactId, version, and Spring Boot starters."
    args_schema: Type[BaseModel] = PomInput

    def _run(self, **kwargs) -> Dict[str, Any]:
        pom_path = kwargs["pom_path"]
        text = pathlib.Path(pom_path).read_text(encoding="utf-8", errors="ignore")
        g = re.search(r"<groupId>([^<]+)</groupId>", text)
        a = re.search(r"<artifactId>([^<]+)</artifactId>", text)
        v = re.search(r"<version>([^<]+)</version>", text)
        deps = re.findall(
            r"<dependency>.*?<groupId>([^<]+)</groupId>.*?<artifactId>([^<]+)</artifactId>.*?</dependency>", text, re.S
        )
        starters = [f"{x[0]}:{x[1]}" for x in deps if "spring-boot-starter" in x[1]]
        return {
            "groupId": g.group(1) if g else None,
            "artifactId": a.group(1) if a else None,
            "version": v.group(1) if v else None,
            "starters": starters,
            "dependencies": [f"{x[0]}:{x[1]}" for x in deps],
        }

    def run(self, *args, **kw):
        return self._run(**kw)

# Using git log --numstat, it totals added+deleted lines per file since a date/range you provide, then returns the files sorted by most change
class GitChurnTool(BaseTool):
    """
    Compute file churn since a given date/range via `git log --numstat`.

    Returns:
        List of {"file": path, "churn": str(total_adds_plus_deletes)} sorted descending by churn.

    Notes:
        - Requires git installed and repo_root to be a git repo.
        - Errors return [] to keep agents resilient.
    """
    name: str = "git_churn"
    description: str = "Compute churn per file since a given date/range using `git log --numstat`."
    args_schema: Type[BaseModel] = GitChurnInput

    def _run(self, **kwargs) -> List[Dict[str, str]]:
        repo_root = kwargs["repo_root"]
        since = kwargs["since"]
        cmd = ["git", "-C", repo_root, "log", f"--since={since}", "--numstat", "--pretty=format:--"]
        try:
            out = subprocess.check_output(cmd, text=True)
        except Exception:
            return []
        churn: Dict[str, int] = {}
        for line in out.splitlines():
            if line.startswith("--") or not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) == 3:
                add, delete, file = parts
                try:
                    a = int(add) if add.isdigit() else 0
                    d = int(delete) if delete.isdigit() else 0
                except ValueError:
                    a = d = 0
                churn[file] = churn.get(file, 0) + a + d
        return [{"file": f, "churn": str(v)} for f, v in sorted(churn.items(), key=lambda x: -x[1])]

    def run(self, *args, **kw):
        return self._run(**kw)

# Using git blame, it counts lines per author for a given file and returns the top authors by line count
class GitBlameTopAuthorsTool(BaseTool):
    """
    Aggregate top authors for a file using `git blame --line-porcelain`.

    Returns:
        List of {"author": str, "lines": str} sorted descending by lines.

    Notes:
        - Requires git installed; returns [] on failure.
    """
    name: str = "git_blame_top_authors"
    description: str = "Top authors by blame count for a given file."
    args_schema: Type[BaseModel] = GitBlameInput

    def _run(self, **kwargs) -> List[Dict[str, str]]:
        repo_root = kwargs["repo_root"]
        file = kwargs["file"]
        try:
            out = subprocess.check_output(
                ["git", "-C", repo_root, "blame", "--line-porcelain", file], text=True
            )
        except Exception:
            return []
        counts: Dict[str, int] = {}
        for line in out.splitlines():
            if line.startswith("author "):
                author = line[len("author "):].strip()
                counts[author] = counts.get(author, 0) + 1
        return [{"author": a, "lines": str(n)} for a, n in sorted(counts.items(), key=lambda x: -x[1])]

    def run(self, *args, **kw):
        return self._run(**kw)


# ------------------------------- Factory Helper -------------------------------

def mapper_toolkit() -> List[BaseTool]:
    """Convenience to build all tool instances for injection into Agents."""
    return [
        RepoGlobTool(),
        ReadTextTool(),
        GrepTool(),
        JavaOutlineTool(),
        MavenCoordsTool(),
        GitChurnTool(),
        GitBlameTopAuthorsTool(),
    ]
