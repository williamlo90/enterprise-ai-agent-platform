import { useState } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { Button } from '../../src/components/Button';
import { CheckboxField, SelectField, TextField } from '../../src/components/Fields';
import { Modal } from '../../src/components/Modal';
import { SourceHighlight } from '../../src/components/SourceHighlight';
import { StatusMessage } from '../../src/components/StatusMessage';

const dialogPrototype = HTMLDialogElement.prototype;
const originalShow = Object.getOwnPropertyDescriptor(dialogPrototype, 'showModal');
const originalClose = Object.getOwnPropertyDescriptor(dialogPrototype, 'close');
afterEach(() => {
  if (originalShow) Object.defineProperty(dialogPrototype, 'showModal', originalShow);
  else Reflect.deleteProperty(dialogPrototype, 'showModal');
  if (originalClose) Object.defineProperty(dialogPrototype, 'close', originalClose);
  else Reflect.deleteProperty(dialogPrototype, 'close');
});

it('keeps pending actions disabled without silently submitting their parent form', async () => {
  const action = vi.fn();
  const submit = vi.fn((event) => event.preventDefault());
  const { rerender } = render(<form onSubmit={submit}><Button pending pendingLabel="Menyimpan" onClick={action}>Simpan</Button></form>);
  const user = userEvent.setup();
  const button = screen.getByRole('button', { name: 'Menyimpan' });
  expect((button as HTMLButtonElement).disabled).toBe(true);
  await user.click(button);
  expect(action).not.toHaveBeenCalled();
  rerender(<form onSubmit={submit}><Button onClick={action}>Simpan</Button></form>);
  await user.click(screen.getByRole('button', { name: 'Simpan' }));
  expect(action).toHaveBeenCalledTimes(1);
  expect(submit).not.toHaveBeenCalled();
});

it('associates labels, hints and errors with actual native controls', async () => {
  render(<><TextField label="Nama dokumen" hint="Gunakan judul lengkap." error="Judul wajib diisi." required />
    <SelectField label="Pembaca" options={[{ value: 'individual', label: 'Orang tertentu' }]} error="Pilih pembaca." />
    <CheckboxField label="Periksa pembaca akhir" hint="Tidak mengubah akses otomatis." aria-invalid="true" /></>);
  const input = screen.getByLabelText('Nama dokumen');
  expect(input.getAttribute('aria-invalid')).toBe('true');
  for (const id of input.getAttribute('aria-describedby')!.split(' ')) expect(document.getElementById(id)).toBeTruthy();
  expect(screen.getByLabelText('Pembaca').getAttribute('aria-invalid')).toBe('true');
  const checkbox = screen.getByRole('checkbox', { name: 'Periksa pembaca akhir' }) as HTMLInputElement;
  expect(checkbox.getAttribute('aria-invalid')).toBe('true');
  const user = userEvent.setup();
  checkbox.focus();
  await user.keyboard(' ');
  expect(checkbox.checked).toBe(true);
});

it('renders unsafe source text as text and merges overlapping highlights without changing the content', () => {
  const text = '<img src=x onerror=alert(1)>\nKebijakan cuti';
  const { container } = render(<SourceHighlight text={text} ranges={[{ start: 0, end: 4 }, { start: 2, end: 7 }, { start: -1, end: 8 }, { start: 0, end: 999 }]} />);
  expect(container.textContent).toBe(text);
  expect(container.querySelector('img')).toBeNull();
  expect(container.querySelectorAll('mark').length).toBe(1);
  expect(container.querySelector('mark')!.textContent).toBe(text.slice(0, 7));
});

it('does not turn an empty, loading or error message into business success', () => {
  const { rerender } = render(<StatusMessage kind="empty" title="Belum ada dokumen" />);
  expect(screen.queryByRole('alert')).toBeNull();
  rerender(<StatusMessage kind="loading" title="Memuat dokumen" />);
  expect(screen.getByRole('status').getAttribute('aria-busy')).toBe('true');
  rerender(<StatusMessage kind="error" title="Dokumen tidak dapat dimuat"><p>Hubungi pengelola.</p></StatusMessage>);
  expect(screen.getByRole('alert').textContent).toContain('Hubungi pengelola.');
});

it('controls shadcn modal lifecycle, safe focus, Escape, outside click and restoration', async () => {
  function Harness() {
    const [open, setOpen] = useState(false);
    return <><Button onClick={() => setOpen(true)}>Buka</Button><Modal open={open} title="Konfirmasi" description="Tinjau kembali."
      closeLabel="Kembali" onClose={() => setOpen(false)}><p>Isi sementara</p></Modal></>;
  }
  render(<Harness />);
  const user = userEvent.setup();
  const trigger = screen.getByRole('button', { name: 'Buka' });
  await user.click(trigger);
  const dialog = screen.getByRole('dialog', { name: 'Konfirmasi' });
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Kembali' }));
  expect(dialog.getAttribute('aria-describedby')).toBeTruthy();
  fireEvent.pointerDown(document.querySelector('[data-slot="dialog-overlay"]')!);
  expect(screen.getByRole('dialog', { name: 'Konfirmasi' })).toBeTruthy();
  await user.keyboard('{Escape}');
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(screen.queryByText('Isi sementara')).toBeNull();
  expect(document.activeElement).toBe(trigger);
  await user.click(trigger);
  await user.click(screen.getByRole('button', { name: 'Kembali' }));
  expect(screen.queryByRole('dialog')).toBeNull();
});
