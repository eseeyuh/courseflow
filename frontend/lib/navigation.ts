/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

/** Top-level product areas (most are placeholders for now). */
export const NAV_ITEMS = [
  { href: "/", label: "Overview" },
  { href: "/courses", label: "Courses" },
  { href: "/assessments", label: "Assessments" },
  { href: "/plan", label: "Study plan" },
  { href: "/conflicts", label: "Conflicts" },
  { href: "/changes", label: "Changes" },
] as const;
