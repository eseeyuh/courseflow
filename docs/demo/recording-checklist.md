# Demo recording checklist

## Before recording

- [ ] Demo running and healthy: `docker compose -f infra/demo/compose.yml up -d --build --wait` (all services healthy, `seed` exited 0), or the development setup from the README.
- [ ] http://localhost:3000 loads; click **Reset demo** and wait for "Demo reset to the initial course state."; dismiss the green banner.
- [ ] Home shows Coursework 1 with **Thu, 12 Nov 2026, 14:00 or Fri, 13 Nov 2026, 23:59** and no *Recent change*.
- [ ] Browser window about 1440 × 900, zoom 100%, a clean profile: no devtools, bookmarks bar, extensions, other tabs or private data in view.
- [ ] Notifications off (OS and browser); microphone level checked.

## Recording sequence

Follow [demo-script.md](demo-script.md):

1. **0:00** Home: problem, upcoming assessment, needs your attention.
2. **0:25** Coursework 1, then the class imbalance requirement, then **Slide 2**: drawer open.
3. **1:05** Home, then *Found by CourseFlow*, then **Show where the handbook says this**, then *What has to happen first*.
4. **1:35** Home, then *Deadline conflict*: **Show what Source A says** and **Source B says**.
5. **2:05** Changes, then **Simulate LMS sync**: wait for the green banner, then walk the change cards.
6. **2:40** Study plan (as of 21 Oct, 13 Nov or 19 Nov), then mark *Week 7* **Studied**; closing line.

## After recording

- [ ] Text readable at the export resolution (quotes in the evidence drawer especially).
- [ ] Each evidence drawer stays on screen long enough to read (about 3 seconds).
- [ ] Before and after the sync are both clearly visible: 12 Nov vs 13 Nov, then 13 Nov vs 19 Nov.
- [ ] No loading spinners or error states left in by accident; the sync wait may be trimmed but not hidden.
- [ ] Final duration under 3 minutes.
- [ ] Reset the demo afterwards.
