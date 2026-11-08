import { useId } from 'react';
import type { InputHTMLAttributes, SelectHTMLAttributes, TextareaHTMLAttributes } from 'react';

interface FieldLabels { label: string; hint?: string; error?: string }
type TextFieldProps = FieldLabels & InputHTMLAttributes<HTMLInputElement>;
export function TextField({ label, hint, error, id, 'aria-describedby': describedBy, ...props }: TextFieldProps) {
  const generated = useId();
  const fieldId = id ?? generated;
  const description = [hint && `${fieldId}-hint`, error && `${fieldId}-error`, describedBy].filter(Boolean).join(' ') || undefined;
  return <div className="field">
    <label htmlFor={fieldId}>{label}</label>
    <input {...props} id={fieldId} aria-invalid={error ? true : props['aria-invalid']} aria-describedby={description} />
    {hint && <p className="field-hint" id={`${fieldId}-hint`}>{hint}</p>}
    {error && <p className="field-error" id={`${fieldId}-error`}>{error}</p>}
  </div>;
}

type SelectFieldProps = FieldLabels & SelectHTMLAttributes<HTMLSelectElement> & {
  options: readonly { value: string; label: string; lang?: string }[];
};
export function SelectField({ label, hint, error, id, options, 'aria-describedby': describedBy, ...props }: SelectFieldProps) {
  const generated = useId();
  const fieldId = id ?? generated;
  const description = [hint && `${fieldId}-hint`, error && `${fieldId}-error`, describedBy].filter(Boolean).join(' ') || undefined;
  return <div className="field">
    <label htmlFor={fieldId}>{label}</label>
    <select {...props} id={fieldId} aria-invalid={error ? true : props['aria-invalid']} aria-describedby={description}>
      {options.map((option) => <option key={option.value} value={option.value} lang={option.lang}>{option.label}</option>)}
    </select>
    {hint && <p className="field-hint" id={`${fieldId}-hint`}>{hint}</p>}
    {error && <p className="field-error" id={`${fieldId}-error`}>{error}</p>}
  </div>;
}

export function CheckboxField({ label, hint, error, id, ...props }: FieldLabels & Omit<InputHTMLAttributes<HTMLInputElement>, 'type'>) {
  const generated = useId();
  const fieldId = id ?? generated;
  return <div className="field">
    <label className="checkbox-label" htmlFor={fieldId}>
      <input {...props} type="checkbox" id={fieldId} aria-invalid={error ? true : props['aria-invalid']}
        aria-describedby={[hint && `${fieldId}-hint`, error && `${fieldId}-error`, props['aria-describedby']].filter(Boolean).join(' ') || undefined} />
      <span>{label}</span>
    </label>
    {hint && <p className="field-hint" id={`${fieldId}-hint`}>{hint}</p>}
    {error && <p className="field-error" id={`${fieldId}-error`}>{error}</p>}
  </div>;
}

export function TextAreaField({label,id,...props}:FieldLabels & TextareaHTMLAttributes<HTMLTextAreaElement>){const generated=useId();const fieldId=id??generated;return <div className="field"><label htmlFor={fieldId}>{label}</label><textarea {...props} id={fieldId}/></div>;}
