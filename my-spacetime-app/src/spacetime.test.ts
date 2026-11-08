import { describe, it, expect } from 'vitest';
import { people$ } from './spacetime';
import type { DbConnection } from './module_bindings';

function fakeConn() {
  let rows = [{ name: 'Ada' }];
  const listeners = { insert: new Set<() => void>(), del: new Set<() => void>() };
  const conn = {
    db: { person: {
      iter: () => rows[Symbol.iterator](),
      onInsert: (f: () => void) => listeners.insert.add(f),
      onDelete: (f: () => void) => listeners.del.add(f),
      removeOnInsert: (f: () => void) => listeners.insert.delete(f),
      removeOnDelete: (f: () => void) => listeners.del.delete(f),
    } },
  } as unknown as DbConnection;
  return {
    conn, listeners,
    insert(name: string) { rows = [...rows, { name }]; listeners.insert.forEach((f) => f()); },
  };
}

describe('people$', () => {
  it('emits the initial snapshot, then again on insert', () => {
    const { conn, insert } = fakeConn();
    const out: string[][] = [];
    const sub = people$(conn).subscribe((r) => out.push(r.map((p) => p.name)));

    insert('Grace');

    expect(out).toEqual([['Ada'], ['Ada', 'Grace']]);
    sub.unsubscribe();
  });

  it('removes its listeners on unsubscribe', () => {
    const { conn, listeners } = fakeConn();
    people$(conn).subscribe().unsubscribe();
    expect(listeners.insert.size).toBe(0);
    expect(listeners.del.size).toBe(0);
  });
});
