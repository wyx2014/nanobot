# Memory in nanobot

nanobot's memory is built on a simple belief: memory should feel alive, but it should not feel chaotic.

Good memory is not a pile of notes. It is a quiet system of attention. It notices what is worth keeping, lets go of what no longer needs the spotlight, and turns lived experience into something calm, durable, and useful.

That is the shape of memory in nanobot.

## The Design

nanobot does not treat memory as one giant file.

It separates conversation continuity from user personalization, because those are different kinds of remembering:

- `session.messages` holds the living short-term conversation.
- A session-local summary preserves older context when a conversation is compacted.
- `memory/history.jsonl` is the append-only staging queue for unreviewed user-profile candidates (plus legacy archive entries).
- `SOUL.md` and `USER.md` are the durable global personalization files.
- `memory/MEMORY.md` remains readable/restorable for legacy compatibility, but is not injected into normal chats and no longer receives new project/session facts.
- `GitStore` records how those durable files change over time.

This keeps the system light in the moment, but reflective over time.

## The Flow

Memory moves through nanobot in two stages.

### Stage 1: Candidate capture and session compaction

Every ordinary or project chat can contribute profile evidence. When a direct user message is
persisted, nanobot writes an unreviewed `user_profile_candidate` record to
`memory/history.jsonl`. This immediate path does not wait for a long context window or an idle chat.

Only the user's own message is captured. Assistant answers, tool results, subagent output,
scheduled jobs, slash commands, structured interactive-prompt answers, and synthetic delivery
events are excluded. Expert-team turns carry an explicit
source tag so Dream can reject the team's roles, workflow, report structure, and rubric while still
allowing a user-authored identity fact or explicitly global preference.

Separately, when a conversation grows large enough to pressure the context window, the
`Consolidator` summarizes the oldest safe slice into session metadata. That summary exists only to
preserve continuity in the same conversation; it is not global user memory. Idle compaction also
backfills candidates from older sessions created before immediate capture existed.

The candidate queue is:

- append-only
- cursor-based
- excluded from normal runtime history injection
- reviewed by Dream before anything reaches durable personalization

Each line is a JSON object:

```json
{"cursor": 42, "timestamp": "2026-08-24 10:02", "content": "[source: user-profile-candidate]\n[2026-08-24T10:02:00] USER: I prefer concise replies in every conversation.", "kind": "user_profile_candidate", "source": "user", "session_key": "websocket:chat-1"}
```

It is not final memory. Project facts and one-off instructions may appear in a direct user message,
but Dream must discard them rather than promote them to `USER.md`, `SOUL.md`, or a reusable skill.

### Stage 2: Dream

`Dream` is the slower, more thoughtful layer. It runs on a cron schedule by default and can also be triggered manually.

Dream reads:

- new entries from `memory/history.jsonl`
- the current `SOUL.md`
- the current `USER.md`
- the current `memory/MEMORY.md`

Then it edits the durable personalization files surgically in a single pass — not by rewriting
everything, but by making the smallest honest change that keeps memory coherent. Only direct-user
evidence can establish a profile fact. Dream's tools can read but cannot write legacy `MEMORY.md`.

This is why nanobot's memory is not just archival. It is interpretive.

## The Files

```text
workspace/
├── SOUL.md              # The bot's long-term voice and communication style
├── USER.md              # Stable knowledge about the user
└── memory/
    ├── MEMORY.md        # Legacy compatibility; no new project/session facts
    ├── history.jsonl    # Append-only candidate queue and legacy entries
    ├── .cursor          # Consolidator write cursor
    ├── .dream_cursor    # Dream consumption cursor
    └── .git/            # Version history for long-term memory files
```

These files play different roles:

- `SOUL.md` remembers how nanobot should sound.
- `USER.md` remembers who the user is and what they prefer.
- `MEMORY.md` preserves older installations' curated content for compatibility.
- `history.jsonl` holds evidence awaiting Dream review, not confirmed user facts.

Project knowledge stays with the project: in project files, workspace instructions, and the active
session's own continuity summary.

Every project loads the same global `USER.md` and `SOUL.md`. A project's own `AGENTS.md` remains
project-scoped; project-local `USER.md` or `SOUL.md` files are not treated as separate profiles.

## Why `history.jsonl`

The old `HISTORY.md` format was pleasant for casual reading, but it was too fragile as an operational substrate.

`history.jsonl` gives nanobot:

- stable incremental cursors
- safer machine parsing
- easier batching
- cleaner migration and compaction
- a better boundary between raw history and curated knowledge

You can still search it with familiar tools:

```bash
# grep
grep -i "keyword" memory/history.jsonl

# jq
cat memory/history.jsonl | jq -r 'select(.content | test("keyword"; "i")) | .content' | tail -20

# Python
python -c "import json; [print(json.loads(l).get('content','')) for l in open('memory/history.jsonl','r',encoding='utf-8') if l.strip() and 'keyword' in l.lower()][-20:]"
```

The difference is philosophical as much as technical:

- `history.jsonl` is an evidence queue
- `USER.md` and `SOUL.md` are curated global personalization
- project and expert-team process state is not user memory

## Commands

Memory is not hidden behind the curtain. Users can inspect and guide it.

| Command | What it does |
|---------|--------------|
| `/dream` | Run Dream immediately |
| `/dream-log` | Show the latest Dream memory change |
| `/dream-log <sha>` | Show a specific Dream change |
| `/dream-restore` | List recent Dream memory versions |
| `/dream-restore <sha>` | Restore memory to the state before a specific change |

These commands exist for a reason: automatic memory is powerful, but users should always retain the right to inspect, understand, and restore it.

## Versioned Memory

After Dream changes long-term memory files, nanobot can record that change with `GitStore`.

This gives memory a history of its own:

- you can inspect what changed
- you can compare versions
- you can restore a previous state

That turns memory from a silent mutation into an auditable process.

## Configuration

Dream is configured under `agents.defaults.dream`:

```json
{
  "agents": {
    "defaults": {
      "dream": {
        "intervalH": 2,
        "modelOverride": null,
        "maxBatchSize": 20,
        "maxIterations": 10
      }
    }
  }
}
```

| Field | Meaning |
|-------|---------|
| `intervalH` | How often Dream runs, in hours |
| `cron` | Cron expression override (takes precedence over `intervalH`) |
| `modelOverride` | Optional Dream-specific model override *(pending implementation)* |
| `maxBatchSize` | *(Deprecated — not used)* |
| `maxIterations` | *(Deprecated — not used)* |

In practical terms:

- `intervalH` is the normal way to configure Dream frequency. Internally it runs as an `every` schedule.
- `cron` overrides `intervalH` when set, allowing precise cron expressions (e.g. `0 */4 * * *`).
- `modelOverride` is reserved for a future release. Currently Dream uses the same model as the main agent.
- `maxBatchSize` and `maxIterations` are preserved for config compatibility but no longer affect behavior.

## In Practice

What this means in daily use is simple:

- conversations can stay fast without carrying infinite context
- short and project chats can still contribute legitimate user-profile evidence
- expert-team mechanics and project facts do not become global preferences
- durable user facts can become clearer over time instead of noisier
- the user can inspect and restore memory when needed

Memory should not feel like a dump. It should feel like continuity.

That is what this design is trying to protect.
