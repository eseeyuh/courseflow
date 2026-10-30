/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

/** Display formatting of stored values. Nothing here invents a value: dates
 * are the source's local date and time as stored, locators are the stored
 * page, slide or section. */

const DAY = new Intl.DateTimeFormat("en-GB", {
  weekday: "short",
  day: "numeric",
  month: "short",
  year: "numeric",
  timeZone: "UTC",
});

/** "Thu 12 Nov 2026, 14:00" from a stored local date ("2026-11-12") and time. */
export function formatLocalDate(date: string, time?: string | null): string {
  const [y, m, d] = date.split("-").map(Number);
  const day = DAY.format(new Date(Date.UTC(y, m - 1, d)));
  return time ? `${day}, ${time}` : day;
}

/** A stored local date ("2026-11-12") in parts, for a date heading. */
export function dateParts(date: string): { weekday: string; day: string; month: string; year: string } {
  const [y, m, d] = date.split("-").map(Number);
  const at = new Date(Date.UTC(y, m - 1, d));
  const part = (options: Intl.DateTimeFormatOptions) =>
    new Intl.DateTimeFormat("en-GB", { ...options, timeZone: "UTC" }).format(at);
  return { weekday: part({ weekday: "short" }), day: String(d), month: part({ month: "short" }), year: String(y) };
}

/** Whole days from the demo clock's date (an instant, read in UTC) to a stored local date. */
export function daysUntil(asOf: string, date: string): number {
  const [y, m, d] = date.split("-").map(Number);
  const from = new Date(asOf);
  return Math.round((Date.UTC(y, m - 1, d) - Date.UTC(from.getUTCFullYear(), from.getUTCMonth(), from.getUTCDate())) / 86_400_000);
}

export function shortDate(date: string): string {
  const [y, m, d] = date.split("-").map(Number);
  return new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", timeZone: "UTC" }).format(
    new Date(Date.UTC(y, m - 1, d)),
  );
}

/** The instant as the demo clock states it, in UTC (never the viewer's clock). */
export function formatInstant(iso: string): string {
  return new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
  }).format(new Date(iso)) + " UTC";
}

type Located = { page_number: number | null; slide_number: number | null; section_path: string | null };

/** Where in the source: the stored slide, page or section, as given. */
export function locator(l: Located): string {
  if (l.slide_number !== null) return `Slide ${l.slide_number}`;
  if (l.page_number !== null) return `Page ${l.page_number}`;
  if (l.section_path) return l.section_path.split(" > ").slice(1).join(" › ") || l.section_path;
  return "Location not stated";
}

export const RESOURCE_LABELS: Record<string, string> = {
  lecture: "Lecture",
  lab: "Lab",
  reading: "Reading",
  workshop: "Workshop",
  handbook: "Handbook",
  brief: "Assessment brief",
  announcement: "Announcement",
  page: "Page",
};

export const TASK_LABELS: Record<string, string> = {
  study_material: "Study",
  obligation: "Obligation",
  submit_assessment: "Submit",
  check_conflict: "Check deadline",
  ethics_approval: "Milestone",
  data_collection: "Milestone",
};

/** "AML7001 Applied Machine Learning" -> "Applied Machine Learning". */
export function courseName(title: string): string {
  return title.replace(/^[A-Z]{2,5}\d{3,5}\s+/, "");
}

export function courseCode(title: string, code: string | null): string {
  return (code ?? title.split(" ")[0]).toUpperCase();
}
