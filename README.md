# 🇷🇺 Russian Daily Tutor — AI Agent

An adaptive Russian language tutor that runs as a **multi-step AI agent**. Each session the LLM:

1. **Checks your profile** — current level, recent words, average difficulty
2. **Picks the right word** — avoids repeats, matches your level, respects topic hints
3. **Enriches the lesson** — calls a dictionary tool for grammar notes, etymology, collocations, a memory tip
4. **Decides if you should level up** — based on your ratings over time

Progress is stored in a local **SQLite database** (`progress.db`). Anki import files and MP3 audio are generated as before.

---

## 1) Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 2) Configure API key

**Option A — session only (PowerShell)**
```powershell
$env:OXLO_API_KEY = "YOUR_API_KEY"
$env:OXLO_BASE_URL = "https://api.oxlo.ai/v1"   # optional, this is the default
```

**Option B — for scheduled runs**

Copy `local_env.bat.example` → `local_env.bat`, add your key. `run_daily.bat` loads it automatically.

---

## 3) Run

### Interactive mode (recommended)
```powershell
python main.py
```
The agent will:
- Ask if you want to focus on a topic (e.g. "food", "travel")
- Show the enriched lesson
- Ask you to rate difficulty (1–5)
- Suggest a level-up if your ratings are consistently high

### Non-interactive (for Task Scheduler / cron)
```powershell
python main.py --non-interactive
```

### View your progress dashboard
```powershell
python main.py --progress
```

### Other flags
```powershell
python main.py --model deepseek-r1-8b --out-dir output --no-audio --no-anki
```

---

## 4) Levels

The agent tracks 5 levels (stored in `progress.db`):

| Level | Description |
|-------|-------------|
| `beginner` | Core vocabulary, greetings, numbers |
| `elementary` | Everyday phrases, basic grammar |
| `intermediate` | Broader vocabulary, aspect, cases |
| `upper-intermediate` | Idiomatic usage, nuanced meaning |
| `advanced` | Literary words, complex collocations |

You start at `beginner`. After rating words ≥ 4/5 consistently, the agent will suggest moving up.

---

## 5) What's in each lesson

| Field | Source |
|-------|--------|
| Word + stress mark | `generate_word_for_level` tool |
| Meaning + pronunciation | `generate_word_for_level` tool |
| Grammatical info (gender, aspect) | `lookup_dictionary` tool |
| Register (formal/informal) | `lookup_dictionary` tool |
| Memory tip (mnemonic) | `lookup_dictionary` tool |
| Collocations | `lookup_dictionary` tool |
| 2 example sentences | `generate_word_for_level` tool |
| MP3 audio | gTTS (Google Text-to-Speech) |

---

## 6) Anki import

| File | Purpose |
|------|---------|
| `output/anki_import_YYYY-MM-DD.txt` | Single-day import |
| `output/anki_russian_deck.tsv` | Cumulative deck (one row per run) |
| `output/YYYY-MM-DD_word.mp3` | Pronunciation audio |

**Import steps:**
1. File → Import → choose the `.txt` or `.tsv` file
2. Map columns to **Front** / **Back**
3. Enable **Allow HTML in fields**
4. Anki copies the `[sound:…]` MP3 from the same folder

The Back field now includes grammar notes, register, memory tip, and collocations — richer than v1.

---

## 7) Daily auto-run (Windows Task Scheduler)

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
.\Register-ScheduledTask.ps1
```

Or manually:
```powershell
schtasks /Create /TN "RussianDailyTutor" /TR "C:\path\to\run_daily.bat" /SC DAILY /ST 08:00 /F
```

Use `--non-interactive` in `run_daily.bat` for unattended runs.

---

## 8) Agent architecture

```
main.py
  └── run_agent()                  ← reasoning loop (up to 6 iterations)
        ├── Tool: get_learner_profile     → reads SQLite
        ├── Tool: generate_word_for_level → calls LLM
        ├── Tool: lookup_dictionary       → calls LLM
        └── Tool: get_progress_summary    → reads SQLite

  └── save_outputs()               ← .txt / .json / .mp3 / Anki files
  └── SQLite (progress.db)         ← words, ratings, level setting
```

The LLM decides **which tools to call and in what order** — you can watch it reason by checking the intermediate tool calls in the code.

---

## 9) Output JSON schema

```json
{
  "word":             "Russian word with stress mark",
  "meaning":          "English meaning",
  "pronunciation":    "phonetic guide",
  "grammatical_info": "e.g. neuter noun, imperfective verb",
  "register":         "neutral / formal / colloquial",
  "memory_tip":       "mnemonic hint for English speakers",
  "collocations":     ["phrase 1", "phrase 2"],
  "examples":         [{"ru": "...", "en": "..."}, {"ru": "...", "en": "..."}],
  "level_used":       "intermediate",
  "suggest_level_up": false
}
```
