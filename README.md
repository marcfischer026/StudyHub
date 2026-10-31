# UniVerse

A local-first university learning workspace for Apple Silicon Macs. Organise any number of subjects and lectures, record and transcribe lectures offline with whisper.cpp (Metal), take timestamped notes linked to the audio, search everything semantically with LanceDB, and round-trip your material through Claude to get professor-tailored quizzes and analysis — without any API keys. After the one-time setup, everything runs offline and lives in `~/.universe_data`.

## Features

- **One-click recording**: the red **● Record** button in the toolbar always works. With no lecture open, UniVerse files the recording under a new dated lecture (and asks which course it belongs to if needed). Pick the microphone from the 🎙 menu; if the mic delivers silence, a warning explains how to fix it.
- **Unlimited subjects and lectures**, managed from the Subjects sidebar. Selecting a subject shows an overview of its lectures.
- **Readable recording names**: subject abbreviation, date and time, e.g. `BWL 2026-10-28 15.30.wav`. The abbreviation is derived from the subject name ("Einführung ins BWL" → BWL, "Corporate Finance" → CF); change it via right-click on the subject → *Abbreviation…*.
- **Professor profiles**: testing style, focus areas, question types, grading and style notes. Set a default per subject, override per lecture. Used to tailor the Claude prompt.
- **Notes in the centre**: rich-text editor saved as Markdown with autosave, the lecture header (subject, professor) above it and the audio player (`pygame.mixer`, ±10 s, slider) below it.
- **Dockable panels** (`QDockWidget`) around it — drag, float, tab or close them. Closed panels come back from the **▦ Panels** button (top right of the toolbar), the *View* menu, or `⌘1`–`⌘5`; *Reset Layout* in the same menu restores the default:
  - **Subjects**: subject/lecture tree.
  - **Documents**: every PDF of the open lecture and of its subject in one list. Each has coloured tags: its type (**Slides**, **Past exam**, **Task**) and your own. Right-click a document or use *🏷 Tags* to add or remove tags, create new ones (a colour alone is enough) and change colours; click tags above the list to filter (several narrow it down). Import asks for the type: slides belong to the open lecture, past exams and tasks to the whole subject, and show up under every lecture of it. Changing a tag never moves the file. Below the list is the PDF viewer (PyMuPDF). Scroll continuously through all slides (trackpad, ↑/↓), flip with ←/→ or ◀ ▶, zoom with − / + / Fit (Fit follows the panel width). Drag PDFs onto the panel to import. Drag across slide text to select it and copy it (⌘C or right-click → *Copy* / *Copy page text*); the **Mark** tool highlights text or any area in yellow, green, blue or pink, saved into the PDF (right-click a mark to remove it).
  - **Transcript**: whisper.cpp segments; click a line to play from there.
  - **AI Tutor**: Claude export/import, interactive Quiz, Analysis, and Search tabs.
  - **Recordings**: the lecture's audio files (`sounddevice` records 16 kHz mono WAV), import, transcription, auto-transcribe switch.
- **Linked timestamps**: `⌘T` inserts `[▶ HH:MM:SS]` at the current recording time or playback position; clicking a stamp plays the audio from that point.
- **Offline transcription** with whisper.cpp, GPU-accelerated via Metal.
- **Semantic search** per subject over transcripts, slide pages and notes (LanceDB).
- **Claude round trip**: export a lecture as `Context.md`, paste Claude's JSON reply back, get interactive quizzes and an analysis.
- **Optional cloud fine-tuning** of an open-source model on a subject via Modal.

## Requirements

- macOS on Apple Silicon (M1 or newer)
- Python 3.11+
- Homebrew (for whisper.cpp)

## Installation

```bash
git clone https://github.com/marcfischer026/StudyHub.git && cd StudyHub
```

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

```bash
git config core.hooksPath .githooks
```

UniVerse uses [`pygame-ce`](https://pyga.me), the maintained drop-in fork of pygame (same `import pygame`, same `pygame.mixer` API), because classic `pygame` has no wheels for the newest Python releases. On Python ≤ 3.12 you can swap in `pygame` if you prefer.

### Speech-to-text setup (one time)

```bash
brew install whisper-cpp
```

```bash
mkdir -p ~/.universe_data/models && curl -L -o ~/.universe_data/models/ggml-large-v3-turbo-q5_0.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin
```

`large-v3-turbo-q5_0` (547 MB) is multilingual and handles lectures that mix German and English well; Homebrew's build runs it on the GPU via Metal. Avoid models ending in `.en` unless every lecture is in English — they cannot transcribe other languages.

UniVerse uses the best model it finds in `~/.universe_data/models/` automatically. The **Recordings** panel lets you pick a specific model and set the spoken language **per subject** (Automatic, Deutsch, English, …). For mixed-language lectures choose the main language — terms in the other language are still recognised. The subject name is passed to whisper as a prompt to help with subject vocabulary, and non-speech tags such as `[INDISTINCT CHATTER]` are dropped.

Quiet recordings are raised to a normal volume automatically when you stop recording (up to +18 dB).

All of these can be changed in the Settings dialog (`⌘,`, under *Preferences…* in the app menu); they are stored in `~/.universe_data/settings.json`:

| Key | Default | Meaning |
|---|---|---|
| `whisper_binary` | `whisper-cli` on `PATH` | path to the whisper.cpp CLI |
| `whisper_model` | `""` (best installed) | file in `~/.universe_data/models/` or an absolute path |
| `whisper_language` | `auto` | default language for subjects without their own setting |
| `whisper_threads` | `8` | CPU threads |
| `battery_pause_below` | `20` | pause background AI work on battery below this percent (`0` = never) |
| `only_when_charging` | `false` | run background AI work only while plugged in |
| `local_llm_unload_after` | `300` | seconds without AI work before UniVerse unloads the model it used (also sent as LM Studio `ttl`); `0` = keep loaded |
| `local_classify` | `true` | sort new transcripts with a local AI server, if one is running |
| `local_llm_url` | `""` (probe 1234, 8080, 11434) | OpenAI-compatible base URL, e.g. `http://localhost:1234/v1` |
| `local_llm_model` | `""` (first chat model) | model id on that server |

*Tools → Check Transcription Setup* tells you whether everything is in place. Non-WAV audio (MP3, M4A, …) is converted with macOS's built-in `afconvert` before transcription.

## Running

```bash
.venv/bin/python -m universe
```

To try it out with a throwaway workspace, set `UNIVERSE_DATA_DIR` to another folder.

## Workflow

1. Press **● Record** (`⌘R`) when class starts — or create a **+ Subject** and **+ Lecture** first if you prefer.
2. Take notes. Press `⌘T` (or **⏱ Timestamp**) to drop a `[▶ HH:MM:SS]` stamp at the current moment.
3. Press **■ Stop**. whisper.cpp transcribes in the background and the Transcript panel fills in.
4. Optional: **Import PDF…** in the Documents panel; click *Assign a professor* under the lecture title.
5. Rename the auto-named lecture any time (right-click it in the sidebar).
6. **⇪ Export for Claude** (`⌘E`) writes `Context.md` and copies it to the clipboard. Paste it into Claude.
7. Copy Claude's reply, click **⇩ Import reply**, paste, press **Import AI Response**.
8. Practise in the **Quiz** tab, read the **Analysis** tab, and use **Search** to find where a topic came up.

## Keyboard shortcuts

| Shortcut | Action |
|---|---|
| `⌘T` | Insert timestamp |
| `⌘R` | Start / stop recording |
| `⌘⇧Space` | Play / pause |
| `⌘E` | Export for Claude |
| `⌘N` / `⌘⇧N` | New lecture / new subject |
| `⌘⇧I` | Import a document (PDF) |
| `⌘S` | Save notes (autosave also runs) |
| `⌘1` … `⌘5` | Show / hide Subjects, Documents, Transcript, AI Tutor, Recordings |
| `⌘B` / `⌘I` | Bold / italic |

## Claude round trip

`Export for Claude` builds `Lectures/<Lecture>/ai/Context.md` containing the professor profile, your notes, the timestamped transcript, the text of every document with its type and tags, and instructions telling Claude to answer with exactly one JSON block. You choose the number of quiz questions.

The importer accepts the reply with or without `` ``` `` fences or surrounding prose, extracts the JSON and validates it: `summary`, `key_concepts`, `exam_focus`, `knowledge_gaps`, `quizzes` (`multiple_choice`, `true_false`, `short_answer`, each with answer, explanation and a slide/timestamp `source_ref`) and `flashcards`. If something is off, it lists exactly what — nothing is saved until the reply is valid.

Valid replies are saved as `ai/response_YYYYMMDD_HHMMSS.json` and `ai/latest.json`. Short answers in the Quiz tab are self-graded; finished attempts are appended to `ai/quiz_attempts.json`.

## Lecture content vs. side talk

A laptop microphone also catches what your neighbours say. UniVerse sorts every transcript line into **lecture content**, **question**, **answer** or **private / off-topic**, and flags **exam-relevant** statements. It does this in three stages:

1. **Exam cues (instant, offline).** Right after transcription, lines such as "das kommt in der Klausur dran", "prüfungsrelevant", "merken Sie sich", "this will be on the exam" are marked ★.
2. **Local AI (automatic, on this Mac).** If a local OpenAI-compatible server is running (LM Studio, Ollama, `mlx_lm.server`), each new transcript is sorted by it in the background. The slide text tells it what the course is about. Ports 1234, 8080 and 11434 are tried automatically; set `local_llm_url` / `local_llm_model` in `settings.json` to choose. *Transcript → Sort with local AI* runs it again.
3. **Claude.** *Export for Claude* numbers each transcript line (`[S1.12 ▶ 00:01:05]`) and asks Claude for `segment_labels`; *Import AI Response* applies them.

In the **Transcript** panel exam-relevant lines are highlighted ★, questions ❓ and answers 💬 tinted, private talk 🔇 greyed. Filter by *Exam-relevant*, *Questions & answers*, *Hide private talk* … and right-click (several lines at once) to correct a label — your corrections are never overwritten. **Private talk is left out of the Claude export, the search index and the training data;** exam-relevant lines are listed first in the export.

### Live check while recording

You can't tell how the laptop mic hears the room compared to your ears. While you record, a status next to the level meter tells you, so you can ask the lecturer to speak up (or move closer) before the lecture is lost:

| Status | Meaning |
|---|---|
| 🟢 Lecturer clear | the lecturer's voice stands ≥ 12 dB above the room background (speech band 300–3400 Hz) |
| 🟡 Lecturer okay — a bit quiet | 9–12 dB: transcription usually still works, but it's borderline |
| 🔴 Lecturer too quiet — ask to speak up | < 9 dB: whisper starts getting most words wrong |
| 🟠 Neighbours are louder than the lecturer | other voices are ≥ 6 dB louder than the lecturer |
| ⚪ Lecturer not heard recently / No speech stands out from the noise | nothing usable in the last 20 s |

Click the status to see **what the computer heard**. Every 45 s to 3 min, whisper transcribes the last 15 seconds so you can compare its text with what you heard. Checks run more often while the sound is doubtful and less often while everything is clear. Text recorded while no clear speech stood out is marked *probably made up*, because whisper invents phrases such as "Vielen Dank." from pure noise. For the same reason no "% understood" figure is shown: on noise, whisper reported 93–99 % confidence for invented text. Telling the lecturer apart from neighbours needs the speaker model (next section); without it the status rates any speech.

**Battery:** loudness and voice tracking use under 1 % of one CPU core. Only the whisper check uses the GPU, for about 3 s per check. It pauses under the same battery rules as background AI work (`battery_pause_below`, `only_when_charging`). Set `live_check` to `false` in `settings.json` to turn the text check off; the coloured status keeps working.

The thresholds were calibrated on 11 chunks of a real lecture recorded with a laptop mic, with steady background noise added. All clean chunks, which whisper transcribed correctly, measured 12–20 dB. Of the chunks where whisper got most words wrong, all but one measured ≤ 9 dB or showed no speech at all; the exception contained a loud nearby voice.

### Speaker recognition (optional)

The classifier works much better when it knows **who** spoke. After transcription UniVerse measures each line's loudness and, if a small voice model is installed, groups the lines by voice. The voice that talks most is the **lecturer** 🎓; others are *Speaker 2*, *Speaker 3* … 👥. Both the local AI and the Claude export see these tags (`S1.4 [Lecturer] …`, `S1.9 [Speaker 2, near mic] …`). The tags are only hints: a line counts as private only if its *wording* is unrelated to the course, and when in doubt it is kept as lecture content, because losing course material is worse than keeping some chatter. *Near mic* marks lines much louder than the rest of the recording, which usually means a student next to the laptop rather than the distant lecturer. Everything runs offline. Without the model you only get loudness, the same way sorting is skipped when no local AI server is running.

```bash
.venv/bin/pip install sherpa-onnx
```

```bash
curl -L -o ~/.universe_data/models/nemo_en_titanet_small.onnx https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_small.onnx
```

`sherpa-onnx` ships Apple Silicon wheels with ONNX Runtime built in (no PyTorch, no Hugging Face token). It handles roughly 20 minutes of audio in 5 seconds. Older transcripts get speakers the next time you press *Sort with local AI*. In the Transcript panel, filter by *Lecturer only* / *Other speakers*. To fix mistakes, right-click a line and pick *The lecturer said this*, *Someone else said this*, or *Speaker N is the lecturer (whole recording)*.

| Key | Default | Meaning |
|---|---|---|
| `speaker_model` | `""` (best `.onnx` found) | speaker-embedding model in `~/.universe_data/models/` or an absolute path |
| `speaker_threshold` | `0.2` | cosine similarity above which two lines count as the same voice; raise it if different people get merged, lower it if one person is split into several speakers |

Model choice was measured on 20 minutes of a German maths lecture (310 lines) mixed with 6 minutes of two students chatting next to the laptop (62 lines). The table shows how much speaking time ended up labelled *lecturer*:

| Model | Lecturer kept | Chatter labelled lecturer |
|---|---|---|
| NeMo TitaNet-small, threshold 0.2 | 97.9 % | 0 % |
| 3D-Speaker ERes2Net, threshold 0.2 | 99.8 % | 6.4 % |
| WeSpeaker ResNet34, any threshold | — | could not separate the voices |

The chatter came from separate recordings, so room and distance differed more than they would inside one lecture. Treat these numbers as an upper bound. In a recording with no lecturer at all, "lecturer" simply means whoever talks most.

## Background AI work and battery

Transcription (whisper.cpp) and sorting (local AI) run in one queue, one job at a time, so they never fight over the GPU. The field at the bottom right of the window shows what is running, its progress, CPU / GPU load and the battery, e.g. `⚙ Sorting BWL 2026-10-28 15.30 · 37% · CPU 12% GPU 88% · 🔋57%`.

- **⏸ / ▶** next to it pauses or resumes everything. Pausing is real: whisper.cpp is frozen by the operating system (SIGSTOP) and continues exactly where it stopped; local-AI sorting stops after the current batch of up to 40 lines.
- **Click the field** for details: current job with elapsed time and estimate, queue, *Cancel*, CPU / GPU / RAM of the whole Mac, the memory held by models on the GPU, UniVerse / whisper.cpp / local AI server usage, and the last finished jobs.
- **LM Studio models** (same popup): every loaded model instance with its size and whether LM Studio will unload it by itself (`auto-unload in 4:12`) or keeps it forever (`stays loaded (no timer)` — typical for models loaded by hand in LM Studio). *Unload* / *Unload all* free the memory; the status field shows the total as `🧠 8.4 GB`.
- **Models go back to standby.** LM Studio only auto-unloads models it loaded on demand (JIT). UniVerse therefore sends a `ttl` with every request (LM Studio then unloads a model it loaded for UniVerse after that idle time) and additionally unloads the model it used once there has been no AI work for *5 min* (adjustable, or off). Other apps that need the model again get it reloaded on demand by LM Studio (a few seconds). Models UniVerse never used are only unloaded when you click *Unload*.
- **Battery rules** (in the same popup, saved in `settings.json`): *Pause on battery below 20 %* (default on) and *Only run while charging*. Work resumes by itself when you plug in; a pause you started by hand is never undone automatically.

## Search

Each subject has its own LanceDB store in `<Subject>/.index`, covering transcripts (in time windows), PDF pages and notes. A lecture is re-indexed automatically after a transcription, after a PDF import and when you leave it with changed notes; *Tools → Rebuild Search Index* rebuilds the whole subject. Double-click a result to jump to the slide page or the audio moment.

Embeddings are computed locally. By default UniVerse uses a built-in hashing embedder (keyword-level, zero downloads). Install `sentence-transformers` to switch to `all-MiniLM-L6-v2` for real semantic matching — the model downloads once and then works offline; rebuild the index after switching.

## API keys & secrets

UniVerse needs **no API key**: Claude is used by copy & paste, transcription and sorting run locally. If you add keys later (e.g. a Hugging Face token for training), keep them out of git:

- **Where keys go:** copy `.env.example` to `.env` and fill it in. `.env`, `*.pem`, `*.key`, `credentials*.json`, `.modal.toml` and `secrets/` are git-ignored. For Modal, prefer `modal secret create huggingface HF_TOKEN=hf_...` — then the key never touches a file. On your Mac, the Keychain is the other safe place.
- **Safety net:** a pre-commit hook in `.githooks/` blocks commits that contain anything that looks like a key (Anthropic, OpenAI, GitHub, Hugging Face, AWS, Google, Slack tokens, private keys, `api_key = "…"`) or a `.env`-style file. Turn it on once per clone:

```bash
git config core.hooksPath .githooks
```

- **If a key was pushed anyway:** revoke / rotate it at the provider first — deleting the commit does not make it secret again.
- Your recordings, notes and transcripts live in `~/.universe_data`, outside the repository, and are never committed.

## Data layout

```
~/.universe_data/
├── settings.json
├── models/ggml-base.en.bin
├── Professors/<id>.json
└── Subjects/<Subject>/
    ├── subject.json            name, description, default professor, tag colours
    ├── .index/                 LanceDB vector store
    ├── exams/                  PDFs imported as past exams + tags.json
    ├── exercises/              PDFs imported as tasks + tags.json
    └── Lectures/<Lecture>/
        ├── lecture.json        name, professor override
        ├── notes.md
        ├── audio/              recordings ("BWL 2026-10-28 15.30.wav") + imported audio
        ├── slides/             PDFs imported as slides + tags.json
        ├── transcripts/        <audio>.json (segments) + <audio>.txt
        └── ai/                 Context.md, response_*.json, latest.json, quiz_attempts.json
```

Everything is plain files — back it up, sync it, or edit it by hand.

## Cloud fine-tuning (optional, Modal)

The only part of UniVerse that uses the internet, and only when you run it. It turns a subject's transcripts, notes, imported quizzes and flashcards into a chat dataset and LoRA-fine-tunes an open model on a Modal A10G GPU.

```bash
.venv/bin/pip install -r modal_training/requirements.txt
```

```bash
.venv/bin/modal setup
```

```bash
.venv/bin/python modal_training/dataset.py --subject "Corporate Finance"
```

```bash
.venv/bin/modal run modal_training/train_model.py --subject "Corporate Finance"
```

```bash
.venv/bin/modal volume get universe-training "Corporate Finance/adapter" ./adapter
```

The first command previews the dataset locally (at least 20 examples are required to train). Options: `--base-model` (default `Qwen/Qwen2.5-1.5B-Instruct`), `--epochs` (2), `--learning-rate` (2e-4), `--lora-rank` (16).

## Project structure

```
universe/
├── __main__.py            entry point (python -m universe)
├── config.py              data root + settings.json
├── storage.py             subjects, lectures, folder layout
├── professors.py          professor profiles
├── services/              no Qt imports — usable from scripts and tests
│   ├── audio_recorder.py  sounddevice → 16 kHz WAV
│   ├── audio_player.py    pygame.mixer playback with seek
│   ├── transcriber.py     whisper.cpp CLI wrapper
│   ├── speakers.py        loudness + offline speaker recognition (sherpa-onnx)
│   ├── live_check.py      live "can the mic hear the lecturer?" status while recording
│   ├── timestamps.py      [▶ HH:MM:SS] helpers
│   ├── pdf_tools.py       PyMuPDF text extraction
│   ├── vector_index.py    LanceDB per-subject index
│   ├── claude_bridge.py   Context.md export, JSON import/validation
│   ├── relevance.py       lecture vs. side talk, exam cues, segment labels
│   ├── local_llm.py       sorting with a local OpenAI-compatible server
│   ├── lmstudio.py        loaded LM Studio instances, unload
│   ├── jobcontrol.py      pause / resume / cancel (SIGSTOP for whisper.cpp)
│   └── system_monitor.py  CPU, GPU, RAM, battery
└── ui/
    ├── main_window.py     toolbar, docks, menus, wiring
    ├── style.py           stylesheet (light + dark)
    ├── welcome.py         welcome / subject overview, lecture view
    ├── audio.py           audio controller, Record bar, player bar, Recordings panel
    ├── sidebar.py         subject/lecture tree
    ├── professor_dialog.py
    ├── pdf_viewer.py
    ├── note_editor.py
    ├── transcript_panel.py
    ├── ai_panel.py        Claude / Quiz / Analysis / Search
    ├── jobs.py            job queue, activity indicator, battery rules
    └── workers.py         background threads
modal_training/
├── train_model.py         Modal app (LoRA via TRL)
├── dataset.py             local dataset builder
└── requirements.txt
tests/                     .venv/bin/python -m unittest discover tests
```

## Troubleshooting

- **Recording is silent / fails** — pick another microphone in the 🎙 menu next to Record (an iPhone or headset mic may be the system default without being nearby). macOS also asks for microphone access the first time; if it was declined, allow the app you launch UniVerse from (Terminal, iTerm, VS Code, Claude) under *System Settings → Privacy & Security → Microphone* — *Audio → Microphone Permissions…* opens that page.
- **"whisper.cpp binary not found"** — `brew install whisper-cpp`, or set `whisper_binary` in `settings.json`.
- **Seeking in some MP3/M4A files restarts from the beginning** — a codec limitation of pygame's mixer. Recordings made in UniVerse are WAV and seek fine.
- **Search finds nothing** — *Tools → Rebuild Search Index*.
