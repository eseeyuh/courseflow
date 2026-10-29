/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

import type { IconName } from "@/components/Icon";

/** Top-level product areas. */
export const NAV_ITEMS: readonly { href: string; label: string; icon: IconName }[] = [
  { href: "/", label: "Home", icon: "home" },
  { href: "/courses", label: "My Courses", icon: "library" },
  { href: "/study-plan", label: "Study Plan", icon: "calendar" },
  { href: "/changes", label: "Changes", icon: "history" },
];

/** Whether a top-level item is the current area. Assessments belong to courses. */
export function isActive(href: string, path: string): boolean {
  if (href === "/") return path === "/";
  if (href === "/courses") return path === "/courses" || path.startsWith("/assessments");
  return path.startsWith(href);
}
