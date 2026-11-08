import { ApiFailure } from '../api/failure';
import { isUuid,object } from '../api/transport';

export interface OrganizationMember {
  membership_id:string;principal_id:string;display_name:string|null;local_display_name:string|null;
  department:string|null;job_title:string|null;groups:string[];organization_version:number;
  membership_active:boolean;principal_active:boolean;
}
function hasControl(value:string):boolean {return [...value].some((char) => char.codePointAt(0)!<32 || char.codePointAt(0)===127);}
function label(value:unknown,max:number):string|null {
  if (value===null) return null;
  if (typeof value!=='string' || !value.trim() || [...value].length>max || new TextEncoder().encode(value).length>max*4 || hasControl(value)) throw new ApiFailure('malformed');
  return value;
}
export function decodeMember(value:unknown):OrganizationMember {
  const row=object(value);
  if (!isUuid(row.membership_id) || !isUuid(row.principal_id) || !Number.isSafeInteger(row.organization_version) || (row.organization_version as number)<1 || typeof row.membership_active!=='boolean' || typeof row.principal_active!=='boolean' || !Array.isArray(row.groups) || row.groups.length>32) throw new ApiFailure('malformed');
  return {membership_id:row.membership_id,principal_id:row.principal_id,display_name:label(row.display_name,200),local_display_name:label(row.local_display_name,200),department:label(row.department,100),job_title:label(row.job_title,100),groups:row.groups.map((value) => {const group=label(value,100);if (!group) throw new ApiFailure('malformed');return group;}),organization_version:row.organization_version as number,membership_active:row.membership_active,principal_active:row.principal_active};
}
export function decodeMembers(value:unknown):{items:OrganizationMember[];next_after:string|null} {
  const row=object(value);
  if (!Array.isArray(row.items) || row.items.length>50 || row.next_after!==null && !isUuid(row.next_after)) throw new ApiFailure('malformed');
  const items=row.items.map(decodeMember);
  if (new Set(items.map((item) => item.membership_id)).size!==items.length) throw new ApiFailure('malformed');
  return {items,next_after:row.next_after as string|null};
}
export interface OrganizationForm {display_name:string;department:string;job_title:string;groups:string}
export function memberForm(member:OrganizationMember):OrganizationForm {
  return {display_name:member.local_display_name ?? '',department:member.department ?? '',job_title:member.job_title ?? '',groups:member.groups.join('\n')};
}
export function organizationInput(form:OrganizationForm):{body:Record<string,unknown>;errors:Partial<Record<keyof OrganizationForm,'length'|'control'|'groups'>>} {
  const errors:Partial<Record<keyof OrganizationForm,'length'|'control'|'groups'>>={};
  const result:Record<string,unknown>={};
  for (const field of ['display_name','department','job_title'] as const) {
    const value=form[field].trim();const max=field==='display_name'?200:100;
    if (hasControl(value)) errors[field]='control';
    else if ([...value].length>max || new TextEncoder().encode(value).length>max*4) errors[field]='length';
    result[field]=value || null;
  }
  const groups=form.groups.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
  if (groups.length>32) errors.groups='groups';
  else if (groups.some((value) => [...value].length>100 || new TextEncoder().encode(value).length>400 || hasControl(value))) errors.groups='length';
  result.groups=[...new Set(groups)];
  return {body:result,errors};
}
