// sync.test.ts
import { describe, it, expect, vi } from 'vitest';
import { firstValueFrom } from 'rxjs';
import { importPeople$ } from './sync';

const jsonFile = (data: unknown) =>
  new File([typeof data === 'string' ? data : JSON.stringify(data)], 'people.json');

describe('importPeople$', () => {
  it('calls add once per row, in file order, and reports the count', async () => {
    const seen: string[] = [];
    const add = vi.fn(async ({ name }: { name: string }) => { seen.push(name); });

    const n = await firstValueFrom(
      importPeople$(jsonFile([{ name: 'Ada' }, { name: 'Grace' }]), add)
    );

    expect(n).toBe(2);
    expect(seen).toEqual(['Ada', 'Grace']);
  });

  it('skips rows without a usable name', async () => {
    const add = vi.fn(async () => {});
    const n = await firstValueFrom(
      importPeople$(jsonFile([{ name: 'Ada' }, { name: '  ' }, { nope: 1 }, null]), add)
    );
    expect(n).toBe(1);
    expect(add).toHaveBeenCalledTimes(1);
  });

  it('runs one reducer call at a time', async () => {
    let active = 0, maxActive = 0;
    const add = async () => {
      active++; maxActive = Math.max(maxActive, active);
      await new Promise((r) => setTimeout(r, 5));
      active--;
    };
    await firstValueFrom(importPeople$(jsonFile([{ name: 'a' }, { name: 'b' }, { name: 'c' }]), add));
    expect(maxActive).toBe(1);
  });

  it('handles an empty array', async () => {
    const n = await firstValueFrom(importPeople$(jsonFile([]), vi.fn()));
    expect(n).toBe(0);
  });

  it('errors on malformed JSON', async () => {
    await expect(
      firstValueFrom(importPeople$(jsonFile('{not json'), vi.fn()))
    ).rejects.toThrow();
  });

  it('errors when the reducer rejects', async () => {
    const add = vi.fn().mockRejectedValue(new Error('boom'));
    await expect(
      firstValueFrom(importPeople$(jsonFile([{ name: 'Ada' }]), add))
    ).rejects.toThrow('boom');
  });
});