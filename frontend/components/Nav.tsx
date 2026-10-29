/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { useApi } from "@/components/DemoState";
import { Icon, Logo } from "@/components/Icon";
import { CourseDot } from "@/components/ui";
import { api } from "@/lib/api";
import { courseName } from "@/lib/format";
import { isActive, NAV_ITEMS } from "@/lib/navigation";

/** The persistent workspace sidebar: product areas, then the courses the API
 * holds, each with its identity dot. */
export function Sidebar() {
  const path = usePathname();
  const courses = useApi(api.courses, "nav-courses");
  return (
    <aside aria-label="Workspace" className="sticky top-0 hidden h-screen w-[248px] shrink-0 flex-col border-r border-line bg-sidebar lg:flex">
      <Link href="/" className="px-5 pt-5 pb-6" aria-label="CourseFlow">
        <Logo />
      </Link>

      <p className="label mb-1.5 px-5">Workspace</p>
      <nav aria-label="Main" className="space-y-0.5 px-3">
        {NAV_ITEMS.map((item) => {
          const active = isActive(item.href, path);
          return (
            <Link key={item.href} href={item.href} aria-current={active ? "page" : undefined}
              className={`flex items-center gap-2.5 rounded-lg px-2.5 py-[7px] text-[13.5px] transition-colors ${
                active ? "bg-surface font-medium text-ink ring-1 ring-line" : "text-muted hover:bg-paper/70 hover:text-ink"}`}>
              <Icon name={item.icon} size={17} strokeWidth={active ? 2.1 : 1.8} className={active ? "text-link" : "text-faint"} />
              {item.label}
            </Link>
          );
        })}
      </nav>

      <p className="label mt-7 mb-1.5 px-5">Courses</p>
      <div className="scrollbar-thin space-y-0.5 overflow-y-auto px-3">
        {courses.data?.map((c) => {
          const active = path === `/courses/${c.id}`;
          return (
            <Link key={c.id} href={`/courses/${c.id}`} aria-current={active ? "page" : undefined} title={c.title}
              className={`flex items-center gap-2.5 rounded-lg px-2.5 py-[7px] text-[13.5px] transition-colors ${
                active ? "bg-surface text-ink ring-1 ring-line" : "text-muted hover:bg-paper/70 hover:text-ink"}`}>
              <CourseDot course={c} />
              <span className="truncate">{courseName(c.title)}</span>
            </Link>
          );
        })}
        {courses.data === undefined && courses.state === "loading" && (
          <div className="space-y-2.5 px-2.5 py-2" aria-hidden>
            <span className="skeleton block h-2.5 w-36" />
            <span className="skeleton block h-2.5 w-28" />
          </div>
        )}
        {courses.data?.length === 0 && <p className="px-2.5 py-1 text-[12.5px] text-faint">No courses loaded</p>}
      </div>

      <div className="flex-1" />
      <div className="mx-5 mb-4 leading-snug">
        <p className="font-display text-[16px] text-muted italic">AI discovers.</p>
        <p className="font-display text-[16px] text-muted italic">The source decides.</p>
      </div>
      <div className="flex items-center gap-2.5 border-t border-line px-5 py-4">
        <span aria-hidden className="grid h-7 w-7 place-items-center rounded-full bg-paper text-[11px] font-semibold text-muted ring-1 ring-line">
          NU
        </span>
        <div className="min-w-0">
          <p className="text-[13px] leading-tight font-medium text-ink-soft">Demo workspace</p>
          <p className="truncate text-[11.5px] leading-tight text-faint">Northbridge University (fictional)</p>
        </div>
      </div>
    </aside>
  );
}

/** The same areas as a compact row, for screens too narrow for the sidebar. */
export function CompactNav() {
  const path = usePathname();
  return (
    <nav aria-label="Main" className="lg:hidden">
      <ul className="flex flex-wrap gap-1 text-[13px]">
        {NAV_ITEMS.map((item) => {
          const active = isActive(item.href, path);
          return (
            <li key={item.href}>
              <Link href={item.href} aria-current={active ? "page" : undefined}
                className={`rounded-md px-2.5 py-1 ${active ? "bg-surface font-medium text-ink ring-1 ring-line" : "text-muted hover:text-ink"}`}>
                {item.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
