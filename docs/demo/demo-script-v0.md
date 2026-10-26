# CourseFlow demo script (about 3 minutes)

Everything on screen comes from the CourseFlow API, rebuilt offline from committed recordings of real Nemotron runs over the synthetic Northbridge University courses. No network, no spend, the same every time.

**Before recording:** start the demo ([README: Run the demo](../../README.md#run-the-demo)), open http://localhost:3000, click **Reset demo** in the Demo strip and wait for "Demo reset to the initial course state." Dismiss the green banner. Browser at about 1440 × 900, zoom 100%.

---

## 0:00–0:25 · The problem

**Screen:** Home.

> "A university course lives in a dozen places: the assignment brief, the module handbook, lecture slides, announcements. The things a student needs are scattered across them, and sometimes they disagree.
>
> CourseFlow reads all of it and turns it into next steps, and every one of them can show you the exact sentence it came from. This is one student's week across two courses."

Point at **Upcoming assessment** (Coursework 1, two stated deadlines) and **Needs your attention**: a deadline conflict, and an obligation marked *Found by CourseFlow*.

## 0:25–1:05 · Requirement to the exact learning material

**Click:** **See what it requires and where it is taught**.

> "This is Coursework 1 in Applied Machine Learning. Each requirement is quoted straight from the brief, and under each one CourseFlow shows where the course's own teaching material covers it."

Scroll to **"Discuss the class imbalance in the data and explain how it affects both model training and the way the models should be evaluated."** Click **Slide 2** under *Week 7: Learning from Imbalanced Data*.

> "Not a summary: the original slide. 'With 7% positives, always predicting the majority class gives 93% accuracy.' Retrieval found the candidates, the Nemotron verifier could only choose among them, and what you see is the stored text of the slide, its version and its location."

Close the drawer.

## 1:05–1:35 · A hidden obligation, found

**Click:** **Home**, then the *Found by CourseFlow* item.

> "Here is something the Project Specification brief never mentions. The dissertation handbook says your supervisor must review a complete draft before you submit. CourseFlow found it, marked it mandatory, and linked it to the assessment it blocks."

Point at **Required for: Project Specification**. Click **Show where the handbook says this**.

> "Page 2 of the handbook, the exact sentence, verified against the source."

Close, scroll to **What has to happen first**:

> "And the dependencies it reconstructs: supervisor review before the specification, ethics approval before data collection."

## 1:35–2:05 · The conflict CourseFlow refuses to hide

**Click:** **Home**, then the *Deadline conflict* item.

> "Two university sources disagree. The brief says Thursday the 12th of November at two. The handbook says Friday the 13th, just before midnight."

Click **Show what Source A says**, close, then **Show what Source B says**, close.

> "Each date has its own source. CourseFlow will not silently choose between conflicting university sources. It shows both and tells you to check with the module team. The plan schedules against the earlier date to be safe, and says that this is not a ruling."

## 2:05–2:40 · The LMS changes; CourseFlow propagates it

**Click:** **Changes**, then **Simulate LMS sync** (about 10 seconds).

> "Now the module posts an announcement and a new handbook version. This simulates the demo LMS changing; it is not a live Moodle connection. CourseFlow brings in only the changed sources and reprocesses what they affect."

When the banner turns green:

> "A new deadline: the 19th. The brief's 12th is replaced, because the announcement says it replaces it. The conflict is still there, but between the current sources now: the 13th against the 19th. And the handbook's supervisor deadline went from five working days to ten. CourseFlow marks the words that changed."

## 2:40–3:00 · The study plan, and why it matters

**Click:** **Study plan**.

> "The plan has already moved: the demo clock is now the 21st of October, and the deadline check shows the 13th or the 19th."

On the *Week 7* study task, click **Studied**.

> "Mark the lecture as studied, and the plan is recomputed. Moodle stores the information. CourseFlow reconstructs the path through it, and shows you the source for every step."

---

## Recovery

- **Something looks stale:** click **Reset demo**, wait for the confirmation, and start again.
- **Sync clicked twice:** the second sync reports "LMS sync found no new changes" and changes nothing.
- **A page shows "CourseFlow could not load this page":** the API is not running. Restart it, then Reset demo.
