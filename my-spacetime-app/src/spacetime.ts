import { Observable } from "rxjs";
import { DbConnection } from "./module_bindings";

/**
 * Opens a SpacetimeDB connection and emits it once the initial data is ready.
 *
 * Flow: build the connection -> on connect, subscribe to
 * `SELECT * FROM person` -> when the server confirms the subscription
 * (`onApplied`, meaning the local cache is populated), emit the connection
 * and complete. Connection failures are forwarded as Observable errors.
 *
 * The returned teardown function calls `conn.disconnect()`. In RxJS the
 * teardown also runs when the Observable completes, so the connection is
 * closed right after it is emitted unless it is kept alive by the consumer
 * (e.g. with `shareReplay({ bufferSize: 1, refCount: true })` and a
 * long-lived subscription, or by changing this to not complete).
 *
 * @param uri - SpacetimeDB server URI (e.g. "ws://localhost:3000").
 * @param dbName - Name of the database/module to connect to.
 * @param token - Optional auth token to resume an existing identity.
 * @returns Observable emitting the ready DbConnection.
 */
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

// Plain-object view of a row in the 'person' table
export interface PersonRow { name: string }

/**
 * Live stream of the `person` table.
 *
 * Emits the current rows immediately, then re-emits a fresh full snapshot
 * whenever a row is inserted or deleted. (Updates are not listened to, so
 * in-place row edits would not trigger an emission.) Rows are mapped to
 * plain `{ name }` objects. Unsubscribing removes both listeners, so no
 * callbacks leak.
 *
 * @param conn - A ready DbConnection (e.g. from `connect$`).
 * @returns Observable of PersonRow arrays; never completes on its own.
 */
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