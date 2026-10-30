/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

import Image from "next/image";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";

import { useApi } from "@/components/DemoState";
import { Icon, Logo } from "@/components/Icon";
import { CourseDot } from "@/components/ui";
import { api } from "@/lib/api";
import { courseName } from "@/lib/format";
import { isActive, NAV_ITEMS } from "@/lib/navigation";

/** The persistent workspace sidebar: product areas, then the courses the API
 * holds, each with its identity dot. Collapsible to an icon rail; the
 * labels stay for screen readers and as tooltips. */
export function Sidebar() {
  const path = usePathname();
  const courses = useApi(api.courses, "nav-courses");
  const [collapsed, setCollapsed] = useState(false);
  const label = collapsed ? "sr-only" : "truncate";
  const row = collapsed ? "justify-center px-0" : "px-2.5";
  return (
    <aside aria-label="Workspace"
      className={`sticky top-0 hidden h-screen shrink-0 flex-col overflow-hidden border-r border-line bg-sidebar transition-[width] duration-200 ease-out lg:flex ${
        collapsed ? "w-[68px]" : "w-[248px]"}`}>
      <div className={`flex pt-5 pb-6 ${collapsed ? "flex-col items-center gap-3 px-0" : "items-center justify-between px-5"}`}>
        <Link href="/" aria-label="CourseFlow">
          <Logo compact={collapsed} />
        </Link>
        <button type="button" onClick={() => setCollapsed((c) => !c)} aria-expanded={!collapsed}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"} title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          className="grid h-7 w-7 place-items-center rounded-md text-faint transition-colors hover:bg-paper/70 hover:text-ink">
          <Icon name="arrowRight" size={15} className={collapsed ? "" : "rotate-180"} />
        </button>
      </div>

      <p className={collapsed ? "sr-only" : "label mb-1.5 px-5"}>Workspace</p>
      <nav aria-label="Main" className="space-y-0.5 px-3">
        {NAV_ITEMS.map((item) => {
          const active = isActive(item.href, path);
          return (
            <Link key={item.href} href={item.href} aria-current={active ? "page" : undefined} title={collapsed ? item.label : undefined}
              className={`flex items-center gap-2.5 rounded-lg py-[7px] text-[13.5px] whitespace-nowrap transition-colors ${row} ${
                active ? "bg-surface font-medium text-ink ring-1 ring-line" : "text-muted hover:bg-paper/70 hover:text-ink"}`}>
              <Icon name={item.icon} size={17} strokeWidth={active ? 2.1 : 1.8} className={`shrink-0 ${active ? "text-link" : "text-faint"}`} />
              <span className={label}>{item.label}</span>
            </Link>
          );
        })}
      </nav>

      {collapsed ? <hr className="mx-4 mt-5 mb-3 border-line" /> : <p className="label mt-7 mb-1.5 px-5">Courses</p>}
      <div className="scrollbar-thin space-y-0.5 overflow-y-auto px-3">
        {courses.data?.map((c) => {
          const active = path === `/courses/${c.id}`;
          return (
            <Link key={c.id} href={`/courses/${c.id}`} aria-current={active ? "page" : undefined} title={c.title}
              className={`flex items-center gap-2.5 rounded-lg py-[7px] text-[13.5px] whitespace-nowrap transition-colors ${row} ${
                active ? "bg-surface text-ink ring-1 ring-line" : "text-muted hover:bg-paper/70 hover:text-ink"}`}>
              <CourseDot course={c} size={collapsed ? "h-2.5 w-2.5" : undefined} />
              <span className={label}>{courseName(c.title)}</span>
            </Link>
          );
        })}
        {!collapsed && courses.data === undefined && courses.state === "loading" && (
          <div className="space-y-2.5 px-2.5 py-2" aria-hidden>
            <span className="skeleton block h-2.5 w-36" />
            <span className="skeleton block h-2.5 w-28" />
          </div>
        )}
        {!collapsed && courses.data?.length === 0 && <p className="px-2.5 py-1 text-[12.5px] text-faint">No courses loaded</p>}
      </div>

      <div className="flex-1" />
      <div className={`flex items-center gap-2.5 border-t border-line py-4 ${collapsed ? "justify-center px-0" : "px-5"}`}
        title={collapsed ? "Daryn Shaidarov" : undefined}>
        <Image src="/photo_2026-06-26_22-34-01.jpg" alt="" aria-hidden width={38} height={38}
          className="h-[38px] w-[38px] shrink-0 rounded-full object-cover object-center ring-1 ring-line" />
        <div className={collapsed ? "sr-only" : "min-w-0"}>
          <p className="truncate text-[13px] leading-tight font-medium text-ink-soft">Daryn Shaidarov</p>
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
