import { defer, Observable } from "rxjs";
import { fromFetch } from "rxjs/fetch";
import { switchMap } from "rxjs/operators";

/**
 * Reads and parses a JSON file chosen via <input type="file">.
 *
 * Wrapped in `defer` so nothing is read until someone subscribes, and so
 * each subscription re-reads the file. `file.text()` is async; if it or
 * `JSON.parse` throws, the error is delivered through the Observable's
 * error channel.
 *
 * @typeParam T - Expected shape of the parsed JSON (not validated at runtime).
 * @param file - A File object, e.g. from an <input type="file"> change event.
 * @returns Observable that emits the parsed value once, then completes.
 */
export const readJsonFile$ = <T>(file: File): Observable<T> =>
  defer(async () => JSON.parse(await file.text()) as T);

/**
 * Fetches JSON from a URL (e.g. a file in your /public folder).
 *
 * Uses RxJS `fromFetch`, which cancels the request if the subscriber
 * unsubscribes. `switchMap` turns the Response into its parsed JSON body.
 * Note: HTTP error statuses (404, 500) do not throw by themselves; the
 * body is parsed regardless, so a non-JSON error page will cause a parse error.
 *
 * @typeParam T - Expected shape of the JSON body (not validated at runtime).
 * @param url - Absolute or relative URL to fetch.
 * @returns Observable that emits the parsed JSON once, then completes.
 */
export const readJsonUrl$ = <T>(url: string): Observable<T> =>
  fromFetch(url).pipe(switchMap((res) => res.json() as Promise<T>));

/**
 * Triggers a browser download of `data` as a pretty-printed .json file.
 *
 * Serializes the data (2-space indent), wraps it in a Blob, creates a
 * temporary object URL, and clicks a hidden <a download> element to start
 * the download. The object URL is revoked afterwards to free memory.
 * Browser-only: it relies on `document`, `Blob` and `URL`.
 *
 * @typeParam T - Type of the data being serialized.
 * @param filename - Suggested file name for the download (e.g. "people.json").
 * @param data - Any JSON-serializable value.
 * @returns Observable that emits `void` once the download was triggered,
 *          then completes.
 */
export const downloadJson$ = <T>(filename: string, data: T): Observable<void> =>
  new Observable<void>((sub) => {
    const blob = new Blob([JSON.stringify(data, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
    sub.next();
    sub.complete();
  });