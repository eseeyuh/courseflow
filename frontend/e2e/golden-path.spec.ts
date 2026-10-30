/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

import { expect, test, type Page } from "@playwright/test";

// The judge demo, end to end against the real offline API. Run it three
// times from reset: `npx playwright test --repeat-each=3`.

async function demoAction(page: Page, testId: "reset-demo" | "simulate-sync", done: RegExp) {
  const header = page.getByRole("banner");
  await header.getByTestId(testId).click();
  await expect(header.getByTestId(testId)).toBeDisabled(); // no duplicate click while running
  await expect(page.getByTestId("demo-progress")).toBeVisible(); // the page says CourseFlow is working
  for (const button of await page.getByTestId(testId).all()) await expect(button).toBeDisabled(); // every copy
  await expect(header.getByTestId("demo-status")).toHaveText(done, { timeout: 180_000 });
  await expect(header.getByTestId(testId)).toBeEnabled();
}

async function openEvidence(page: Page, trigger: ReturnType<Page["getByTestId"]>) {
  await trigger.click();
  const drawer = page.getByRole("dialog", { name: "Evidence" });
  await expect(drawer).toBeVisible();
  return drawer;
}

/** A plan task by its own title (other tasks may name it as a prerequisite). */
function planTask(page: Page, title: string) {
  return page.getByTestId("plan-task").filter({ has: page.getByTestId("task-title").filter({ hasText: title }) });
}

async function closeEvidence(page: Page) {
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Evidence" })).toBeHidden();
}

test("the CourseFlow golden path, from reset to a propagated LMS change", async ({ page }) => {
  // Reset demo -> Home
  await page.goto("/");
  await demoAction(page, "reset-demo", /Demo reset to the initial course state/);
  await expect(page.getByTestId("attention-conflict")).toContainText("Thu, 12 Nov 2026, 14:00 or Fri, 13 Nov 2026, 23:59");
  await expect(page.getByTestId("attention-change")).toHaveCount(0); // no sync yet
  const upcoming = page.getByTestId("upcoming-assessment");
  await expect(upcoming).toContainText("Coursework 1");
  await expect(upcoming).toContainText("Thu, 12 Nov 2026, 14:00 or Fri, 13 Nov 2026, 23:59"); // both, no winner

  // Applied Machine Learning -> Coursework 1
  await page.getByTestId("course-card").filter({ hasText: "Applied Machine Learning" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Applied Machine Learning");
  await expect(page.getByTestId("run-status")).toHaveCount(0); // processed: no failed or pending run to report
  await expect(page.getByTestId("course-next")).toBeVisible();
  await page.getByTestId("assessment-link").filter({ hasText: "Coursework 1" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Coursework 1");

  // Requirements -> class imbalance -> Lecture 07 -> exact evidence
  const imbalance = page.getByTestId("requirement").filter({ hasText: "class imbalance" });
  await expect(imbalance).toHaveCount(1);
  await expect(imbalance.getByTestId("material-title")).toHaveText("Week 7: Learning from Imbalanced Data");
  await expect(imbalance.getByTestId("material-rationale")).toContainText("hybrid retrieval");
  let drawer = await openEvidence(page, imbalance.getByTestId("material-location").filter({ hasText: "Slide 2" }));
  await expect(drawer.getByTestId("evidence-locator").first()).toHaveText("Slide 2");
  await expect(drawer.getByTestId("evidence-quote").first()).toContainText("With 7% positives");
  await expect(drawer.getByTestId("evidence-claim")).toContainText("class imbalance");
  await closeEvidence(page);

  // The conflict: both values, no choice, each side with its own source
  const card = page.getByTestId("conflict-card");
  await expect(card.getByTestId("conflict-value")).toHaveText(["Thu, 12 Nov 2026, 14:00", "Fri, 13 Nov 2026, 23:59"]);
  await expect(card.getByTestId("conflict-trust")).toHaveText("CourseFlow will not silently choose between conflicting university sources.");
  drawer = await openEvidence(page, card.getByRole("button", { name: "Show what Source A says" }));
  await expect(drawer.getByTestId("evidence-quote")).toContainText("Thursday 12 November 2026, 14:00");
  await closeEvidence(page);
  drawer = await openEvidence(page, card.getByRole("button", { name: "Show what Source B says" }));
  await expect(drawer.getByTestId("evidence-quote").first()).toContainText("13 November");
  await closeEvidence(page);

  // Hidden supervisor-review obligation: Home -> attention item -> evidence (3 clicks)
  await page.getByRole("link", { name: "Home", exact: true }).click();
  await page.getByTestId("attention-hidden").click();
  const hidden = page.getByTestId("hidden-obligation");
  await expect(hidden).toContainText("Found by CourseFlow");
  await expect(hidden).toContainText("supervisor");
  await expect(hidden).toContainText("Mandatory");
  await expect(hidden).toContainText("Handbook");
  await expect(hidden.getByTestId("obligation-relationship")).toContainText("Project Specification");
  drawer = await openEvidence(page, hidden.getByRole("button", { name: /says this/ }));
  await expect(drawer.getByTestId("evidence-quote").first()).toContainText("Before you submit your Project Specification");
  await closeEvidence(page);

  // Dependencies
  const deps = page.getByTestId("dependency");
  await expect(deps.filter({ hasText: "Project Specification" })).toContainText("required for");
  await expect(deps.filter({ hasText: "Ethics approval" })).toContainText("Data collection");

  // Study plan: mark Lecture 07 studied -> the plan updates
  await page.getByRole("link", { name: "Study Plan", exact: true }).click();
  const lecture = planTask(page, "Learning from Imbalanced Data");
  await expect(lecture).toHaveAttribute("data-status", "ready");
  await lecture.getByTestId("state-studied").click();
  await expect(lecture).toHaveAttribute("data-status", "completed");
  await expect(lecture.getByTestId("why-now")).toHaveText("Done: you have studied this material.");
  await expect(page.getByTestId("plan-saved")).toContainText("Saved as Studied");
  await expect(page.getByTestId("plan-saved")).toContainText("now step 22 of 22, marked done");
  await expect(page.getByTestId("plan-task").last().getByTestId("task-title")).toContainText("Learning from Imbalanced Data");
  const submit = planTask(page, "Submit Coursework 1");
  await expect(submit.getByTestId("task-prerequisite").filter({ hasText: "Learning from Imbalanced Data" }))
    .toHaveAttribute("data-status", "completed");

  // The conflict in the plan: both values, scheduling only
  const check = page.getByTestId("plan-task").filter({ has: page.locator('[data-task-type="check_conflict"]') })
    .or(page.locator('[data-task-type="check_conflict"]'));
  await expect(check.first().getByTestId("task-anchor")).toContainText("Thu, 12 Nov 2026, 14:00 or Fri, 13 Nov 2026, 23:59");
  await expect(check.first().getByTestId("why-now")).toContainText("does not decide which source is right");

  // Simulate LMS sync -> Changes
  await page.getByRole("link", { name: "Changes", exact: true }).click();
  await expect(page.getByTestId("no-changes")).toBeVisible();
  await demoAction(page, "simulate-sync", /LMS sync brought in 2 changed resources/);
  await expect(page.getByTestId("change")).toHaveCount(2);
  await expect(page.getByTestId("affected-deadline_added")).toContainText("Thu, 19 Nov 2026, 14:00");
  await expect(page.getByTestId("affected-deadline_superseded")).toContainText("Thu, 12 Nov 2026, 14:00 replaced by Thu, 19 Nov 2026, 14:00");
  const changed = page.getByTestId("affected-conflict_changed");
  await expect(changed).toContainText("Before" + "Thu, 12 Nov 2026, 14:00 vs Fri, 13 Nov 2026, 23:59");
  await expect(changed).toContainText("Now" + "Fri, 13 Nov 2026, 23:59 vs Thu, 19 Nov 2026, 14:00");
  await expect(page.getByTestId("affected-obligation_wording_changed").first()).toBeVisible();
  await expect(page.getByTestId("plan-diff")).toContainText("Check the Coursework 1");

  // The conflict is now 13 Nov vs 19 Nov; the brief's date is history
  await page.getByRole("link", { name: "My Courses", exact: true }).click();
  await page.getByTestId("course-card").filter({ hasText: "Applied Machine Learning" }).click();
  await page.getByTestId("assessment-link").filter({ hasText: "Coursework 1" }).click();
  await expect(page.getByTestId("conflict-card").getByTestId("conflict-value")).toHaveText([
    "Fri, 13 Nov 2026, 23:59",
    "Thu, 19 Nov 2026, 14:00",
  ]);
  await expect(page.getByTestId("superseded-deadline")).toContainText("Thu, 12 Nov 2026, 14:00");

  // The study plan refreshed: new clock, new conflict sides
  await page.getByRole("link", { name: "Study Plan", exact: true }).click();
  await expect(page.getByTestId("plan-as-of")).toContainText("21 Oct 2026");
  await expect(page.locator('[data-task-type="check_conflict"]').getByTestId("task-anchor")).toContainText(
    "Fri, 13 Nov 2026, 23:59 or Thu, 19 Nov 2026, 14:00",
  );

  // Reset demo -> the initial state returns
  await demoAction(page, "reset-demo", /Demo reset to the initial course state/);
  await expect(page.getByTestId("plan-as-of")).toContainText("5 Oct 2026");
  await expect(planTask(page, "Learning from Imbalanced Data")).toHaveAttribute("data-status", "ready");
  await page.getByRole("link", { name: "Home", exact: true }).click();
  await expect(page.getByTestId("attention-conflict")).toContainText("Thu, 12 Nov 2026, 14:00 or Fri, 13 Nov 2026, 23:59");
  await expect(page.getByTestId("attention-change")).toHaveCount(0);
});
