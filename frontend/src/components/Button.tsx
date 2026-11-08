import type { ComponentProps } from 'react';
import { ShadcnButton } from './ui/button';

type ButtonProps = ComponentProps<'button'> & {
  variant?: 'primary' | 'secondary' | 'quiet' | 'danger';
  pending?: boolean;
  pendingLabel?: string;
};
export function Button({ variant = 'secondary', type = 'button', pending = false, pendingLabel,
  disabled, className = '', children, ...props }: ButtonProps) {
  return <ShadcnButton {...props} type={type} variant={variant} className={className}
    disabled={disabled || pending} aria-busy={pending || undefined}>
    {pending && pendingLabel ? pendingLabel : children}
  </ShadcnButton>;
}
