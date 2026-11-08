// Adapted from shadcn/ui (MIT); see THIRD_PARTY_NOTICES.md.
import type { ComponentProps } from 'react';
import { Slot } from '@radix-ui/react-slot';
import { cva } from 'class-variance-authority';
import { clsx } from 'clsx';

const variants = cva('button', {
  variants: { variant: { primary: 'button--primary', secondary: 'button--secondary', quiet: 'button--quiet', danger: 'button--danger' } },
  defaultVariants: { variant: 'secondary' },
});
export function ShadcnButton({ className, variant = 'secondary', asChild = false, ...props }: ComponentProps<'button'> & {
  variant?: 'primary' | 'secondary' | 'quiet' | 'danger'; asChild?: boolean;
}) {
  const Comp = asChild ? Slot : 'button';
  return <Comp data-slot="button" data-variant={variant} className={clsx(variants({ variant }), className)} {...props} />;
}
