"""
Research-to-Publish Pipeline
Orchestrates two agents:
  1. Research Agent  — searches the web, writes a Markdown report
  2. Publisher Agent — converts the report to MDX and publishes it to the website

Usage:
    python pipeline.py                        # interactive mode
    python pipeline.py --topic "AI in medicine" --type blog
    python pipeline.py --auto                 # picks next topic from daily_topics.json
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import anyio
from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, ResultMessage, query

# Paths — WEBSITE_DIR can be overridden via env var (used in CI)
ROOT = Path(__file__).parent.resolve()
WEBSITE_DIR = Path(os.environ.get("WEBSITE_DIR", ROOT / "website"))
REPORTS_DIR = ROOT / "reports"
TOPICS_FILE = ROOT / "daily_topics.json"


# ---------------------------------------------------------------------------
# Interactive prompt helpers
# ---------------------------------------------------------------------------

def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(0)
    return value or default


def ask_list(prompt: str) -> list[str]:
    raw = ask(prompt + " (comma-separated, or leave blank)")
    return [t.strip() for t in raw.split(",") if t.strip()] if raw else []


def gather_inputs() -> dict:
    print("\n" + "=" * 60)
    print("  Research → Publish Pipeline")
    print("=" * 60)

    topic = ""
    while not topic:
        topic = ask("\nResearch topic")
        if not topic:
            print("  Topic cannot be empty. Please try again.")

    print("\nContent type:")
    print("  1. Blog post")
    print("  2. Research paper")
    choice = ""
    while choice not in ("1", "2"):
        choice = ask("Choose", default="1")
    content_type = "blog" if choice == "1" else "papers"

    tags = ask_list("Tags")

    authors: list[str] = []
    venue = ""
    doi = ""
    if content_type == "papers":
        authors = ask_list("Authors")
        venue = ask("Venue (journal/conference)", default="")
        doi = ask("DOI (optional)", default="")

    docs_dir = ask("\nLocal docs directory (optional, leave blank to skip)", default="")
    docs_dir = str(Path(docs_dir).resolve()) if docs_dir else None

    return {
        "topic": topic,
        "content_type": content_type,
        "tags": tags,
        "authors": authors,
        "venue": venue,
        "doi": doi,
        "docs_dir": docs_dir,
    }


# ---------------------------------------------------------------------------
# Auto mode: pick next topic from daily_topics.json
# ---------------------------------------------------------------------------

def pick_next_topic() -> dict:
    if not TOPICS_FILE.exists():
        print(f"Error: {TOPICS_FILE} not found. Create it or run in interactive mode.")
        sys.exit(1)

    data = json.loads(TOPICS_FILE.read_text(encoding="utf-8"))
    topics = data.get("topics", [])
    last_index = data.get("last_index", -1)

    if not topics:
        print("Error: No topics found in daily_topics.json.")
        sys.exit(1)

    next_index = (last_index + 1) % len(topics)
    entry = topics[next_index]

    # Save the updated index back
    data["last_index"] = next_index
    TOPICS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    print(f"\nAuto mode: picked topic {next_index + 1}/{len(topics)}: {entry['topic']}")
    return {
        "topic": entry["topic"],
        "content_type": entry.get("type", "blog"),
        "tags": entry.get("tags", []),
        "authors": entry.get("authors", []),
        "venue": entry.get("venue", ""),
        "doi": entry.get("doi", ""),
        "docs_dir": None,
    }


# ---------------------------------------------------------------------------
# Agent system prompts
# ---------------------------------------------------------------------------

def research_system_prompt(docs_dir: str | None) -> str:
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
# [Topic]
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


PUBLISHER_SYSTEM_PROMPT = """You are a content publisher. Your job is to:
1. Read a Markdown research report
2. Convert it into a properly formatted MDX file with YAML frontmatter
3. Save it to the correct directory in the website project
4. Commit and push the changes to GitHub so the website is updated

Always produce clean, readable MDX. Preserve all headings, lists, code blocks,
and source links from the original report.
"""


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def run_pipeline(inputs: dict) -> None:
    topic = inputs["topic"]
    content_type = inputs["content_type"]
    tags = inputs["tags"]
    authors = inputs["authors"]
    venue = inputs["venue"]
    doi = inputs["doi"]
    docs_dir = inputs["docs_dir"]

    REPORTS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_topic = "".join(c if c.isalnum() or c in " _-" else "_" for c in topic)
    safe_topic = safe_topic[:40].strip().replace(" ", "_")
    report_file = REPORTS_DIR / f"report_{safe_topic}_{timestamp}.md"

    slug = safe_topic.lower().replace("_", "-")
    publish_path = WEBSITE_DIR / "content" / content_type / f"{slug}.mdx"
    today = datetime.now().strftime("%Y-%m-%d")

    frontmatter_fields = f"""title: (derive a concise title from the report)
date: "{today}"
excerpt: (write a 1-2 sentence summary based on the report's Summary section)
tags: {tags if tags else []}"""

    if content_type == "papers":
        frontmatter_fields += f"""
authors: {authors if authors else []}
venue: "{venue}"
doi: "{doi}"
"""

    research_tools = ["WebSearch", "WebFetch", "Write"]
    if docs_dir:
        research_tools += ["Read", "Glob", "Grep"]

    agents = {
        "research-agent": AgentDefinition(
            description="Researches a topic on the web and writes a Markdown report to a file.",
            prompt=research_system_prompt(docs_dir),
            tools=research_tools,
        ),
        "publisher-agent": AgentDefinition(
            description="Reads a Markdown report and publishes it as MDX to the website, then commits and pushes to GitHub.",
            prompt=PUBLISHER_SYSTEM_PROMPT,
            tools=["Read", "Write", "Bash"],
        ),
    }

    orchestrator_prompt = f"""You are coordinating a research-to-publish pipeline. Follow these steps in order:

STEP 1 — Research
Use the research-agent to research this topic and save a Markdown report:
- Topic: {topic}
- Save the report to: {report_file}
{f'- Local docs available at: {docs_dir}' if docs_dir else ''}

STEP 2 — Publish
Once the report is saved, use the publisher-agent to:
1. Read the report from: {report_file}
2. Convert it to an MDX file with this YAML frontmatter:
---
{frontmatter_fields}
---
3. Save the MDX file to: {publish_path}
4. Run these commands to commit and push to GitHub:
   cd {WEBSITE_DIR}
   git add content/{content_type}/{slug}.mdx
   git commit -m "publish: {topic}"
   git push

After both steps complete, summarize what was researched and confirm the file was published.
"""

    print("\n" + "=" * 60)
    print(f"  Topic:   {topic}")
    print(f"  Type:    {content_type}")
    print(f"  Report:  {report_file}")
    print(f"  Publish: {publish_path}")
    print("=" * 60)
    print("\nStarting pipeline...\n")

    async for message in query(
        prompt=orchestrator_prompt,
        options=ClaudeAgentOptions(
            allowed_tools=["Agent"],
            agents=agents,
            permission_mode="acceptEdits",
            max_turns=10,
        ),
    ):
        if isinstance(message, ResultMessage):
            print("\n" + "=" * 60)
            if message.is_error:
                print(f"Pipeline failed: {message.result}")
                sys.exit(1)
            else:
                print("Pipeline complete!\n")
                if message.result:
                    print(message.result)
            if hasattr(message, "total_cost_usd") and message.total_cost_usd:
                print(f"\nTotal cost: ${message.total_cost_usd:.4f}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Research-to-publish multi-agent pipeline.")
    parser.add_argument("--topic", help="Research topic (skips interactive prompt)")
    parser.add_argument("--type", choices=["blog", "papers"], default="blog", dest="content_type")
    parser.add_argument("--tags", help="Comma-separated tags", default="")
    parser.add_argument("--auto", action="store_true", help="Pick next topic from daily_topics.json")
    args = parser.parse_args()

    if args.auto:
        inputs = pick_next_topic()
    elif args.topic:
        inputs = {
            "topic": args.topic,
            "content_type": args.content_type,
            "tags": [t.strip() for t in args.tags.split(",") if t.strip()],
            "authors": [],
            "venue": "",
            "doi": "",
            "docs_dir": None,
        }
    else:
        inputs = gather_inputs()

    anyio.run(run_pipeline, inputs)


if __name__ == "__main__":
    main()
