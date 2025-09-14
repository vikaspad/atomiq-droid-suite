from __future__ import annotations
from typing import List, Dict
import os
import re
import io

# ----------------------------- FILE BLOCK PARSERS -----------------------------

FILE_BLOCK_RE_A = re.compile(
    r"(?s)<<<FILE:(?P<path>[^>]+)>>>\s*```(?:[\w+-]+)?\s*(?P<body>.*?)```.*?<<<END_FILE>>>"
)

FILE_BLOCK_RE_B = re.compile(
    r"(?m)^FILE:\s*(?P<path>[^\n]+)\n```(?:[\w+-]+)?\n(?P<body>.*?)\n```",
    re.S,
)

ALLOWED_ROOTS = ("unit-tests", "bdd-tests")


def _safe_join(root: str, rel: str) -> str:
    """Resolve and validate a relative path so it stays inside 'root'."""
    rel = (rel or "").strip().lstrip("/\\")
    full = os.path.abspath(os.path.join(root, rel))
    root_abs = os.path.abspath(root)
    if full != root_abs and not full.startswith(root_abs + os.sep):
        raise ValueError(f"Unsafe path refused: {rel}")
    return full


def _clean_body(text: str) -> str:
    """Normalize newlines and strip trailing whitespace without touching content."""
    if text is None:
        return ""
    # Normalize CRLF -> LF, then trim trailing spaces/newlines once.
    return re.sub(r"\r\n?", "\n", text).rstrip() + "\n"


def _materialize(text: str, out_root: str, allowed_roots=ALLOWED_ROOTS) -> int:
    """Write all FILE blocks from 'text' into 'out_root'. Return count written."""
    count = 0
    if not text:
        return 0

    for regex in (FILE_BLOCK_RE_A, FILE_BLOCK_RE_B):
        for m in regex.finditer(text):
            rel = (m.group("path") or "").strip().replace("\\", "/")
            # Only allow files directly under unit-tests/** or bdd-tests/**
            if not any(rel == p or rel.startswith(p + "/") for p in allowed_roots):
                continue

            body = _clean_body(m.group("body"))
            full = _safe_join(out_root, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(body)
            count += 1

    return count


def build_context_bundle(
    summaries: List[Dict], max_files: int = 32, per_file_chars: int = 1500
) -> str:
    """Make a compact, LLM-friendly context from analyzer summaries."""
    buf = io.StringIO()
    buf.write("# Repository Context\n")
    for s in summaries[:max_files]:
        f = s.get("file", "")
        pkg = s.get("package") or ""
        cls = s.get("class") or ""
        methods = ", ".join(m.get("name") for m in s.get("methods", [])[:12])
        buf.write(f"{f} :: {pkg}.{cls} :: methods[{methods}]\n")
        snippet = (s.get("snippet") or "")[:per_file_chars]
        if snippet:
            buf.write("----8<----\n")
            buf.write(snippet + "\n")
            buf.write("---->8----\n\n")
    return buf.getvalue()


# ------------------------------- MAIN PIPELINE -------------------------------

def run_crewai_generation(
    repo_context: str,
    user_prompt: str,
    provider: str,
    model: str,
    out_dir: str,
    do_unit: bool,
    do_bdd: bool,
) -> int:
    """
    Execute CrewAI agents to propose targets and generate tests.
    Returns the number of files written under out_dir/{unit-tests|bdd-tests}.
    Raises RuntimeError if zero files are written (so caller can choose to fail).
    """

    # --- Provider/Key checks (we currently support OpenAI only) ---
    if provider.lower() != "openai":
        raise RuntimeError(f"Unsupported provider '{provider}'. Only 'openai' is supported.")
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY not set; cannot run CrewAI with OpenAI provider."
        )

    # --- Imports here to keep top-level import fast for environments without deps ---
    from crewai import Agent, Task, Crew, Process
    from langchain_openai import ChatOpenAI

    try:
        from .tools.mapper_tools import mapper_toolkit
    except Exception:
        try:
            from backend.agent.tools.mapper_tools import mapper_toolkit
        except Exception:
            from tools.mapper_tools import mapper_toolkit

    tool_instances = mapper_toolkit()        

    # --- LLM: explicit & deterministic enough for code generation ---
    llm = ChatOpenAI(
        model=model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        temperature=0.2,
        timeout=120,
        max_retries=2,
    )

    # --------------------------- AGENTS ---------------------------------------
    mapper = Agent(
        role="Risk-Driven Java Code Cartographer",
        goal=(
            "Produce a prioritized, risk-weighted test map of the repository: identify high-value Java classes and "
            "methods, their responsibilities, key collaborators and seams, observable behaviors, inputs/outputs, "
            "data contracts, and the edge cases that must be validated. Include concrete test ideas and the most "
            "appropriate test style (pure unit, Spring slice, integration) and tools for each target."
        ),
        backstory=(
            "Seasoned SDET/architect across Spring Boot 3, REST, JPA/Hibernate, Bean Validation, scheduling/concurrency, "
            "and testing with JUnit 5, AssertJ, Mockito, Testcontainers, WireMock/MockWebServer, Awaitility, Reactor-Test. "
            "Optimizes for maximum risk burn-down per minute of testing. Excels at surfacing nullability pitfalls, boundary "
            "conditions, idempotency, race conditions, transactional semantics, and serialization/validation mismatches."
        ),
        llm=llm,
        verbose=False,
        allow_delegation=False,
        tools=tool_instances
    )

    # Optional: a short planner to tighten outputs (kept minimal to reduce tokens)
    planner = Agent(
        role="Test Plan Referee",
        goal="Enforce crisp scope and strict output format; ensure each mapping item is actionable for downstream writers.",
        backstory="Pragmatic reviewer who trims fluff, checks naming consistency with code symbols, and validates scoring completeness.",
        llm=llm,
        verbose=False,
        allow_delegation=False,
    )

    agents = [mapper, planner]

    # --------------------------- TASKS ----------------------------------------
    t1 = Task(
        description=(
            "You are the Senior Java Code Cartographer.\n"
            "Analyze this repository context and propose the complete set of classes & methods "
            "to test first. Focus on behavior (not accessors), public/protected APIs, and code paths "
            "with risk or business value.\n\n"
            "Repository Context:\n"
            f"{repo_context}\n\n"
            "Guidelines:\n"
            "1) Ignore trivial getters/setters, Lombok-generated methods, equals/hashCode/toString unless they contain logic.\n"
            "2) Prefer units with outward effects or contracts: controllers, services, repositories, "
            "   domain logic, utilities with branching.\n"
            "3) Call out risks: io, time/clock, random, threading/executors, http, db/jpa, validation, "
            "   boundary, exception paths, stateful/cache.\n"
            "4) Note collaborators and propose seams/mocking strategies where needed (e.g., repo stub, "
            "   clock stub, WebMvcTest slice, etc.).\n"
            "5) Prioritize by impact & risk; include 10–30 entries.\n"
            "6) Do NOT output test code here—only the map.\n"
        ),
        expected_output=(
            "Return EXACTLY TWO sections in this order:\n\n"
            "SECTION 1: BULLETS (human-readable)\n"
            "- <package>.<Class>#<method>(params): <short rationale>; "
            "risks=[io|time|random|threading|http|db|validation|boundary|exception|stateful|cache]; "
            "edges=[comma-separated edge cases]\n"
            "Include 10–30 bullets, ordered by priority.\n\n"
            "SECTION 2: JSON (machine-readable) in a fenced block:\n"
            "```json\n"
            "{ \"candidates\": [ {"
            "  \"package\": \"string\", \"class\": \"string\", \"method\": \"string\", "
            "  \"signature\": \"string\", \"file\": \"string\", \"reasons\": [\"string\"], "
            "  \"risks\": [\"io\",\"time\",\"random\",\"threading\",\"http\",\"db\",\"validation\",\"boundary\",\"exception\",\"stateful\",\"cache\"], "
            "  \"edge_cases\": [\"string\"], \"dependencies\": [\"string\"], "
            "  \"suggested_test_strategy\": ["
            "    \"happy\",\"boundary\",\"exception\",\"parameterized\",\"mock_external\",\"clock_stub\","
            "    \"tempdir\",\"executor_stub\",\"repo_stub\",\"controller_webmvc_test\""
            "  ], "
            "  \"bdd_flow_candidate\": true, \"priority_score\": 0.0 } ] }\n"
            "```\n"
        ),
        agent=mapper,
    )

    t1_guard = Task(
        description=(
            "Review the mapper's output. Ensure it is concise and suitable as input for code generation. "
            "Point out any missing high-priority targets. Return the mapper output unchanged unless critical gaps."
        ),
        expected_output="Either the original map or a minimally edited version with critical additions only.",
        agent=planner,
    )

    tasks = [t1, t1_guard]

    if do_unit:
        unit_writer = Agent(
            role="Cutting-Edge Unit Test Engineer",
            goal="Author production-grade, mutation-resistant JUnit 5 tests that use AssertJ and Mockito 5, "
                    "and selectively apply modern tooling (Parameterized tests, Testcontainers, WireMock/MockWebServer, "
                    "Awaitility, Reactor-Test, WebMvcTest/WebTestClient) based on the class under test.",
            backstory="A senior engineer who writes AAA-structured tests with meaningful, behavior-oriented names and "
                "clear Arrange/Act/Assert sections. Skilled at isolating units with Mockito (inline mocking for finals/records), "
                "capturing interactions via ArgumentCaptor, asserting rich states with AssertJ (including exceptions/soft assertions), "
                "and adding parameterized edge cases. Knows Spring Boot 3 test slices (@WebMvcTest) and when to use WebTestClient or MockMvc; "
                "prefers pure unit tests unless a slice test materially improves confidence. For async/reactive flows, uses Awaitility and StepVerifier. "
                "For code that touches infra (HTTP/DB/Kafka), stubs with WireMock/MockWebServer or spins minimal Testcontainers in tests when essential.",
            llm=llm,
            verbose=False,
            allow_delegation=False,
        )
        unit_format = (
            "For EACH file, output STRICTLY as:\n\n"
            "FILE: unit-tests/pom.xml\n"
            "```xml\n...pom...\n```\n\n"
            "FILE: unit-tests/src/test/java/<package>/<ClassName>Test.java\n"
            "```java\n// imports\n// Arrange/Act/Assert tests\n```\n\n"
            "# OPTIONAL (only if used by tests):\n"
            "FILE: unit-tests/src/test/resources/junit-platform.properties\n"
            "```properties\n# junit configuration if needed\n```\n\n"
            "FILE: unit-tests/src/test/resources/application-test.yml\n"
            "```yaml\n# spring test overrides if needed\n```\n"
        )
        t_unit = Task(
            description=(
                "Using the validated class map and the user's prompt, generate **state-of-the-art unit tests** targeting REAL classes/methods.\n"
                "\nREQUIREMENTS\n"
                "- Output ONLY files under unit-tests/** using the exact FILE format shown below.\n"
                "- Include a minimal, modern Maven pom at unit-tests/pom.xml with properties and plugins:\n"
                "  * junit-jupiter (with junit-jupiter-params), mockito-core & mockito-inline (final classes/records), assertj-core\n"
                "  * (conditionally) reactor-test for reactive code, awaitility for async, wiremock or okhttp MockWebServer for HTTP stubs,\n"
                "    testcontainers (only if a containerized dependency is explicitly exercised in tests), jacoco-maven-plugin (coverage),\n"
                "    maven-surefire-plugin (JUnit Platform).\n"
                "- Tests MUST follow Arrange // Act // Assert comments and have descriptive, behavior-oriented method names.\n"
                "- Prefer isolated unit tests with Mockito over Spring context; only use @WebMvcTest or WebTestClient slice tests for controllers.\n"
                "- Use AssertJ idioms: assertThat, assertThatThrownBy, tuple/filteredOn, SoftAssertions (where valuable), extracting(), usingRecursiveComparison().\n"
                "- Use Mockito idioms: @ExtendWith(MockitoExtension.class), @Mock/@InjectMocks, BDDMockito given/when/then, ArgumentCaptor, verifyNoMoreInteractions.\n"
                "- Add **edge cases** and **negative paths**; include at least one @ParameterizedTest where inputs vary meaningfully.\n"
                "- For reactive flows: use StepVerifier and virtual time where beneficial.\n"
                "- For async/concurrent flows: use Awaitility with sensible timeouts.\n"
                "- For HTTP integrations: prefer WireMock or MockWebServer (do NOT hit the network).\n"
                "- For persistence: prefer pure unit tests with mocked repos; only use Testcontainers if logic truly depends on DB behavior.\n"
                "- Keep tests deterministic and fast; no flaky sleeps.\n"
                "- Target REAL classes/methods in context; NO placeholders.\n"
                "\nPOM GUIDANCE\n"
                "- Define versions via <properties> (e.g., junit.jupiter.version, mockito.version, assertj.version, awaitility.version, reactor.test.version, testcontainers.version).\n"
                "- Configure surefire to use JUnit Platform; jacoco with a reasonable coverage rule (e.g., 0.7 instruction coverage) but do not fail if classes are missing.\n"
                "- Only include libraries that the emitted tests actually use.\n"
                + "\n\n" + unit_format +
                "\nUSER PROMPT:\n" + (user_prompt or "")
            ),
            expected_output="Multiple FILE blocks: one pom.xml plus *Test.java files under unit-tests/src/test/java/**; "
                            "OPTIONAL junit-platform.properties/application-test.yml if referenced by tests.",
            agent=unit_writer,
        )
        tasks.append(t_unit)

    if do_bdd:
        bdd_writer = Agent(
            role="Cutting-Edge BDD Architect",
            goal="Author behavior-driven tests with Cucumber JVM on JUnit 5 that map 1:1 to real repo flows. "
                "Use AssertJ for rich assertions and modern Cucumber features: Scenario Outline, Background, "
                "DataTable/DocString, ParameterType/DataTableType, and parallel execution. "
                "Selectively enable Spring test slices (@CucumberContextConfiguration + @SpringBootTest) when needed; "
                "otherwise keep glue fast with detailed java code and not just empty step definitions.",
            backstory="Veteran BDD engineer who writes business-readable Gherkin with clear Rules and Backgrounds, "
                    "keeps steps atomic, avoids duplication via ParameterType and DataTableType, and adds Hooks for setup/teardown. "
                    "For HTTP integrations, prefers WireMock or MockWebServer; for async logic uses Awaitility; "
                    "for reactive flows uses Reactor Test; uses Testcontainers *only* when behavior depends on real infra. "
                    "Optimizes for speed and determinism; embraces JUnit Platform and Cucumber parallel execution.",
            llm=llm,
            verbose=False,
            allow_delegation=False,
        )
        bdd_format = (
            "For EACH file, output STRICTLY as:\n\n"
            "FILE: bdd-tests/pom.xml\n"
            "```xml\n...pom...\n```\n\n"
            "FILE: bdd-tests/src/test/java/com/generated/bdd/CucumberRunner.java\n"
            "```java\n// JUnit Platform runner for Cucumber\n```\n\n"
            "FILE: bdd-tests/src/test/java/com/generated/bdd/steps/StepDefinitions.java\n"
            "```java\n// step definitions using AssertJ\n```\n\n"
            "FILE: bdd-tests/src/test/resources/features/<feature>.feature\n"
            "```gherkin\nFeature: ...\nScenario: ...\n  Given ...\n  When ...\n  Then ...\n```\n\n"
            "# OPTIONAL (include only if referenced by the glue/tests):\n"
            "FILE: bdd-tests/src/test/resources/junit-platform.properties\n"
            "```properties\n# cucumber.execution.parallel.enabled=true\n# cucumber.execution.parallel.config.fixed.parallelism=4\n# junit.jupiter.execution.parallel.enabled=true\n```\n\n"
            "FILE: bdd-tests/src/test/resources/cucumber.properties\n"
            "```properties\n# cucumber.publish.quiet=true\n# plugin=pretty,summary,html:target/cucumber.html,json:target/cucumber.json\n```\n\n"
            "FILE: bdd-tests/src/test/resources/application-test.yml\n"
            "```yaml\n# spring overrides if @CucumberContextConfiguration is used\n```\n"
        )
        t_bdd = Task(
            description=(
                "Create a **Cucumber BDD** suite aligned to the validated map and real repository entities.\n"
                "\nREQUIREMENTS\n"
                "- Output ONLY files under bdd-tests/** using the exact FILE format shown below.\n"
                "- Scenarios MUST reference real classes/methods/HTTP endpoints in the repo; keep names consistent with code symbols.\n"
                "- Glue MUST assert with AssertJ and map **one-to-one** with step texts; avoid overly generic regex.\n"
                "- Prefer fast, isolated glue with Mockito (@ExtendWith(MockitoExtension.class)); only start Spring via\n"
                "  @CucumberContextConfiguration + @SpringBootTest when necessary (e.g., real MVC slice behavior).\n"
                "- Use modern Cucumber features where appropriate:\n"
                "  * **Background** for shared setup; **Rule** to express constraints;\n"
                "  * **Scenario Outline + Examples** to cover input partitions/edge cases;\n"
                "  * **DataTable / DocString** for structured inputs and payloads;\n"
                "  * **@ParameterType** and **@DataTableType** to convert domain types.\n"
                "- For HTTP: stub with **WireMock** or **MockWebServer**; DO NOT call external services.\n"
                "- For async/concurrent flows: use **Awaitility**; for reactive: **Reactor Test (StepVerifier)** if applicable.\n"
                "- Only include infra libs (WireMock, MockWebServer, Testcontainers, Reactor Test) if the emitted glue actually uses them.\n"
                "- Enable JUnit 5 Platform; support **parallel execution** via junit-platform.properties (optional file) when tests are parallel-safe.\n"
                "- Provide a minimal, modern Maven **pom.xml** including:\n"
                "    * io.cucumber:cucumber-java, io.cucumber:cucumber-junit-platform-engine\n"
                "    * org.junit.jupiter:junit-jupiter, org.assertj:assertj-core\n"
                "    * org.mockito:mockito-core and mockito-inline (for finals/records)\n"
                "    * (conditionally) com.github.tomakehurst:wiremock-jre8 OR okhttp3:mockwebserver, org.awaitility:awaitility,\n"
                "      io.projectreactor:reactor-test, org.testcontainers:testcontainers (and specific modules) when used\n"
                "    * maven-surefire-plugin configured for the JUnit Platform\n"
                "    * (optional) report plugins via cucumber.properties (html/json)\n"
                "- Keep steps deterministic (no Thread.sleep); use hooks for state cleanup; verify interactions with Mockito.\n"
                "\nGLUE GUIDANCE\n"
                "- Organize steps by domain (e.g., steps/OrdersSteps.java) as the suite grows; start with StepDefinitions.java.\n"
                "- StepDefinitions.java should be detailed with java code for all gherkin statements, dont just create empty methods. \n"
                "- Prefer expressive step texts over implementation detail; keep parameter capture precise.\n"
                "- Reuse helpers for JSON payloads and object builders; keep glue thin (business language in steps, logic in helpers/services).\n"
                "\n" + bdd_format +
                "\nRepository Context (for reference):\n" + (repo_context or "")
            ),
            expected_output="FILE blocks for bdd-tests project: pom.xml, JUnit Platform runner, step definitions with AssertJ, "
                            "and realistic .feature files; OPTIONAL junit-platform.properties/cucumber.properties/application-test.yml if used.",
            agent=bdd_writer,
        )
        tasks.append(t_bdd)

    # --------------------------- RUN CREW -------------------------------------
    crew = Crew(
        agents=agents + ([unit_writer] if do_unit else []) + ([bdd_writer] if do_bdd else []),
        tasks=tasks,
        process=Process.sequential,
    )

    result = crew.kickoff()

    # Some CrewAI versions return a complex object; stringify is safest
    content = str(result) if result is not None else ""

    # Always persist the raw LLM output for troubleshooting
    os.makedirs(out_dir, exist_ok=True)
    raw_path = os.path.join(out_dir, "_crewai_raw.md")
    try:
        with open(raw_path, "w", encoding="utf-8") as f:
            f.write(content or "")
    except Exception:
        # Non-fatal: continue
        pass

    # Write files extracted from FILE blocks (only under allowed roots)
    written = _materialize(content, out_dir, allowed_roots=ALLOWED_ROOTS)
    if written == 0:
        raise RuntimeError(
            "CrewAI produced no file blocks. Check _crewai_raw.md for the raw output."
        )

    return written
