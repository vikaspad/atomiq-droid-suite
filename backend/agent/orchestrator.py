import os
from typing import List, Dict, Tuple
from .models import BuildOptions
from .repo import shallow_clone
from .analyzers.java_parser import discover_java_files, summarize_java
from .utils.zipper import zip_dir

# From a list of Java file summaries, pick up to limit targets as (package, className) pairs.
def _pick_targets(summaries: List[Dict], limit: int = 6) -> List[Tuple[str, str]]:
    picked = []
    for s in summaries:
        cls = s.get("class"); pkg = s.get("package")
        if cls:
            picked.append((pkg or "", cls))
        if len(picked) >= limit:
            break
    return picked

"""Runs the whole build flow: clones the repo, scans Java, optionally uses CrewAI to generate tests (unit or BDD), 
   falls back to a simple scaffold if AI isn’t available, then zips everything and returns the ZIP path.  """
def run_pipeline(opts: BuildOptions, job_progress_cb, base_dir: str) -> str:
    work = os.path.join(base_dir, "work", opts.job_id)
    os.makedirs(work, exist_ok=True)

    job_progress_cb(5, "Fetching repository")
    repo = shallow_clone(opts.github_url, work)

    job_progress_cb(25, "Scanning Java sources")
    files = discover_java_files(repo)
    if not files:
        raise RuntimeError("No Java files found in the repository.")

    summaries: List[Dict] = []
    for fp in files[:200]:
        try:
            summaries.append(summarize_java(fp))
        except Exception:
            continue

    # radio semantics
    do_unit = bool(opts.generate_unit) and not bool(opts.generate_bdd)
    do_bdd  = bool(opts.generate_bdd)  and not bool(opts.generate_unit)
    if not (do_unit or do_bdd):
        do_unit = True

    out_root = os.path.join(work, "generated-tests")
    os.makedirs(out_root, exist_ok=True)

    # If an API key is present, REQUIRE CrewAI to run (no silent fallback).
    used_ai = False
    ai_required = bool(os.environ.get("OPENAI_API_KEY"))
    try:
        from .crewai_pipeline import build_context_bundle, run_crewai_generation
        job_progress_cb(55, "CrewAI generation")
        ctx = build_context_bundle(summaries, max_files=32, per_file_chars=1500)

        # optionally include uploaded requirement file
        if getattr(opts, "requirement_path", None):
            try:
                with open(opts.requirement_path, "r", encoding="utf-8", errors="ignore") as f:
                    req = f.read()
                ctx += "\n# Requirements\n" + req[:4000]
            except Exception:
                pass

        count = run_crewai_generation(
            repo_context=ctx,
            user_prompt=opts.prompt or "",
            provider=opts.llm_provider or "openai",
            model=opts.llm_model or "gpt-4o-mini",
            out_dir=out_root,
            do_unit=do_unit,
            do_bdd=do_bdd
        )
        used_ai = count > 0
    except Exception as e:
        if ai_required:
            # When key is present, fail fast so you SEE the reason (tokens path)
            raise
        else:
            with open(os.path.join(out_root, "NOTICE.txt"), "w", encoding="utf-8") as f:
                f.write("CrewAI not available or failed; using fallback scaffold.\n")
                f.write(str(e))

    if not used_ai:
        # Fallback scaffold (only when AI not required)
        from pathlib import Path
        def _pkg_to_path(pkg: str) -> str: return pkg.replace(".", "/") if pkg else ""
        targets = _pick_targets(summaries, limit=1) or [("", "Sample")]

        if do_unit:
            unit_root = os.path.join(out_root, "unit-tests")
            os.makedirs(unit_root, exist_ok=True)
            Path(os.path.join(unit_root, "pom.xml")).write_text(
                "<project xmlns='http://maven.apache.org/POM/4.0.0'><modelVersion>4.0.0</modelVersion>"
                "<groupId>ai.atomiq</groupId><artifactId>unit-tests</artifactId><version>1.0.0</version></project>",
                encoding="utf-8"
            )
            pkg, cls = targets[0]
            test_path = os.path.join(unit_root, "src", "test", "java", _pkg_to_path(pkg), f"{cls}Test.java")
            os.makedirs(os.path.dirname(test_path), exist_ok=True)
            Path(test_path).write_text(
                (f"package {pkg};\n\n" if pkg else "") +
                "import org.junit.jupiter.api.*;\nimport static org.junit.jupiter.api.Assertions.*;\n\n"
                f"class {cls}Test {{\n  @Test void sanity() {{ assertTrue(true); }}\n}}\n",
                encoding="utf-8"
            )

        if do_bdd:
            bdd_root = os.path.join(out_root, "bdd-tests")
            os.makedirs(bdd_root, exist_ok=True)
            feat = os.path.join(bdd_root, "src", "test", "resources", "features", "sample.feature")
            os.makedirs(os.path.dirname(feat), exist_ok=True)
            Path(feat).write_text(
                "Feature: Sample\n"
                "  Scenario: Always true\n"
                "    Given nothing\n"
                "    Then it works\n",
                encoding="utf-8"
            )

    job_progress_cb(85, "Packaging")
    artifacts = os.path.join(base_dir, "artifacts"); os.makedirs(artifacts, exist_ok=True)
    zip_path = os.path.join(artifacts, f"{opts.job_id}-tests.zip")
    zip_dir(out_root, zip_path)

    job_progress_cb(100, "Complete")
    return zip_path
