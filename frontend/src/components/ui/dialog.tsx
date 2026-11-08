// Adapted from shadcn/ui (MIT); see THIRD_PARTY_NOTICES.md.
import type { ComponentProps } from 'react';
import * as Primitive from '@radix-ui/react-dialog';
import { clsx } from 'clsx';

export function Dialog(props: ComponentProps<typeof Primitive.Root>) { return <Primitive.Root {...props} />; }
export function DialogTitle(props: ComponentProps<typeof Primitive.Title>) { return <Primitive.Title data-slot="dialog-title" {...props} />; }
export function DialogDescription(props: ComponentProps<typeof Primitive.Description>) { return <Primitive.Description data-slot="dialog-description" {...props} />; }
export function DialogContent({ className, children, ...props }: ComponentProps<typeof Primitive.Content>) {
  return <Primitive.Portal><Primitive.Overlay className="dialog-overlay" data-slot="dialog-overlay" />
    <Primitive.Content data-slot="dialog-content" className={clsx('modal', className)} {...props}>{children}</Primitive.Content>
  </Primitive.Portal>;
}
