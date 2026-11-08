import { concatMap, defer, filter, from, map, of, switchMap, toArray } from "rxjs";
import { downloadJson$, readJsonFile$ } from "./jsonFile";

//Shape of one person record in the JSON import/export file
export interface PersonJson { name: string }

//Signature of the SpacetimeDB reducer used to insert a person
//May return a promise or nothing
type AddFn = (params: { name: string }) => unknown;

/**
 * Imports people from a JSON file into SpacetimeDB.
 *
 * Pipeline:
 *  1. Read and parse the file (expects an array of `{ name }`).
 *  2. Flatten the array into a stream of individual rows.
 *  3. Drop invalid rows (missing, non-string, or blank `name`).
 *  4. Call the `add` reducer for each row, strictly one at a time and in
 *     file order (`concatMap`). `defer(async ...)` makes this work whether
 *     the reducer returns a Promise or void.
 *  5. Collect results and emit how many rows were imported.
 *
 * Nothing runs until the returned Observable is subscribed to.
 *
 * @param file - The JSON file selected by the user.
 * @param add - Reducer function that inserts one person.
 * @returns Observable emitting the number of imported rows, then completing.
 */
export const importPeople$ = (file: File, add: AddFn) =>
  readJsonFile$<PersonJson[]>(file).pipe(
    concatMap((rows) => from(rows)),
    filter((p) => typeof p?.name === "string" && p.name.trim() !== ""),
    // concatMap = one reducer call at a time, in file order.
    // defer(async) works whether the reducer returns a Promise or void.
    concatMap((p) => defer(async () => { await add({ name: p.name }); })),
    toArray(),
    map((done) => done.length)
  );

/**
 * Exports the given rows as a downloaded JSON file.
 *
 * Strips each row down to just `{ name }` (so extra fields on the source
 * objects are not leaked into the file), then triggers a browser download
 * via `downloadJson$`.
 *
 * @param rows - Rows to export (typically the latest value from `people$`).
 * @param filename - Download file name; defaults to "people.json".
 * @returns Observable that emits once the download was triggered, then completes.
 */
export const exportPeople$ = (
  rows: readonly PersonJson[],
  filename = "people.json"
) =>
  of(rows).pipe(
    map((r) => r.map(({ name }) => ({ name }))),
    switchMap((r) => downloadJson$(filename, r))
  );