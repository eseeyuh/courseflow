/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

import type { Metadata } from "next";
import Link from "next/link";

import { DemoControls } from "@/components/DemoControls";
import { DemoStateProvider } from "@/components/DemoState";
import { EvidenceProvider } from "@/components/Evidence";
import { Nav } from "@/components/Nav";

import "./globals.css";

export const metadata: Metadata = {
  title: "CourseFlow",
  description: "Evidence-backed academic navigation.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="flex min-h-full flex-col">
        <DemoStateProvider>
          <EvidenceProvider>
            <header className="border-b border-line bg-white">
              <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-8 gap-y-3 px-5 py-3">
                <Link href="/" className="text-lg font-semibold tracking-tight">
                  CourseFlow
                </Link>
                <Nav />
                <div className="ml-auto">
                  <DemoControls compact />
                </div>
              </div>
            </header>
            <main className="mx-auto w-full max-w-6xl flex-1 px-5 py-8">{children}</main>
            <footer className="border-t border-line py-4 text-center text-xs text-muted">
              Demo data: Northbridge University (fictional). Every value shown comes from the CourseFlow API.
            </footer>
          </EvidenceProvider>
        </DemoStateProvider>
      </body>
    </html>
  );
}
