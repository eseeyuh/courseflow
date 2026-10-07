/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

/**
 * Browser-visible frontend configuration.
 *
 * NEXT_PUBLIC_* values are inlined into the client bundle at build time, so
 * they are public. Only non-secret configuration belongs here. The variable
 * must be referenced literally (process.env.NEXT_PUBLIC_...) for Next.js to
 * inline it.
 */

function normaliseBaseUrl(raw: string | undefined): string | null {
  const value = raw?.trim();
  if (!value) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    return url.origin + url.pathname.replace(/\/+$/, "");
  } catch {
    return null;
  }
}

/** Base URL of the CourseFlow API, or null when missing/invalid. */
export const apiBaseUrl: string | null = normaliseBaseUrl(process.env.NEXT_PUBLIC_API_BASE_URL);
