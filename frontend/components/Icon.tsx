/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

// A few stroke icons, drawn inline so the interface needs no icon package.
// Decorative only: every icon sits next to text that says the same thing.

const PATHS = {
  home: "M3 10.5 12 3l9 7.5M5 9v11h5v-6h4v6h5V9",
  library: "M4 4h4v16H4zM10 4h4v16h-4zM16.5 4.5l3.5.9-3.6 14.1-3.5-.9",
  calendar: "M4 6h16v14H4zM4 10h16M8 3v4M16 3v4M9 15l2 2 4-4",
  history: "M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5M12 7v5l3 2",
  sparkles: "M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8zM19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z",
  alert: "M12 3 2 20h20zM12 9v5M12 17v.5",
  clock: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 7v5l3 2",
  check: "M4 12.5l5 5L20 6.5",
  checkCircle: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM8 12.5l3 3 5-6",
  arrowRight: "M4 12h15M13 6l6 6-6 6",
  chevronRight: "M9 5l7 7-7 7",
  refresh: "M20 11a8 8 0 0 0-14.3-4.9L4 8M4 3v5h5M4 13a8 8 0 0 0 14.3 4.9L20 16M20 21v-5h-5",
  lock: "M6 11h12v9H6zM8.5 11V8a3.5 3.5 0 0 1 7 0v3",
  file: "M6 3h8l4 4v14H6zM14 3v4h4M9 13h6M9 17h6",
  quote: "M5 17c0-4 1.5-7 5-9M14 17c0-4 1.5-7 5-9M5 17h4v-4H5zM14 17h4v-4h-4z",
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name, size = 16, className = "", strokeWidth = 1.8 }: {
  name: IconName;
  size?: number;
  className?: string;
  strokeWidth?: number;
}) {
  return (
    <svg aria-hidden viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor"
      strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" className={`shrink-0 ${className}`}>
      <path d={PATHS[name]} />
    </svg>
  );
}

export function Logo() {
  return (
    <span className="flex items-center gap-2.5">
      <svg aria-hidden width={26} height={26} viewBox="0 0 32 32">
        <rect width="32" height="32" rx="8" fill="#3B4ECF" />
        <path d="M8.5 21c3.2-8.5 11.8-8.5 15 0" stroke="white" strokeWidth="2.8" fill="none" strokeLinecap="round" />
        <circle cx="23.5" cy="21" r="2.6" fill="white" />
        <circle cx="8.5" cy="21" r="1.6" fill="white" opacity=".55" />
      </svg>
      <span className="text-[16.5px] font-semibold tracking-tight text-ink">CourseFlow</span>
    </span>
  );
}
