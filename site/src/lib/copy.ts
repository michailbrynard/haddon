// Copying to the clipboard for the site's copy buttons. Runs in the browser.

// Copy text that may still be on its way. Handing ClipboardItem the promise keeps the click's permission to
// write while a fetch finishes, which Safari requires; browsers without it get writeText once the text is in.
export async function copyText(text: string | Promise<string>): Promise<void> {
  if (typeof text !== 'string' && typeof ClipboardItem !== 'undefined' && navigator.clipboard?.write) {
    const blob = text.then((t) => new Blob([t], { type: 'text/plain' }));
    try {
      await navigator.clipboard.write([new ClipboardItem({ 'text/plain': blob })]);
      return;
    } catch {
      // Fall through to writeText, which some browsers allow where write() is refused.
    }
  }
  await navigator.clipboard.writeText(await text);
}

// Run a copy from a button: show "Copied" or "Copy failed" on it for a moment and say the same to screen
// readers through `status`, a live region.
export function copyFrom(button: HTMLElement, status: HTMLElement | null, text: () => string | Promise<string>) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  button.addEventListener('click', async () => {
    let state: 'copied' | 'failed' = 'copied';
    try {
      await copyText(text());
    } catch {
      state = 'failed';
    }
    button.dataset.state = state;
    if (status) status.textContent = state === 'copied' ? 'Copied to the clipboard' : 'Could not copy. Open the markdown link instead.';
    clearTimeout(timer);
    timer = setTimeout(() => {
      delete button.dataset.state;
      if (status) status.textContent = '';
    }, 2000);
  });
}
