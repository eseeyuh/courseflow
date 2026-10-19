# CourseFlow retrieval benchmark v1

A small, fully synthetic benchmark for course-scoped retrieval. It was frozen before any retrieval score was computed on it. Quillmere Institute and its two courses are fictional: no text describes a real institution, course or person.

## Contents

| Path | Purpose |
|---|---|
| `manifest.json` | Inventory, counts, rules, critical case and hash definition (`retrieval-manifest/v1`) |
| `cases.jsonl` | 14 cases, one JSON object per line (`retrieval-case/v1`) |
| `corpus/<course>/<resource>/v<n>.html` | Source documents, ingested with the production HTML parser |
| `dataset.sha256` | The dataset hash (not itself a hash input) |
| `README.md` | This file (not a hash input) |

Two courses:

- `ds101` (DS101 Data Systems): 10 resources. Lectures, labs, a module handbook, an optional reading that is semantically close to several topics, an unrelated careers notice, and a coursework brief with two versions. Version 2 supersedes version 1 (new deadline, individual rather than group work, stricter normalisation requirement, new generative AI rule).
- `snd210` (SND210 Sound Design for Media): 3 resources. It contains near-duplicates of DS101 statements (a Coursework 1 deadline, a late-submission rule, an extension rule) and the homonym "normalisation" in its audio sense. It exists mainly to prove that no search of one course ever returns the other.

With the production `span-pack/v1` chunker the corpus yields 39 chunks in current versions and 41 in all versions; the manifest records both and validation fails if they change.

Every document is HTML. This is a choice about what the evaluation covers, not a limit of the platform: the benchmark measures retrieval, so it uses one parser and keeps parser differences out of the comparison. Parsing PDF, DOCX and PPTX is tested separately.

## Cases

| Category | Count | What it tests |
|---|---|---|
| `keyword` | 4 | Identifiers and exact terms (one case uses a `resource_types` filter) |
| `paraphrase` | 5 | Natural questions that avoid the target's distinctive words |
| `mixed` | 3 | An identifier plus paraphrased intent |
| `stale_version_invariant` | 1 | A query that only the superseded brief answers |
| `cross_course_invariant` | 1 | A query that only the other course answers |

A positive case has target groups. Each group is one required piece of information, and any one of its alternatives satisfies it. Every v1 positive case has exactly one group. A target is a course, resource, version and exact quote, which must occur exactly once in that version's extracted text unless an explicit `occurrence` selects one. Targets always name the current version, because only current versions are searchable.

Distractors are labelled challenges, not expected ranking outcomes. The five kinds are: same-course semantic, same-course irrelevant, stale version, cross-course near-duplicate and cross-course homonym.

**Relevance rule (`full-target-containment/v1`).** A target is the absolute range `[T_start, T_end)` of its quote in one resource version's extracted text. A retrieved chunk of that same version with range `[C_start, C_end)` satisfies the target if and only if `C_start <= T_start` and `C_end >= T_end`. Partial overlap does not count: a chunk that touches only part of the labelled evidence gets no credit. A target group is satisfied when any one of its alternatives is satisfied. If a different chunker splits a target so that no chunk contains all of it, that counts against the chunker. The quotes are never changed to suit a chunker.

**Target span rule (`single-source-span/v1`).** Every labelled quote lies wholly inside exactly one SourceSpan of its version (`S_start <= T_start` and `S_end >= T_end`), so every target is an exact sub-range of one SourceSpan.

The manifest names both rules by their versioned IDs and repeats each canonical definition under `definitions`. The loader rejects a manifest whose definitions differ from the code's. Changing a rule's meaning requires a new rule version.

**Hard invariants.** A run fails if any hit, in any case, comes from another course or from a non-current version. An invariant case also fails if any hit comes from one of its forbidden references. Invariant cases have no positive targets and are excluded from Recall@k and MRR denominators.

**Critical case.** `ds101-mixed-cw1-deadline` was chosen before any score existed. The selected default retrieval mode must place an acceptable target in the top 3. The reason is recorded in the manifest.

## Dataset hash

`sha256-path-and-bytes/v1` is SHA-256 over this byte stream:

1. the ASCII header `sha256-path-and-bytes/v1` and one LF (`0x0A`);
2. then, for each input file in ascending bytewise order of its relative POSIX path encoded as UTF-8: the path bytes, one NUL (`0x00`), the file's byte length as ASCII decimal digits with no leading zeros, one NUL, then the file's bytes.

The inputs are `manifest.json`, `cases.jsonl` and every corpus file the manifest lists. `dataset.sha256` and this README are not inputs. `dataset.sha256` holds the result as 64 lowercase hexadecimal digits and a LF. Paths cannot contain NUL, and every file's contents are prefixed with their length, so the stream is unambiguous. Hashed files must be UTF-8 with LF line endings, and an unlisted file in `corpus/` is an error, so a checkout's line-ending settings cannot change the hash. The hash covers no database IDs, timestamps, scores, reports or vectors.

An independent reproduction in Python:

```python
import hashlib, json, pathlib
root = pathlib.Path("evals/datasets/retrieval/v1")
m = json.loads((root / "manifest.json").read_bytes())
paths = ["manifest.json", "cases.jsonl"] + [
    v["file"] for c in m["courses"] for r in c["resources"] for v in r["versions"]
]
h = hashlib.sha256(b"sha256-path-and-bytes/v1\n")
for p in sorted(paths, key=lambda p: p.encode("utf-8")):
    data = (root / p).read_bytes()
    h.update(p.encode("utf-8") + b"\0" + str(len(data)).encode("ascii") + b"\0" + data)
print(h.hexdigest())
```

## Validating

From `backend/`:

```bash
uv run python -m app.evaluation.retrieval.cli check --dataset ../evals/datasets/retrieval/v1
uv run python -m app.evaluation.retrieval.cli validate --dataset ../evals/datasets/retrieval/v1
```

`check` validates the files and the recorded hash offline. `validate` also ingests the corpus with the production pipeline and resolves every quote to a SourceSpan sub-range and the chunks that contain it. It runs inside one database transaction that is always rolled back. Neither command embeds, ranks or scores anything.

## Changes

A frozen version is never edited. A labelling error found later is fixed in a new version (`v2`) with an entry here, and results reported against v1 stay valid for v1.

- v1: initial frozen release.
