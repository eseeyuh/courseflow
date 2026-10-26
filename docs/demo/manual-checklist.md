# Manual demo checklist (about 3 minutes)

Start the API (`sh scripts/demo-api.sh`) and the UI (`cd frontend && npm run dev`), then open http://localhost:3000.

| # | Do | You should see |
|---|----|----------------|
| 1 | Click **Reset demo** in the Demo strip | "Rebuilding the initial demo state" banner, both buttons disabled; then "Demo reset to the initial course state." |
| 2 | Look at Home | Upcoming assessment *Coursework 1* with **Thu, 12 Nov 2026, 14:00 or Fri, 13 Nov 2026, 23:59**; a *Deadline conflict* and a *Found by CourseFlow* item under Needs your attention; no Recent change |
| 3 | Click the *Applied Machine Learning* course card, then *Coursework 1* | Conflict card: Source A (brief) 12 Nov 14:00 and Source B (handbook) 13 Nov 23:59, side by side; "CourseFlow will not silently choose between conflicting university sources." |
| 4 | Click **Show what Source A says**, close; then **Source B** | Each opens its own quote: the brief's "Thursday 12 November 2026, 14:00", the handbook's "13 November 2026 at 23:59" |
| 5 | In the class imbalance requirement, click **Slide 2** under *Week 7: Learning from Imbalanced Data* | Drawer: the claim, then the excerpt "With 7% positives…", Lecture, version 1, Slide 2 |
| 6 | Home, then the *Found by CourseFlow* item | Supervisor-review obligation: Mandatory, Verified, from the dissertation handbook page 2, **Required for Project Specification** |
| 7 | Click **Show where the handbook says this** | Handbook quote "Before you submit your Project Specification, your supervisor must have reviewed…" |
| 8 | Scroll to *What has to happen first* | Supervisor review, required for, Project Specification; Ethics approval, required for, Data collection |
| 9 | **Study plan**: on the *Week 7* task click **Studied** | "Saving…", then "Saved as Studied … now step 10 of 10, marked done"; the task moves to the end, marked Done |
| 10 | **Changes**: click **Simulate LMS sync** | "CourseFlow is processing the changed LMS sources" banner (about 10 s); then 2 changed sources: New deadline 19 Nov, Deadline replaced (12 Nov struck through), Conflict changed (now 13 Nov vs 19 Nov), Obligation reworded (5 to 10 working days marked); *Your study plan, recomputed* |
| 11 | Open Coursework 1 again | Conflict **Fri, 13 Nov 2026, 23:59 vs Thu, 19 Nov 2026, 14:00**; 12 Nov shown as a *Replaced deadline* |
| 12 | **Study plan** | As of 21 Oct 2026; the deadline check shows 13 Nov or 19 Nov |
| 13 | Click **Reset demo** | Back to step 2: 12 Nov vs 13 Nov, no changes, Week 7 Not started |

If anything looks stale, click **Reset demo** and start again at step 2. A second sync reports "LMS sync found no new changes."
