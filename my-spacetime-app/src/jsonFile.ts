import { defer, Observable } from "rxjs";
import { fromFetch } from "rxjs/fetch";
import { switchMap } from "rxjs/operators";

/** Read and parse a JSON file chosen via <input type="file">. */
export const readJsonFile$ = <T>(file: File): Observable<T> =>
  defer(async () => JSON.parse(await file.text()) as T);

/** Fetch JSON from a URL (e.g. a file in your /public folder). */
export const readJsonUrl$ = <T>(url: string): Observable<T> =>
  fromFetch(url).pipe(switchMap((res) => res.json() as Promise<T>));

/** Trigger a browser download of `data` as a .json file. */
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