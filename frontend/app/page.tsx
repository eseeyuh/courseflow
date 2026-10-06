/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

import { HealthStatus } from "@/components/HealthStatus";

export default function OverviewPage() {
  return (
    <div className="space-y-6">
      <section>
        <h1 className="text-2xl font-semibold">Overview</h1>
        <p className="mt-2 max-w-prose text-neutral-600 dark:text-neutral-400">
          CourseFlow turns course material into an evidence-backed academic plan. Course import and
          analysis are not available yet.
        </p>
      </section>
      <HealthStatus />
    </div>
  );
}
