import { concatMap, defer, filter, from, map, of, switchMap, toArray } from "rxjs";
import { downloadJson$, readJsonFile$ } from "./jsonFile";

export interface PersonJson { name: string }
type AddFn = (params: { name: string }) => unknown;

/** JSON file -> SpacetimeDB. Emits the number of rows imported. */
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

/** Current table rows -> downloaded JSON file. */
export const exportPeople$ = (
  rows: readonly PersonJson[],
  filename = "people.json"
) =>
  of(rows).pipe(
    map((r) => r.map(({ name }) => ({ name }))),
    switchMap((r) => downloadJson$(filename, r))
  );