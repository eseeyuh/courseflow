/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

// A page that fails to render shows this instead of a blank screen; the
// navigation and demo controls stay usable. The raw error stays in the
// browser console, never on the page.

import { useEffect } from "react";

export default function PageError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => console.error(error), [error]);
  return (
    <section role="alert" className="panel max-w-xl border-conflict-line p-7">
      <p className="label text-conflict">Something went wrong</p>
      <h1 className="page-title mt-2 text-[1.9rem]">This page could not be shown</h1>
      <p className="mt-3 text-[13.5px] text-muted">
        Something in the course data could not be displayed. Try again; if it persists, use Reset demo to
        rebuild the demo state.
      </p>
      {error.digest && <p className="mt-2 text-xs text-muted">Reference {error.digest}</p>}
      <button type="button" className="btn btn-quiet mt-4" onClick={reset}>Try again</button>
    </section>
  );
}
