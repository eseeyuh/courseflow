/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

// A page that fails to render shows this instead of a blank screen; the
// navigation and demo controls stay usable.

export default function PageError({ error, reset }: { error: Error; reset: () => void }) {
  return (
    <section role="alert" className="panel max-w-xl border-conflict/40 p-6">
      <h1 className="section-title text-conflict">This page could not be shown</h1>
      <p className="mt-2 text-sm text-muted">{error.message}</p>
      <button type="button" className="btn btn-quiet mt-4" onClick={reset}>Try again</button>
    </section>
  );
}
