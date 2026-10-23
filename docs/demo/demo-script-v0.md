# CourseFlow demo script (v0, about 3 minutes)

The demo runs fully offline on the synthetic Northbridge University data: every value on screen comes from the CourseFlow API, rebuilt from committed recordings. No network, no model spend.

## Before you start

```bash
sh scripts/demo-api.sh               # API on http://127.0.0.1:8000 (its own demo database)
cd frontend && npm run dev           # UI on http://localhost:3000
```

Open http://localhost:3000 and click **Reset demo** (about 10 seconds). The header shows "Demo reset to the initial course state."

Optional rehearsal check: `cd frontend && npx playwright test` drives this exact path.

## 1. Fragmented course information (0:00–0:30)

**Screen:** Home.

> "A student's course lives in many places: assessment briefs, handbooks, lecture slides, announcements. CourseFlow reads them and keeps every claim tied to the exact source it came from. Here is one student's week across two courses."

Point at **Next actions** (from the study plan) and **Needs your attention**: a deadline conflict and an obligation marked *Found by CourseFlow*.

## 2. CourseFlow finds a hidden obligation (0:30–1:05)

**Click:** the *Found by CourseFlow* item (MSc Dissertation).

> "The Project Specification brief never mentions it, but the dissertation handbook says the supervisor must review a complete draft before you submit. CourseFlow found it, verified the wording against the source, and linked it to the assessment it blocks."

Show the **Required for: Project Specification** line, then **Show where the handbook says this**: the evidence drawer opens with the handbook, version, page and the exact sentence.

Scroll to **What has to happen first**: *Supervisor review, required for, Project Specification* and *Ethics approval, required for, Data collection*.

## 3. A requirement maps to the exact learning material (1:05–1:40)

**Click:** Courses, Applied Machine Learning, Coursework 1.

> "Each requirement is quoted from the brief. Under it, CourseFlow shows where in the course's own teaching material it is taught."

At requirement 2 (*Discuss the class imbalance…*), click **Slide 2** under *Week 7: Learning from Imbalanced Data*.

> "Not a summary: the original slide. Retrieval found it, a verifier chose it from the retrieved candidates only, and this is the exact slide text."

## 4. CourseFlow refuses to hide a deadline conflict (1:40–2:10)

**Screen:** the conflict card on Coursework 1.

> "The brief says Thursday 12 November, 14:00. The module handbook says Friday 13 November, 23:59. CourseFlow will not silently choose between conflicting university sources. It shows both, each with its source, and tells you to check with the module team."

**Click:** Study plan. Point at task 1, *Check the Coursework 1 deadline with the module team*, with both dates. Then mark *Week 7* **Studied**: the task moves to the end as done and the rest of the plan stays in order.

## 5. LMS sync changes the sources; CourseFlow propagates it (2:10–3:00)

**Click:** Changes, then **Simulate LMS sync** (about 10 seconds; it simulates the demo LMS, not a live Moodle).

> "The module posts an announcement: the deadline moves to 19 November, and it says it replaces the date in the brief. The handbook is also updated. CourseFlow brings in only the changed versions and reprocesses what they affect."

Point at the change cards: *Deadline added* (19 Nov), *Deadline superseded* (the brief's 12 Nov, replaced because the source says so), *Conflict changed* (12/13 Nov is now 13/19 Nov) and the handbook's reworded obligation (5 working days is now 10).

> "The handbook still says 13 November, so the conflict stays visible. It is just between the current sources now."

Open **Study plan**: the clock moved to 21 October and the Coursework 1 conflict task now shows 13 Nov or 19 Nov.

> "Every step you saw is evidence-backed and reproducible."

## Recovery

- Something looks stale: click **Reset demo**, wait for the confirmation, start again at step 1.
- Sync clicked twice: the second sync reports "no new changes" and creates nothing.
