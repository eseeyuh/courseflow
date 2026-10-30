/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

import type { Metadata } from "next";

import { DemoProgress, TopBar } from "@/components/DemoControls";
import { DemoStateProvider } from "@/components/DemoState";
import { EvidenceProvider } from "@/components/Evidence";
import { Sidebar } from "@/components/Nav";

import "./globals.css";

export const metadata: Metadata = {
  title: "CourseFlow",
  description: "Evidence-backed academic navigation.",
};

// Geist and Instrument Serif (both SIL Open Font Licence) load from Google
// Fonts at view time; the build never needs the network, and an offline
// viewer gets the close system fallbacks named in globals.css.
const FONTS = "https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Instrument+Serif&display=swap";

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className="h-full antialiased">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link rel="stylesheet" href={FONTS} />
      </head>
      <body className="min-h-full">
        <DemoStateProvider>
          <EvidenceProvider>
            <div className="flex min-h-screen">
              <Sidebar />
              <div className="flex min-w-0 flex-1 flex-col">
                <TopBar />
                <main className="mx-auto w-full max-w-[1520px] flex-1 px-8 pt-10 pb-16 xl:px-12">
                  <DemoProgress />
                  {children}
                </main>
              </div>
            </div>
          </EvidenceProvider>
        </DemoStateProvider>
      </body>
    </html>
  );
}
