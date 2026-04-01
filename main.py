"""
Russian Daily Tutor — Agentic Rewrite
======================================
Features:
  • Multi-step reasoning loop  — LLM plans which word to teach based on history/level
  • Tool use                   — dictionary, examples, progress tools the LLM calls
  • Interactive CLI            — greets user, collects feedback, adapts difficulty
  • SQLite progress tracking   — persists every word, rating, and level over time
  • Anki + audio output        — preserved from v1
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import os
import sqlite3
import textwrap
from pathlib import Path
from typing import Any

from gtts import gTTS
from openai import OpenAI

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

DEFAULT_MODEL = "deepseek-r1-8b"
DEFAULT_OUT_DIR = "output"
DB_PATH = Path("progress.db")

LEVELS = ["beginner", "elementary", "intermediate", "upper-intermediate", "advanced"]

MAX_AGENT_ITERATIONS = 6   # safety cap on the reasoning loop

# ─────────────────────────────────────────────
# Database helpers
# ─────────────────────────────────────────────

def init_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS words (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT    NOT NULL,
            word        TEXT    NOT NULL,
            meaning     TEXT,
            level       TEXT,
            rating      INTEGER,          -- 1 (hard) .. 5 (easy), NULL until rated
            audio_file  TEXT,
            json_blob   TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    conn.commit()
    return conn


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def get_recent_words(conn: sqlite3.Connection, n: int = 30) -> list[str]:
    rows = conn.execute(
        "SELECT word FROM words ORDER BY id DESC LIMIT ?", (n,)
    ).fetchall()
    return [r["word"] for r in rows]


def get_average_rating(conn: sqlite3.Connection) -> float | None:
    row = conn.execute(
        "SELECT AVG(rating) as avg FROM words WHERE rating IS NOT NULL"
    ).fetchone()
    return row["avg"]


def save_word_record(
    conn: sqlite3.Connection,
    date: str,
    data: dict,
    level: str,
    audio_file: str | None,
) -> int:
    cur = conn.execute(
        "INSERT INTO words(date, word, meaning, level, audio_file, json_blob) "
        "VALUES(?,?,?,?,?,?)",
        (
            date,
            data.get("word", ""),
            data.get("meaning", ""),
            level,
            audio_file,
            json.dumps(data, ensure_ascii=False),
        ),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def update_rating(conn: sqlite3.Connection, row_id: int, rating: int) -> None:
    conn.execute("UPDATE words SET rating=? WHERE id=?", (rating, row_id))
    conn.commit()


def get_progress_summary(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) as n FROM words").fetchone()["n"]
    avg = get_average_rating(conn)
    by_level = conn.execute(
        "SELECT level, COUNT(*) as n FROM words GROUP BY level"
    ).fetchall()
    return {
        "total_words_learned": total,
        "average_difficulty_rating": round(avg, 2) if avg else None,
        "words_by_level": {r["level"]: r["n"] for r in by_level},
    }


# ─────────────────────────────────────────────
# Tool definitions (for the LLM)
# ─────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_learner_profile",
            "description": (
                "Returns the learner's current level, recent words studied, "
                "and average difficulty rating. Call this FIRST before deciding "
                "what word to teach."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_word_for_level",
            "description": (
                "Generate a Russian word/phrase appropriate for the given level. "
                "Avoids words the learner has recently seen. "
                "Returns a JSON object with word, meaning, pronunciation, examples."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "level": {
                        "type": "string",
                        "enum": LEVELS,
                        "description": "Target CEFR-inspired proficiency level.",
                    },
                    "topic_hint": {
                        "type": "string",
                        "description": (
                            "Optional thematic hint, e.g. 'food', 'travel', "
                            "'emotions'. Leave empty for a free choice."
                        ),
                    },
                },
                "required": ["level"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_dictionary",
            "description": (
                "Look up additional context for a Russian word: etymology, "
                "grammatical notes (gender, aspect), common collocations, "
                "register (formal/informal). Use to enrich the lesson."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "word": {
                        "type": "string",
                        "description": "Russian word to look up.",
                    }
                },
                "required": ["word"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_progress_summary",
            "description": (
                "Returns statistics: total words learned, average difficulty "
                "rating, breakdown by level. Use to decide if the learner "
                "should move up a level."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

# ─────────────────────────────────────────────
# Tool implementations (Python side)
# ─────────────────────────────────────────────

def tool_get_learner_profile(conn: sqlite3.Connection, level: str) -> dict:
    recent = get_recent_words(conn, 30)
    avg = get_average_rating(conn)
    return {
        "current_level": level,
        "recent_words": recent,
        "average_rating": round(avg, 2) if avg else None,
        "tip": (
            "If average_rating >= 4.0 and total words at this level >= 10, "
            "consider bumping the level up."
        ),
    }


def tool_generate_word(
    client: OpenAI,
    model: str,
    level: str,
    topic_hint: str,
    recent_words: list[str],
) -> dict:
    avoid = ", ".join(recent_words[:20]) if recent_words else "none"
    topic_str = f" Focus on the topic: {topic_hint}." if topic_hint else ""
    prompt = (
        f"Generate one Russian word or short phrase for a {level} learner.{topic_str}\n"
        f"Avoid these recently taught words: {avoid}.\n"
        "Return valid JSON only — no markdown, no code fences:\n"
        "{\n"
        '  "word": "Russian word with stress mark",\n'
        '  "meaning": "Short English meaning",\n'
        '  "pronunciation": "IPA or phonetic guide",\n'
        '  "examples": [\n'
        '    {"ru": "...", "en": "..."},\n'
        '    {"ru": "...", "en": "..."}\n'
        "  ]\n"
        "}"
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "You are a Russian language tutor. Output only valid JSON.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.85,
        max_tokens=500,
        response_format={"type": "json_object"},
    )
    content = resp.choices[0].message.content or ""
    return json.loads(content)


def tool_lookup_dictionary(client: OpenAI, model: str, word: str) -> dict:
    prompt = (
        f"Provide a concise dictionary entry for the Russian word «{word}». "
        "Return valid JSON only:\n"
        "{\n"
        '  "grammatical_info": "gender / verb aspect / etc.",\n'
        '  "etymology": "brief origin",\n'
        '  "collocations": ["example phrase 1", "example phrase 2"],\n'
        '  "register": "neutral / formal / informal / colloquial",\n'
        '  "memory_tip": "a short mnemonic hint for English speakers"\n'
        "}"
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "You are a Russian linguistics expert. Output only valid JSON.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.4,
        max_tokens=300,
        response_format={"type": "json_object"},
    )
    content = resp.choices[0].message.content or ""
    return json.loads(content)


# ─────────────────────────────────────────────
# Agent reasoning loop
# ─────────────────────────────────────────────

AGENT_SYSTEM_PROMPT = """You are an adaptive Russian language tutor agent.

Your job each session:
1. Call get_learner_profile to understand the learner's level and history.
2. Call generate_word_for_level with an appropriate level (and optional topic hint).
3. Call lookup_dictionary to enrich the lesson with grammar notes and a memory tip.
4. Optionally call get_progress_summary to decide if the level should increase.
5. When you have everything, output a final lesson as a JSON object with this schema:
{
  "word":             "Russian word with stress mark",
  "meaning":          "English meaning",
  "pronunciation":    "phonetic guide",
  "grammatical_info": "from dictionary lookup",
  "register":         "from dictionary lookup",
  "memory_tip":       "from dictionary lookup",
  "collocations":     ["...", "..."],
  "examples":         [{"ru": "...", "en": "..."}, {"ru": "...", "en": "..."}],
  "level_used":       "the level you chose",
  "suggest_level_up": true or false
}

Rules:
- Always call get_learner_profile first.
- Always call lookup_dictionary after generating the word.
- Output the final JSON as plain text (no markdown fences) when done.
- Do not ask the user questions — this is an automated pipeline.
"""


def run_agent(
    client: OpenAI,
    model: str,
    conn: sqlite3.Connection,
    level: str,
) -> dict:
    """
    Runs the multi-step tool-use loop.
    Returns the final enriched lesson dict.
    """
    messages: list[dict] = [{"role": "system", "content": AGENT_SYSTEM_PROMPT}]
    messages.append(
        {
            "role": "user",
            "content": (
                "Please prepare today's Russian lesson. "
                f"The learner's current level is: {level}."
            ),
        }
    )

    recent_words = get_recent_words(conn)

    for iteration in range(MAX_AGENT_ITERATIONS):
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.5,
            max_tokens=800,
        )

        msg = response.choices[0].message
        messages.append(msg.model_dump(exclude_unset=True))  # append assistant turn

        # If no tool calls → the agent produced its final answer
        if not msg.tool_calls:
            text = (msg.content or "").strip()
            # Strip any accidental markdown fences
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            return json.loads(text)

        # Handle each tool call
        for tc in msg.tool_calls:
            fn = tc.function.name
            args: dict[str, Any] = json.loads(tc.function.arguments or "{}")

            if fn == "get_learner_profile":
                result = tool_get_learner_profile(conn, level)

            elif fn == "generate_word_for_level":
                result = tool_generate_word(
                    client=client,
                    model=model,
                    level=args.get("level", level),
                    topic_hint=args.get("topic_hint", ""),
                    recent_words=recent_words,
                )

            elif fn == "lookup_dictionary":
                result = tool_lookup_dictionary(
                    client=client,
                    model=model,
                    word=args.get("word", ""),
                )

            elif fn == "get_progress_summary":
                result = get_progress_summary(conn)

            else:
                result = {"error": f"Unknown tool: {fn}"}

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

    raise RuntimeError("Agent did not produce a final answer within iteration limit.")


# ─────────────────────────────────────────────
# Output helpers (Anki, audio, text)
# ─────────────────────────────────────────────

def format_anki_back(data: dict, audio_basename: str | None) -> str:
    parts = [
        f"<b>Meaning</b>: {html.escape(str(data.get('meaning', '')))}",
        f"<b>Pronunciation</b>: {html.escape(str(data.get('pronunciation', '')))}",
        f"<b>Grammar</b>: {html.escape(str(data.get('grammatical_info', '')))}",
        f"<b>Register</b>: {html.escape(str(data.get('register', '')))}",
        f"<b>Memory tip</b>: {html.escape(str(data.get('memory_tip', '')))}",
        "<b>Examples</b>:",
    ]
    for ex in data.get("examples", []):
        ru = html.escape(str(ex.get("ru", "")))
        en = html.escape(str(ex.get("en", "")))
        parts.append(f"{ru}<br><i>{en}</i>")
    if audio_basename:
        parts.append(f"[sound:{audio_basename}]")
    return "<br><br>".join(parts)


def write_anki_files(out_dir: Path, today: str, front: str, back: str) -> tuple[Path, Path]:
    daily = out_dir / f"anki_import_{today}.txt"
    deck = out_dir / "anki_russian_deck.tsv"
    with daily.open("w", encoding="utf-8", newline="") as f:
        f.write("#separator:tab\n#html:true\n#columns:Front,Back\n")
        csv.writer(f, delimiter="\t", quoting=csv.QUOTE_MINIMAL).writerow([front, back])
    new_deck = not deck.exists()
    with deck.open("a", encoding="utf-8", newline="") as f:
        if new_deck:
            f.write("#separator:tab\n#html:true\n#columns:Front,Back\n")
        csv.writer(f, delimiter="\t", quoting=csv.QUOTE_MINIMAL).writerow([front, back])
    return daily, deck


def save_outputs(
    data: dict,
    out_dir: Path,
    with_audio: bool,
    with_anki: bool,
    today: str,
) -> tuple[Path, Path, Path | None, Path | None, Path | None]:
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / f"russian_{today}.json"
    txt_path = out_dir / f"russian_{today}.txt"

    with json_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    collocations = data.get("collocations", [])
    col_str = "  " + "\n  ".join(collocations) if collocations else "  —"

    lines = [
        "═" * 52,
        f"  🇷🇺  Today's Russian Word  ({data.get('level_used', '').upper()})",
        "═" * 52,
        f"  Word          : {data.get('word', '')}",
        f"  Meaning       : {data.get('meaning', '')}",
        f"  Pronunciation : {data.get('pronunciation', '')}",
        f"  Grammar       : {data.get('grammatical_info', '')}",
        f"  Register      : {data.get('register', '')}",
        f"  Memory tip    : {data.get('memory_tip', '')}",
        "",
        "  Collocations:",
        col_str,
        "",
        "  Examples:",
    ]
    for idx, ex in enumerate(data.get("examples", []), 1):
        lines.append(f"  {idx}. {ex.get('ru', '')}")
        lines.append(f"     → {ex.get('en', '')}")
    lines.append("")

    audio_path: Path | None = None
    if with_audio and data.get("word"):
        audio_path = out_dir / f"{today}_word.mp3"
        gTTS(text=data["word"], lang="ru").save(str(audio_path))
        lines.append(f"  🔊  Audio: {audio_path.name}")

    lines.append("═" * 52)
    with txt_path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    anki_daily: Path | None = None
    anki_deck: Path | None = None
    if with_anki:
        front = str(data.get("word", ""))
        back = format_anki_back(data, audio_path.name if audio_path else None)
        anki_daily, anki_deck = write_anki_files(out_dir, today, front, back)

    return txt_path, json_path, audio_path, anki_daily, anki_deck


# ─────────────────────────────────────────────
# Interactive CLI
# ─────────────────────────────────────────────

def cli_banner() -> None:
    print("\n" + "═" * 52)
    print("   🇷🇺  Russian Daily Tutor — AI Agent")
    print("═" * 52 + "\n")


def cli_ask_rating() -> int:
    """Prompt user to rate today's word difficulty 1–5."""
    while True:
        try:
            raw = input(
                "\n  How difficult was today's word?\n"
                "  [1 = very hard  2 = hard  3 = okay  4 = easy  5 = very easy]: "
            ).strip()
            val = int(raw)
            if 1 <= val <= 5:
                return val
        except (ValueError, EOFError):
            pass
        print("  Please enter a number between 1 and 5.")


def cli_ask_topic() -> str:
    """Optionally ask the user for a topic preference."""
    try:
        raw = input(
            "\n  Want to focus on a topic today? "
            "(e.g. food, travel, emotions — or press Enter to skip): "
        ).strip()
        return raw
    except EOFError:
        return ""


def cli_show_lesson(txt_path: Path) -> None:
    print("\n" + txt_path.read_text(encoding="utf-8"))


def cli_suggest_level_up(current: str) -> str:
    idx = LEVELS.index(current)
    if idx + 1 >= len(LEVELS):
        return current
    next_level = LEVELS[idx + 1]
    try:
        ans = input(
            f"\n  🎉  The agent suggests you're ready to move from "
            f"'{current}' → '{next_level}'.\n"
            f"  Accept? [y/N]: "
        ).strip().lower()
    except EOFError:
        ans = "n"
    return next_level if ans == "y" else current


def show_progress(conn: sqlite3.Connection) -> None:
    summary = get_progress_summary(conn)
    print("\n  📊  Your Progress")
    print(f"     Total words learned : {summary['total_words_learned']}")
    avg = summary["average_difficulty_rating"]
    print(f"     Average rating      : {avg if avg else 'n/a'} / 5.0")
    if summary["words_by_level"]:
        print("     By level:")
        for lvl, count in summary["words_by_level"].items():
            print(f"       {lvl:<20} {count} words")


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

def get_client() -> OpenAI:
    api_key = os.getenv("OXLO_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OXLO_API_KEY environment variable.")
    base_url = os.getenv("OXLO_BASE_URL", "https://api.oxlo.ai/v1")
    return OpenAI(base_url=base_url, api_key=api_key)


def main() -> None:
    parser = argparse.ArgumentParser(description="Russian Daily Tutor — AI Agent")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--no-audio", action="store_true")
    parser.add_argument("--no-anki", action="store_true")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Skip all prompts (for Task Scheduler / cron use)",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Show learning progress and exit",
    )
    args = parser.parse_args()

    conn = init_db()
    client = get_client()
    today = dt.date.today().isoformat()

    if args.progress:
        cli_banner()
        show_progress(conn)
        return

    # ── Interactive: greet + optional topic ──────────────────────────────────
    cli_banner()
    level = get_setting(conn, "level", LEVELS[0])
    print(f"  Welcome back! Your current level: {level.upper()}")

    topic_hint = ""
    if not args.non_interactive:
        topic_hint = cli_ask_topic()

    # ── Run the agent reasoning loop ─────────────────────────────────────────
    print("\n  🤖  Agent is preparing your lesson", end="", flush=True)

    # Patch topic into system if provided
    global AGENT_SYSTEM_PROMPT
    if topic_hint:
        AGENT_SYSTEM_PROMPT = AGENT_SYSTEM_PROMPT.replace(
            "Please prepare today's Russian lesson.",
            f"Please prepare today's Russian lesson. The learner wants to focus on: {topic_hint}.",
        )

    data = run_agent(client=client, model=args.model, conn=conn, level=level)
    level_used = data.get("level_used", level)
    suggest_up = data.get("suggest_level_up", False)

    print(" ✓\n")

    # ── Save outputs ─────────────────────────────────────────────────────────
    txt_path, json_path, audio_path, anki_daily, anki_deck = save_outputs(
        data=data,
        out_dir=Path(args.out_dir),
        with_audio=not args.no_audio,
        with_anki=not args.no_anki,
        today=today,
    )

    # ── Persist to DB ────────────────────────────────────────────────────────
    row_id = save_word_record(
        conn=conn,
        date=today,
        data=data,
        level=level_used,
        audio_file=audio_path.name if audio_path else None,
    )

    # ── Show lesson ──────────────────────────────────────────────────────────
    cli_show_lesson(txt_path)

    # ── Interactive: collect rating ──────────────────────────────────────────
    if not args.non_interactive:
        rating = cli_ask_rating()
        update_rating(conn, row_id, rating)
        print(f"\n  ✅  Rating saved ({rating}/5). See you tomorrow!")

        if suggest_up:
            new_level = cli_suggest_level_up(level)
            if new_level != level:
                set_setting(conn, "level", new_level)
                print(f"\n  🚀  Level updated to: {new_level.upper()}")
    else:
        print("\n  ✅  Lesson saved (non-interactive mode).")

    # ── Show file paths ──────────────────────────────────────────────────────
    print(f"\n  📁  Files saved in: {Path(args.out_dir).resolve()}")
    if anki_daily:
        print(f"     Anki (today)  : {anki_daily.name}")
    if anki_deck:
        print(f"     Anki (deck)   : {anki_deck.name}")
    if audio_path:
        print(f"     Audio         : {audio_path.name}")

    show_progress(conn)
    print()


if __name__ == "__main__":
    main()
