import { Observable } from "rxjs";
import { DbConnection } from "./module_bindings";

export const connect$ = (uri: string, dbName: string, token?: string) =>
  new Observable<DbConnection>((sub) => {
    const conn = DbConnection.builder()
      .withUri(uri)
      .withDatabaseName(dbName)
      .withToken(token)
      .onConnect((c) => {
        c.subscriptionBuilder()
          .onApplied(() => {
            sub.next(c);
            sub.complete();
          })
          .subscribe(["SELECT * FROM person"]);
      })
      .onConnectError((_ctx, err) => sub.error(err))
      .build();

    return () => conn.disconnect();
  });

export interface PersonRow { name: string }

/** Live stream of the person table. Re-emits on every insert/delete. */
export const people$ = (conn: DbConnection): Observable<PersonRow[]> =>
  new Observable((sub) => {
    const emit = () =>
      sub.next(Array.from(conn.db.person.iter(), ({ name }) => ({ name })));
    emit();
    conn.db.person.onInsert(emit);
    conn.db.person.onDelete(emit);
    return () => {
      conn.db.person.removeOnInsert(emit);
      conn.db.person.removeOnDelete(emit);
    };
  });