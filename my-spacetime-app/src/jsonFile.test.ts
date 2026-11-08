// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { firstValueFrom } from 'rxjs';
import { downloadJson$, readJsonUrl$ } from './jsonFile';

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('downloadJson$', () => {
  it('creates a .json blob and clicks a download link', async () => {
    let blob: Blob | undefined;
    URL.createObjectURL = vi.fn((b: Blob) => { blob = b; return 'blob:fake'; });
    URL.revokeObjectURL = vi.fn();
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});

    await firstValueFrom(downloadJson$('out.json', [{ name: 'Ada' }]));

    expect(click).toHaveBeenCalledOnce();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:fake');
    expect(blob?.type).toBe('application/json');
    expect(JSON.parse(await blob!.text())).toEqual([{ name: 'Ada' }]);
  });
});

describe('readJsonUrl$', () => {
  it('fetches and parses JSON', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify([{ name: 'Ada' }]))));
    expect(await firstValueFrom(readJsonUrl$('/people.json'))).toEqual([{ name: 'Ada' }]);
  });
});