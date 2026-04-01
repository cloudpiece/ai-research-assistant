"""
Research Assistant — Claude Agent SDK
Searches the web and reads local documents, writes a Markdown report,
then enters an interactive follow-up Q&A mode.

Usage:
    python research_assistant.py "your research topic"
    python research_assistant.py "your research topic" --docs ./my_documents
    python research_assistant.py "your research topic" --output report.md
    python research_assistant.py "your research topic" --no-interactive
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

import anyio
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query


def build_system_prompt(docs_dir: str | None) -> str:
    base = """You are an expert research assistant. Your job is to:
1. Search the web for authoritative, up-to-date information on the given topic
2. Fetch and read relevant web pages in depth
3. Synthesize findings into a clear, well-structured Markdown report

Research methodology:
- Start with broad web searches to understand the landscape
- Follow up with targeted searches for specific subtopics
- Fetch and read the most relevant pages for detailed information
- Cross-reference multiple sources for accuracy
- Always note the sources you used

Report structure (use this exact format):
# Research Report: [Topic]
**Date:** [today's date]
**Summary:** [2-3 sentence executive summary]

## Key Findings
[Bullet points of the most important discoveries]

## Detailed Analysis
[In-depth sections with headings for each major subtopic]

## Sources
[List of URLs and documents consulted]

## Conclusion
[Final synthesis and takeaways]
"""

    if docs_dir:
        base += f"""
Local documents are available in: {docs_dir}
- Use Glob to find document files (*.pdf, *.txt, *.md, *.docx, etc.)
- Use Read to read their contents
- Use Grep to search for specific terms within documents
- Integrate insights from local documents with web research findings
"""
    return base.strip()


async def run_research(
    topic: str,
    docs_dir: str | None,
    output_file: str,
    max_turns: int,
) -> str | None:
    """Run the initial research phase. Returns the session_id for follow-up questions."""
    print(f"\nResearching: {topic}")
    if docs_dir:
        print(f"Local docs:  {docs_dir}")
    print(f"Output:      {output_file}")
    print("-" * 60)

    allowed_tools = ["WebSearch", "WebFetch", "Write"]
    if docs_dir:
        allowed_tools += ["Read", "Glob", "Grep"]

    prompt = f"""Research the following topic thoroughly and write a comprehensive Markdown report.

Topic: {topic}

Save the final report to: {output_file}

Steps:
1. Search the web multiple times with different queries to gather broad coverage
2. Fetch and read the most relevant pages in detail
{f'3. Search local documents in {docs_dir} for additional context' if docs_dir else ''}
4. Write a comprehensive, well-structured Markdown report to {output_file}

Be thorough — perform at least 3-5 web searches and read multiple pages before writing the report.
"""

    session_id = None

    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            allowed_tools=allowed_tools,
            permission_mode="acceptEdits",
            system_prompt=build_system_prompt(docs_dir),
            max_turns=max_turns,
            cwd=str(Path(output_file).parent.resolve()),
        ),
    ):
        if isinstance(message, ResultMessage):
            session_id = message.session_id
            print("\n" + "-" * 60)
            if message.is_error:
                print(f"Research failed: {message.result}")
                return None
            print("Research complete!")
            if message.result:
                print(f"\n{message.result}")

    if session_id and Path(output_file).exists():
        size = Path(output_file).stat().st_size
        print(f"\nReport saved: {output_file} ({size:,} bytes)")
    elif session_id:
        print(f"\nNote: Report file not found at {output_file}")

    return session_id


async def ask_follow_up(
    question: str,
    session_id: str,
    output_file: str,
    max_turns: int,
) -> str | None:
    """Run a single follow-up question using the existing research session."""
    answer_parts: list[str] = []

    async for message in query(
        prompt=question,
        options=ClaudeAgentOptions(
            resume=session_id,
            permission_mode="acceptEdits",
            max_turns=max_turns,
            cwd=str(Path(output_file).parent.resolve()),
        ),
    ):
        if isinstance(message, ResultMessage) and message.result:
            answer_parts.append(message.result)

    return "\n".join(answer_parts) if answer_parts else None


def append_qa_to_report(output_file: str, question: str, answer: str) -> None:
    """Append a Q&A pair to the report file."""
    report_path = Path(output_file)
    if not report_path.exists():
        return

    content = report_path.read_text(encoding="utf-8")

    if "## Follow-up Q&A" not in content:
        content += "\n\n---\n\n## Follow-up Q&A\n"

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    content += f"\n### Q: {question}\n*({timestamp})*\n\n{answer}\n"
    report_path.write_text(content, encoding="utf-8")


async def interactive_loop(
    session_id: str,
    output_file: str,
    max_turns: int,
) -> None:
    """Interactive Q&A loop using the research session context."""
    print("\n" + "=" * 60)
    print("Interactive Follow-up Mode")
    print("=" * 60)
    print("Ask follow-up questions about the research.")
    print("Commands:  'save' — show report path | 'exit' — quit")
    print("-" * 60)

    while True:
        try:
            user_input = input("\nYour question: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nExiting interactive mode.")
            break

        if not user_input:
            continue

        if user_input.lower() in ("exit", "quit", "q"):
            print("\nExiting interactive mode.")
            break

        if user_input.lower() == "save":
            print(f"Report file: {output_file}")
            continue

        print("\n[Researching your question...]\n")

        try:
            answer = await ask_follow_up(user_input, session_id, output_file, max_turns)
        except Exception as e:
            print(f"Error: {e}")
            continue

        if answer:
            print("-" * 60)
            print(answer)
            append_qa_to_report(output_file, user_input, answer)
            print("\n[Answer appended to report]")
        else:
            print("[No answer returned]")


async def run(
    topic: str,
    docs_dir: str | None,
    output_file: str,
    max_turns: int,
    interactive: bool,
) -> None:
    session_id = await run_research(topic, docs_dir, output_file, max_turns)

    if interactive and session_id:
        await interactive_loop(session_id, output_file, max_turns)
    elif interactive and not session_id:
        print("\nCould not start interactive mode: research did not complete successfully.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Research assistant powered by Claude — searches the web and local docs, writes a Markdown report."
    )
    parser.add_argument("topic", help="The research topic or question")
    parser.add_argument(
        "--docs",
        metavar="DIR",
        help="Optional path to a directory of local documents to include in research",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        default=None,
        help="Output Markdown file path (default: report_TOPIC_TIMESTAMP.md)",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=40,
        help="Maximum agent turns per query (default: 40)",
    )
    parser.add_argument(
        "--no-interactive",
        action="store_true",
        help="Skip interactive follow-up mode and exit after writing the report",
    )
    args = parser.parse_args()

    if args.output:
        output_file = str(Path(args.output).resolve())
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_topic = "".join(c if c.isalnum() or c in " _-" else "_" for c in args.topic)
        safe_topic = safe_topic[:40].strip().replace(" ", "_")
        output_file = str(Path.cwd() / f"report_{safe_topic}_{timestamp}.md")

    docs_dir = None
    if args.docs:
        docs_dir = str(Path(args.docs).resolve())
        if not Path(docs_dir).is_dir():
            print(f"Error: --docs path does not exist or is not a directory: {docs_dir}")
            sys.exit(1)

    anyio.run(run, args.topic, docs_dir, output_file, args.max_turns, not args.no_interactive)


if __name__ == "__main__":
    main()
