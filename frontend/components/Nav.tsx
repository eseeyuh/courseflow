/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { NAV_ITEMS } from "@/lib/navigation";

export function Nav() {
  const path = usePathname();
  return (
    <nav aria-label="Main">
      <ul className="flex flex-wrap gap-1 text-sm">
        {NAV_ITEMS.map((item) => {
          const active = item.href === "/" ? path === "/" : path.startsWith(item.href)
            || (item.href === "/courses" && path.startsWith("/assessments"));
          return (
            <li key={item.href}>
              <Link href={item.href} aria-current={active ? "page" : undefined}
                className={`rounded-md px-3 py-1.5 ${active ? "bg-accent-soft font-medium text-accent" : "text-muted hover:text-ink"}`}>
                {item.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
