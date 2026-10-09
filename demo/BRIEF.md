# Brief: one fictional user and their chats with a personal assistant

You are writing demo data for Hindsight Lens, a dashboard that shows what an AI
agent's memory (Hindsight) believes about each user and how that changes. Write
ONE JSON file describing a fictional person and the multi-week chat they have
with a general personal assistant. Everything is fiction: never use a real
person, a real company or a real brand.

The file is replayed through a real Hindsight server with a real model: each
session is retained as a transcript, Hindsight extracts the facts and forms and
rewrites its beliefs on its own. So the conversation is all you write. What
makes a good demo is a life that **changes**: plans that move, preferences that
flip, things that stop being true, details that pile up, so memory has to learn,
rewrite and forget.

## The assistant's voice

- Warm, concise chat: **1–3 sentences per message**, no markdown, no lists, at most one emoji.
- Uses what the person said before ("You moved the trip to the 14th, still flying from Leeds?"); never invents facts.
- Usually ends with one real question. Helps with plans, schedules, ideas, drafts, decisions.
- No medical, legal or financial advice beyond everyday common sense.

The person writes like a real chat user: short, informal, sometimes typos, one-word replies.

Do NOT write: health conditions or medication, heavy themes (grief, abuse, self-harm),
politics, religion, real people, real companies or brands.

## File format (strict JSON, UTF-8)

```json
{
  "user_id": "maya-chen",
  "display_name": "Maya",
  "profile": {"full_name": "Maya Chen", "about": "34, Toronto. Freelance product designer"},
  "sessions": [
    {
      "id": "s01",
      "started_at": "2026-08-24T09:12:00-04:00",
      "recalls": [],
      "messages": [
        {"role": "user", "text": "hey, can you help me plan my week? 3 client deadlines"},
        {"role": "assistant", "text": "Happy to. Which of the three is due first, and how many hours does it still need?"}
      ]
    }
  ],
  "portrait": "Two to four plain sentences describing the person as of the last session."
}
```

### Fields

- `sessions[].id`: `s01`, `s02`, … in time order. `started_at`: ISO time with the
  person's own UTC offset, between 2026-08-24 and 2026-10-05, at a believable hour.
- `recalls`: 0–2 questions the assistant's software would look up in memory before
  replying, e.g. "Which clients is Maya working with?". `[]` for `s01`.
- `messages`: `role` is `user` or `assistant`; alternate naturally (two in a row is fine).
  Each `text` ≤ 300 characters. **The sum of all `text` lengths in one session ≤ 2200.**
- `portrait`: ≤ 600 characters.

## How to work

1. Read your assignment (in your task prompt): who the person is, how many sessions, the arc.
2. Sketch the arc first: what is true at the start, what changes when, what stops being true.
3. Write the JSON file to the path given.
4. Run `python3 demo/check.py <your file>` and fix every error until it prints `OK`.
5. Reply with one line: the file path and the checker's last line.
