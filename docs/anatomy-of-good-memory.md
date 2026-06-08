# Anatomy of good memory

A tour of what each memory layer is for, and what makes a memory worth keeping.
This is documentation — it does **not** ship inside your memory pack, so it never
costs context or gets confused for one of your own notes. Read it once to build a
mental model of where things go; the directive scaffolds in your memory home will
remind you in the moment.

REKOL memory is split by **trigger** — *when does Claude need this?* — not by
topic. The same project might leave a fact in `topics/`, a rule in `when/`, and
the reasoning in `knowledge/`. Pick the layer by how the memory should surface.

## The four layers

| Layer | What it holds | When it surfaces | Tiny example |
| --- | --- | --- | --- |
| `always/` | Permanent foundational facts about you. Re-injected into every session, so it has a hard **8 KB** budget — keep it tight. | Every session, automatically. | `always/identity.md`: "I'm a backend engineer on the payments team." |
| `when/` | Task-triggered rules and conventions. Named `when-<activity>.md`. | When the activity matches the request. | `when/when-touching-repos.md`: "Repos live in `~/dev/<name>`; check the local clone before cloning fresh." |
| `topics/` | Canonical-source pointers per noun. Named `topics/<noun>.md`. A pointer, not a copy. | When the noun appears in the request. | `topics/billing-api.md`: "The billing API base URL is defined in `infra/config.yaml` — don't guess it." |
| `knowledge/` | Long-form durable reference and **reasoning** — the "understanding" layer. | On demand, via `rekol search`. | `knowledge/why-we-chose-x.md`: "We chose Postgres over Mongo because…" |

## Layer vs layer — the distinction that trips people up

- **`always/` vs everything else:** if it doesn't need to be in *every* session,
  it doesn't belong in `always/`. The 8 KB cap is a feature: it forces the
  always-on context to stay small so the model actually reads it.
- **`when/` vs `topics/`:** `when/` is a **rule** triggered by an *activity*
  ("before editing a repo, do X"). `topics/` is a **fact source** triggered by a
  *noun* ("the truth about `<service>` lives here"). Rule → `when/`; where the
  answer lives → `topics/`.
- **`topics/` vs `knowledge/`:** `topics/` is the short pointer ("the URL lives
  in this repo"). `knowledge/` is the long-form *why* ("we picked this stack
  because…"). If you're explaining reasoning or background, it's `knowledge/`.

## What makes a memory "good"

- **Earns its place:** would a *future* session genuinely benefit? If not, skip it.
- **Right layer, right trigger:** a rule the model never sees at the right moment
  is dead weight; a fact buried in `knowledge/` won't fire on the noun.
- **Points, doesn't copy:** for anything with a canonical home, link to it. Copies
  drift; pointers stay correct.
- **Honest about staleness:** when a fact changes, fix it (and run
  `rekol index update` after hand-edits). Stale memory is worse than none.

## A note on the scaffolds

Each layer in your memory home ships with a short **directive** scaffold — a note
to the assistant describing *what to learn and record there* as it works with you
(your name and role in `always/identity.md`, your repo conventions in
`when/when-touching-repos.md`, and so on). The scaffolds carry no example facts on
purpose: anything written in a memory body is read as truth, so a placeholder like
a fake name would be a confabulation hazard. The scaffolds tell the assistant what
*kind* of thing belongs there; the real content grows as it learns about you.
