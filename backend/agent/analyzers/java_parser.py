import os, re
from typing import List, Dict

PACKAGE_RE = re.compile(r'^\s*package\s+([\w\.]+)\s*;', re.MULTILINE)
CLASS_RE = re.compile(r'\b(class|interface|enum)\s+(\w+)')
METHOD_RE = re.compile(r'(public|protected|private)\s+[\w\<\>\[\]]+\s+(\w+)\s*\(([^)]*)\)\s*(?:throws\s+[\w\.,\s]+)?\s*\{', re.MULTILINE)

# walk through a folder and list all .java files.
def discover_java_files(root: str) -> List[str]:
    files = []
    for base, _, names in os.walk(root):
        lowered = base.lower()
        if any(skip in lowered for skip in ["target", "build", "out", "node_modules", "generated-sources"]):
            continue
        for n in names:
            if n.endswith(".java"):
                files.append(os.path.join(base, n))
    return files

# give a quick summary of a single Java source file
def summarize_java(file_path: str) -> Dict:
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        src = f.read()
    pkg = (PACKAGE_RE.search(src).group(1)) if PACKAGE_RE.search(src) else None
    cls = (CLASS_RE.search(src).group(2)) if CLASS_RE.search(src) else None
    methods = [{'name': m.group(2), 'params': m.group(3).strip()} for m in METHOD_RE.finditer(src)]
    snippet = src[:2000]
    return {'file': file_path, 'package': pkg, 'class': cls, 'methods': methods, 'snippet': snippet}
