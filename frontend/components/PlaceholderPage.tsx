/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

type PlaceholderPageProps = {
  title: string;
  purpose: string;
};

/** Marks a product area that is planned but not built yet. */
export function PlaceholderPage({ title, purpose }: PlaceholderPageProps) {
  return (
    <section>
      <h1 className="text-2xl font-semibold">{title}</h1>
      <p className="mt-2 text-neutral-600 dark:text-neutral-400">{purpose}</p>
      <p className="mt-4 inline-block rounded bg-neutral-100 px-2 py-1 text-sm text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300">
        Not built yet
      </p>
    </section>
  );
}
