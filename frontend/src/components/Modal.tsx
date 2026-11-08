import { useRef } from 'react';
import type { ReactNode } from 'react';
import { Button } from './Button';
import { Dialog, DialogContent, DialogTitle, DialogDescription } from './ui/dialog';

interface ModalProps {
  open: boolean; title: string; description?: string; closeLabel: string;
  onClose: () => void; children?: ReactNode;
}
// Controlled shadcn/Radix dialog. No pointer-outside dismissal for business confirmations.
export function Modal({ open, title, description, closeLabel, onClose, children }: ModalProps) {
  const close = useRef<HTMLButtonElement>(null);
  const trigger = useRef<Element | null>(null);
  return <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }}><DialogContent
    {...(!description ? { 'aria-describedby': undefined } : {})}
    onPointerDownOutside={(event) => event.preventDefault()}
    onInteractOutside={(event) => event.preventDefault()}
    onOpenAutoFocus={(event) => { event.preventDefault(); trigger.current = document.activeElement; close.current?.focus(); }}
    onCloseAutoFocus={(event) => { event.preventDefault(); if (trigger.current instanceof HTMLElement && trigger.current.isConnected) trigger.current.focus(); }}>
    <div className="modal-body">
      <DialogTitle>{title}</DialogTitle>
      {description && <DialogDescription>{description}</DialogDescription>}
      {open && children}
      <div className="modal-actions"><Button ref={close} data-modal-close onClick={onClose}>{closeLabel}</Button></div>
    </div>
  </DialogContent></Dialog>;
}
